"""계획대로 실행하기: 플래너 → (단계마다 툴 하나) → 답 쓰기 → 답 검사.

  1. 계획     harness/planner.py 가 LLM 계획을 받아 코드로 검사·보정한다.
  2. 단계     계획의 단계마다 그 툴 하나만 LLM 에게 붙여 인자를 정하게 한다.
              인자를 코드가 정할 수 있는 툴(harness/steps.py 의 bind)은 LLM 을 부르지 않는다.
              LLM 이 그 툴을 부르지 않으면 조회 규칙의 인자(질문에서 뽑은 값)로 부른다.
              툴 실행은 AgentRunner._handle_tool_call 그대로 (권한 검사 → 승인 → 실행 → 감사 로그).
              권한 거부가 나면 남은 단계는 하지 않고 서버 고정 문구로 끝낸다 (계약 규칙 8).
  3. 답       툴 없이 답만 쓰게 한다. 서버가 만든 결과를 그대로 보여 주는 툴(리포트 저장 등)은 LLM 을 부르지 않는다.
  4. 답 검사  harness/answer_check.py (숫자·근거·완료 주장). 걸리면 이유를 알려 주고 한 번 다시 쓰게 한다.
              근거는 이번 요청에서 성공한 도구 결과뿐이다 (질문·지난 대화는 근거가 아니다).
규정 모드(harness/regulation.py, 규정 질문)는 3·4 대신 원문 인용만 답한다: 검색 후보를 점수·주제로 거르고 원문을 읽은 뒤,
LLM 은 인용할 문장만 고르고 서버가 원문과 같은지 다시 확인해 인용으로 보여 준다.
첨부를 읽은 답은 근거 검사(evidence_problem)에 걸리면 첨부의 다음 부분을 이어 읽고 한 번 다시 쓰게 한다.
LLM 호출은 계획·단계·요약·답을 합쳐 max_turns 번을 넘지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import TYPE_CHECKING

from contracts.harness import Denial, LLMToolCall, Message, RequiredToolLLMClient, RunResult, ToolSpec
from contracts.permission import Principal, ToolCall
from harness.answer_check import WRITE_EFFECTS, check_answer, log_blocked
from harness.attachment_grounding import compact_system, evidence_problem, extractive_reply, parse_record
from harness.denial import DoneTool
from harness.grounding import RequireSearch
from harness.planner import Plan, PlanStep, make_plan, require_regulation_steps
from harness.regulation import SELECT_PROMPT, UNAVAILABLE, read_evidence, render_quotes, selected_quotes
from harness.steps import AFTER, BIND, Args, Calls, Finish, Skip, StepContext, load

if TYPE_CHECKING:
    from harness.runner import AgentRunner

log = logging.getLogger(__name__)

MAX_STEPS = 6
MAX_CALLS_PER_STEP = 10  # 한 단계에서 같은 툴을 여러 번 부를 수 있다 (예: 고객마다 안내 메일)
SKIPPED_CONTENT = "error: not executed: 앞선 도구 호출이 중단되어 실행하지 않았습니다"
STEP_PROMPT = ("[계획 {index}/{total}단계] {goal}\n지금은 {tool} 도구만 호출한다. 인자는 직원 요청과 앞 단계 도구 결과에 있는 값만 쓰고 "
               "지어내지 않는다. /no_think")


class PlannedRun:
    def __init__(self, runner: AgentRunner, messages: list[Message], question: str, specs: list[ToolSpec],
                 principal: Principal, session_id: str, agent_id: str | None, run_id: str | None,
                 *, files: list[dict] | None = None, role: str = "") -> None:
        self.r = runner
        self.messages = messages
        self.start = len(messages)  # 이번 run 에서 붙는 메시지의 시작 (사용자 메시지 다음)
        self.question = question.split("[사용자 첨부 파일 목록]", 1)[0]
        self.files = list(files or [])
        self.role = role
        self.specs = {s.name: s for s in specs}
        self.principal = principal
        self.session_id = session_id
        self.agent_id = agent_id
        self.run_id = run_id
        self.calls = 0  # LLM 호출 수
        self.done: list[DoneTool] = []
        self.succeeded: set[str] = set()
        self.wrote = False
        self.evidence_sources: list[str] = []  # 답 검사의 근거: 이번 요청에서 성공한 도구 결과 (+ 검증된 인자)
        self.plan: Plan | None = None
        self.notes: list[str] = []  # 건너뛴 단계의 이유 (최종 답 끝에 붙인다)

    # ── 진행 ──────────────────────────────────────────────

    async def run(self) -> RunResult:
        r = self.r
        limit = max(1, min(MAX_STEPS, r.max_turns - 2))
        history = self.messages[:-1]
        if r.max_turns >= 2:
            self.plan = await make_plan(r.llm, self.question, history, list(self.specs.values()), r.rules,
                                        limit=limit, files=self.files)
            self.calls += 1
        else:  # 계획에 쓸 호출이 없으면 규칙만으로 계획
            from harness.planner import check_plan

            self.plan = check_plan(Plan(source="fallback"), self.question, list(self.specs.values()), r.rules,
                                   limit=limit, files=self.files)
        # 서버 콘솔에서 보이게 uvicorn 로거로 남긴다 (시연 때 계획 확인용)
        logging.getLogger("uvicorn.error").info("계획 (%s, run %s): %s %s", self.plan.source, self.run_id,
                                                self.plan.describe(), "; ".join(self.plan.notes))

        regulation = r.regulation_mode and "parse_doc" not in self.plan.tools  # 첨부를 읽는 계획은 첨부 답으로
        if regulation:
            if "search_doc" not in self.specs:
                return self._finish("이 에이전트에 사내 문서 검색 도구가 없어 규정의 근거를 확인할 수 없습니다.")
            require_regulation_steps(self.plan, self.specs)
        ctx = StepContext(self.question, r.llm, self.specs, later=[], files=self.files,
                          regulation=regulation, min_score=r.regulation_min_score)
        if r.mail_recipients is not None:
            ctx.recipients = lambda: r.mail_recipients(self.principal)
        steps = self.plan.steps
        for index, step in enumerate(steps):
            ctx.later = [s.tool for s in steps[index + 1:]]
            ended = await self._step(step, index, len(steps), ctx)
            if ended is not None:
                return ended
        if ctx.records:
            return await self._answer_from_attachments(ctx)
        if regulation:
            return await self._answer_regulation(ctx)
        return await self._answer()

    async def _step(self, step: PlanStep, index: int, total: int, ctx: StepContext) -> RunResult | None:
        spec = self.specs[step.tool]
        ctx.llm_budget = max(0, self.r.max_turns - self.calls - 1)  # 답 쓰기 한 번은 남긴다
        bind = BIND.get(step.tool)
        bound = await bind(ctx) if bind else None
        self.calls += ctx.llm_calls
        ctx.llm_calls = 0
        if isinstance(bound, Finish):
            return self._finish(bound.text)
        if isinstance(bound, Skip):
            log.info("단계 %s 건너뜀 (run %s): %s", step.tool, self.run_id, bound.note)
            self.notes.append(bound.note)
            return None
        if isinstance(bound, Args):
            calls = [self._call(step.tool, bound.arguments)]
        elif isinstance(bound, Calls):
            calls = [self._call(step.tool, args) for args in bound.arguments[:MAX_CALLS_PER_STEP]]
        else:
            calls = await self._ask(step, index, total, spec, ctx)
        if not calls:
            log.info("단계 %s 를 실행할 인자를 정하지 못해 건너뜁니다 (run %s)", step.tool, self.run_id)
            if spec.effect in ("write", "send"):
                self.notes.append(f"{spec.label}은(는) 실행하지 않았습니다.")
            return None

        after = AFTER.get(step.tool)
        for _ in range(2):  # after 가 같은 툴을 한 번 더 실행하라고 할 수 있다 (재검색)
            outcomes, denial = await self._execute(calls)
            if denial is not None:
                return await self.r._stop_denied(self.messages, denial, self.done, llm_calls=self.calls)
            retry = None
            for call, (ok, content) in zip(calls, outcomes):
                if ok:
                    ctx.results[step.tool] = load(content)
                verdict = after(ctx, ok, content, call.arguments) if after else None
                if isinstance(verdict, Finish):
                    return self._finish(verdict.text)
                if isinstance(verdict, Args):
                    retry = verdict
            if retry is None:
                return None
            calls = [self._call(step.tool, retry.arguments)]
        return None

    async def _ask(self, step: PlanStep, index: int, total: int, spec: ToolSpec, ctx: StepContext) -> list[LLMToolCall]:
        """이 단계의 툴 하나만 주고 LLM 에게 인자를 정하게 한다. 못 정하면 조회 규칙의 인자로.
        코드가 고정한 인자(ctx.forced, 예: 메일 받는 사람)는 LLM 이 쓴 값 위에 덮어쓴다."""
        calls: list[LLMToolCall] = []
        if self.calls < self.r.max_turns - 1:
            goal = step.goal or spec.label
            if step.tool in ctx.hints:
                goal += "\n" + ctx.hints[step.tool]
            prompt = [*self.messages, Message("user", STEP_PROMPT.format(
                index=index + 1, total=total, goal=goal, tool=step.tool))]
            try:
                if isinstance(self.r.llm, RequiredToolLLMClient):
                    reply = (await self.r.llm.chat_required_tool(prompt, spec)).message
                else:
                    reply = (await self.r.llm.chat(prompt, [spec])).message
                calls = [c for c in reply.tool_calls if c.tool_name == step.tool][:MAX_CALLS_PER_STEP]
            except Exception:
                log.warning("단계 %s 의 LLM 호출 실패 (run %s)", step.tool, self.run_id, exc_info=True)
            self.calls += 1
        forced = ctx.forced.get(step.tool)
        if calls and forced:
            calls = [LLMToolCall(c.call_id, c.tool_name, {**c.arguments, **forced}) for c in calls[:1]]
        if calls:
            return calls
        for rule in self.r.rules:
            if step.tool in (rule.tool, *rule.alternatives):
                args = rule.arguments(self.question)
                if args is not None:
                    return [self._call(step.tool, args)]
        return []

    async def _execute(self, calls: list[LLMToolCall]) -> tuple[list[tuple[bool, str]], Denial | None]:
        """툴 호출들을 순서대로 실행한다. 거부가 나면 남은 호출은 실행하지 않는다 (계약 규칙 8-b)."""
        self.messages.append(Message("assistant", "", tool_calls=tuple(calls)))
        outcomes: list[tuple[bool, str]] = []
        for index, llm_call in enumerate(calls):
            call = ToolCall(llm_call.call_id, llm_call.tool_name, llm_call.arguments, self.principal, self.session_id,
                            agent_id=self.agent_id, run_id=self.run_id)
            outcome = await self.r._handle_tool_call(call)
            self.messages.append(outcome.message)
            outcomes.append((outcome.ok, outcome.message.content))
            if outcome.ok:
                self.done.append(DoneTool(outcome.label, outcome.message.content, call.tool_name))
                self.evidence_sources.append(outcome.message.content)
                # 성공한 작업의 식별자·수치 인자는 완료 답변에 쓸 수 있다. 검색어·메일 본문은 근거로 섞지 않는다
                if call.tool_name not in {"search_doc", "get_doc_chunk", "parse_doc"}:
                    verified = {k: v for k, v in call.arguments.items()
                                if type(v) in (int, float) or (k.endswith("_id") and isinstance(v, str)
                                                               and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", v))}
                    if verified:
                        self.evidence_sources.append(json.dumps(verified))
                self.succeeded.add(call.tool_name)
                spec = self.specs.get(call.tool_name)
                self.wrote = self.wrote or bool(spec and spec.effect in WRITE_EFFECTS)
            if outcome.denial is not None:
                for pending in calls[index + 1:]:
                    self.messages.append(Message("tool", SKIPPED_CONTENT, tool_call_id=pending.call_id))
                return outcomes, outcome.denial
        return outcomes, None

    # ── 마무리 ────────────────────────────────────────────

    async def _answer(self) -> RunResult:
        r = self.r
        rule_tools = frozenset().union(*(rule.grounded_tools for rule in r.rules)) if r.rules else frozenset()
        doc_rule = next((rule for rule in r.rules if isinstance(rule, RequireSearch) and rule.tool in self.specs), None)
        nudge: str | None = None
        while self.calls < r.max_turns:
            prompt = [*self.messages, Message("user", nudge)] if nudge else list(self.messages)
            reply = (await r.llm.chat(prompt, [])).message
            self.calls += 1
            reply = Message("assistant", reply.content)  # 답 단계에서는 툴을 주지 않는다
            if r.check_answers:
                verdict = check_answer(reply.content, sources=self.evidence_sources,
                                       grounded=bool(self.succeeded & rule_tools), wrote=self.wrote)
                if not verdict.ok:
                    await log_blocked({"run_id": self.run_id, "session_id": self.session_id,
                                       "user_id": self.principal.user_id, "problems": verdict.problems(),
                                       "answer": reply.content[:1000], "retried": nudge is not None})
                    if nudge is None and self.calls < r.max_turns:
                        nudge = verdict.nudge()
                        continue
                    reply = Message("assistant", r._fallback(reply.content, verdict, doc_rule))
            return self._finish(reply.content)
        return RunResult(None, self.messages, self.calls, "max_turns")

    async def _answer_regulation(self, ctx: StepContext) -> RunResult:
        """규정 모드의 답: LLM 은 인용할 원문만 고르고, 서버가 원문과 같은지 확인해 인용으로 보여 준다 (dy UC-1 수정과 같은 규칙)."""
        r = self.r
        if not ctx.evidence or self.calls >= r.max_turns:
            return self._finish(UNAVAILABLE)
        # 예전 대화·사용자 인용·개인 역할 프롬프트는 검증 가능한 규정 원문이 아니다
        request = {"question": self.question, "documents": [
            {"chunk_id": e.chunk_id, "title": e.title, "location": e.location, "text": e.text} for e in ctx.evidence]}
        try:
            self.calls += 1
            reply = (await r.llm.chat([Message("system", SELECT_PROMPT),
                                       Message("user", json.dumps(request, ensure_ascii=False))], [])).message
            quotes = [] if reply.tool_calls else selected_quotes(reply.content, ctx.evidence, self.question)
        except Exception:
            log.warning("규정 원문 선택 실패, 추측 답변을 차단합니다 (run %s)", self.run_id)
            return self._finish(UNAVAILABLE)
        if not quotes:
            await log_blocked({"run_id": self.run_id, "session_id": self.session_id, "user_id": self.principal.user_id,
                               "problems": ["규정 원문 인용 검증 실패 또는 근거 부족"], "answer": reply.content[:1000]})
            return self._finish(UNAVAILABLE)
        if "get_doc_chunk" in self.specs:  # 고르는 사이 권한·버전이 바뀌었을 수 있어 고른 원문을 다시 확인한다
            checked = []
            for record, quote in quotes:
                outcomes, denial = await self._execute([self._call("get_doc_chunk", {"chunk_id": record.chunk_id})])
                if denial is not None:
                    return await r._stop_denied(self.messages, denial, self.done, llm_calls=self.calls)
                ok, content = outcomes[0]
                fresh = read_evidence(content, {"chunk_id": record.chunk_id}) if ok else None
                selected = selected_quotes(json.dumps({"status": "supported", "evidence": [
                    {"chunk_id": record.chunk_id, "quote": quote}]}), [fresh], self.question) if fresh else []
                if not selected:
                    return self._finish(UNAVAILABLE)
                checked.extend(selected)
            quotes = checked
        return self._finish(render_quotes(quotes))

    async def _answer_from_attachments(self, ctx: StepContext) -> RunResult:
        """첨부를 읽은 계획의 답. 긴 대화 기록 대신 질문과 이번 run 의 도구 결과만 보여 준다 (원문이 잘리지 않게).
        근거 검사(evidence_problem)는 첨부 원문 + 이번에 성공한 다른 도구 결과를 근거로 본다.
        걸리면 첨부의 다음 부분을 이어 읽고(남은 부분이 있을 때) 한 번만 다시 쓰게 한다. 그래도 걸리면 원문 발췌."""
        r = self.r
        records = ctx.records

        def focus_messages() -> list[Message]:
            return [Message("system", compact_system(self.role)), Message("user", self.question),
                    *self.messages[self.start:]]

        def evidence() -> list[dict]:
            return [*records, *({"filename": d.tool_name, "text": d.content} for d in self.done if d.name != "parse_doc")]

        focus = focus_messages()
        answer, retried = "", False
        while self.calls < r.max_turns:
            reply = (await r.llm.chat(focus, [])).message
            self.calls += 1
            answer = reply.content
            issue = evidence_problem(answer, evidence(), self.question)
            if issue is None:
                break
            log.info("첨부 답변 재검증 (run %s): %s", self.run_id, issue)
            if retried:
                answer = ""
                break
            retried = True
            more = await self._read_more(ctx)
            if isinstance(more, RunResult):
                return more
            if more:
                log.info("첨부 %d건을 이어 읽고 다시 씁니다 (run %s)", len(more), self.run_id)
                records.extend(more)
                focus = focus_messages()
            focus += [Message("assistant", answer),
                      Message("system", f"이 답은 사용할 수 없습니다: {issue} 확인한 text에서 직접 인용해 다시 답하세요.")]
        if not answer or evidence_problem(answer, evidence(), self.question):
            answer = extractive_reply(records)
        return self._finish(answer)

    async def _read_more(self, ctx: StepContext) -> list[dict] | RunResult:
        """남은 부분이 있는 첨부를 이어 읽는다 (같은 검색어, read_status.next_offset 부터). 권한 검사·감사는 그대로."""
        calls = []
        for record in ctx.records:
            status = record.get("read_status") or {}
            args = ctx.read_args.get(record.get("file_id", ""))
            if args and status.get("has_more") and status.get("next_offset") is not None:
                calls.append(self._call("parse_doc", {**args, "offset": status["next_offset"], "max_chars": 1800}))
                ctx.read_args[record["file_id"]] = {}  # 파일마다 한 번만
        if not calls:
            return []
        outcomes, denial = await self._execute(calls)
        if denial is not None:
            return await self.r._stop_denied(self.messages, denial, self.done, llm_calls=self.calls)
        names = {f["file_id"]: f["filename"] for f in ctx.files}
        more = []
        for call, (ok, content) in zip(calls, outcomes):
            record = parse_record(content, names.get(call.arguments["file_id"], "첨부 문서")) if ok else None
            if record and record["text"].strip():
                more.append(record)
        return more

    def _finish(self, text: str) -> RunResult:
        if self.notes:  # 건너뛴 단계가 있으면 왜 안 했는지 붙인다 (예: 받는 사람을 못 찾아 메일을 보내지 않음)
            text = text.rstrip() + "\n\n" + "\n".join(dict.fromkeys(self.notes))
        final = Message("assistant", text)
        self.messages.append(final)
        return RunResult(final, self.messages, self.calls, "completed")

    @staticmethod
    def _call(tool: str, arguments: dict) -> LLMToolCall:
        return LLMToolCall(f"plan_{uuid.uuid4().hex[:12]}", tool, arguments)
