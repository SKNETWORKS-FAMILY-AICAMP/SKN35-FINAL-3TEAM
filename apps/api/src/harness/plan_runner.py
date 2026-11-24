"""계획대로 실행하기: 플래너 → (단계마다 툴 하나) → 답 쓰기 → 답 검사.

  1. 계획     harness/planner.py 가 LLM 계획을 받아 코드로 검사·보정한다.
  2. 단계     계획의 단계마다 그 툴 하나만 LLM 에게 붙여 인자를 정하게 한다.
              인자를 코드가 정할 수 있는 툴(harness/steps.py 의 bind)은 LLM 을 부르지 않는다.
              LLM 이 그 툴을 부르지 않으면 조회 규칙의 인자(질문에서 뽑은 값)로 부른다.
              툴 실행은 AgentRunner._handle_tool_call 그대로 (권한 검사 → 승인 → 실행 → 감사 로그).
              권한 거부가 나면 남은 단계는 하지 않고 서버 고정 문구로 끝낸다 (계약 규칙 8).
  3. 답       툴 없이 답만 쓰게 한다. 서버가 만든 결과를 그대로 보여 주는 툴(리포트 저장 등)은 LLM 을 부르지 않는다.
  4. 답 검사  harness/answer_check.py (숫자·근거·완료 주장). 걸리면 이유를 알려 주고 한 번 다시 쓰게 한다.
LLM 호출은 계획·단계·요약·답을 합쳐 max_turns 번을 넘지 않는다.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from contracts.harness import Denial, LLMToolCall, Message, RequiredToolLLMClient, RunResult, ToolSpec
from contracts.permission import Principal, ToolCall
from harness.answer_check import WRITE_EFFECTS, check_answer, log_blocked
from harness.attachment_grounding import compact_system, evidence_problem, extractive_reply
from harness.denial import DoneTool
from harness.grounding import RequireSearch
from harness.planner import Plan, PlanStep, make_plan
from harness.steps import AFTER, BIND, Args, Calls, Finish, StepContext, load

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
        self.plan: Plan | None = None

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
        log.info("계획 (%s, run %s): %s %s", self.plan.source, self.run_id, self.plan.describe(),
                 "; ".join(self.plan.notes))

        ctx = StepContext(self.question, r.llm, self.specs, later=[], files=self.files)
        steps = self.plan.steps
        for index, step in enumerate(steps):
            ctx.later = [s.tool for s in steps[index + 1:]]
            ended = await self._step(step, index, len(steps), ctx)
            if ended is not None:
                return ended
        if ctx.records:
            return await self._answer_from_attachments(ctx.records)
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
        if isinstance(bound, Args):
            calls = [self._call(step.tool, bound.arguments)]
        elif isinstance(bound, Calls):
            calls = [self._call(step.tool, args) for args in bound.arguments[:MAX_CALLS_PER_STEP]]
        else:
            calls = await self._ask(step, index, total, spec)
        if not calls:
            log.info("단계 %s 를 실행할 인자를 정하지 못해 건너뜁니다 (run %s)", step.tool, self.run_id)
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

    async def _ask(self, step: PlanStep, index: int, total: int, spec: ToolSpec) -> list[LLMToolCall]:
        """이 단계의 툴 하나만 주고 LLM 에게 인자를 정하게 한다. 못 정하면 조회 규칙의 인자로."""
        calls: list[LLMToolCall] = []
        if self.calls < self.r.max_turns - 1:
            prompt = [*self.messages, Message("user", STEP_PROMPT.format(
                index=index + 1, total=total, goal=step.goal or spec.label, tool=step.tool))]
            try:
                if isinstance(self.r.llm, RequiredToolLLMClient):
                    reply = (await self.r.llm.chat_required_tool(prompt, spec)).message
                else:
                    reply = (await self.r.llm.chat(prompt, [spec])).message
                calls = [c for c in reply.tool_calls if c.tool_name == step.tool][:MAX_CALLS_PER_STEP]
            except Exception:
                log.warning("단계 %s 의 LLM 호출 실패 (run %s)", step.tool, self.run_id, exc_info=True)
            self.calls += 1
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
                verdict = check_answer(reply.content, sources=[m.content for m in self.messages if m.role != "assistant"],
                                       grounded=bool(self.succeeded & rule_tools), wrote=self.wrote)
                if not verdict.ok:
                    await log_blocked({"run_id": self.run_id, "session_id": self.session_id,
                                       "user_id": self.principal.user_id, "problems": verdict.problems(),
                                       "answer": reply.content[:1000], "retried": nudge is not None})
                    if nudge is None and self.calls < r.max_turns:
                        nudge = verdict.nudge()
                        continue
                    reply = Message("assistant", r._fallback(reply.content, verdict, doc_rule))
            self.messages.append(reply)
            return RunResult(reply, self.messages, self.calls, "completed")
        return RunResult(None, self.messages, self.calls, "max_turns")

    async def _answer_from_attachments(self, records: list[dict]) -> RunResult:
        """첨부를 읽은 계획의 답. 긴 대화 기록 대신 질문과 이번 run 의 도구 결과만 보여 준다 (원문이 잘리지 않게).
        근거 검사(evidence_problem)는 첨부 원문 + 이번에 성공한 다른 도구 결과를 근거로 본다."""
        r = self.r
        focus = [Message("system", compact_system(self.role)), Message("user", self.question),
                 *self.messages[self.start:]]
        evidence = [*records, *({"filename": d.tool_name, "text": d.content} for d in self.done if d.name != "parse_doc")]
        answer, retried = "", False
        while self.calls < r.max_turns:
            reply = (await r.llm.chat(focus, [])).message
            self.calls += 1
            answer = reply.content
            issue = evidence_problem(answer, evidence, self.question)
            if issue is None:
                break
            log.info("첨부 답변 재검증 (run %s): %s", self.run_id, issue)
            if retried:
                answer = ""
                break
            retried = True
            focus += [Message("assistant", answer),
                      Message("system", f"이 답은 사용할 수 없습니다: {issue} 확인한 text에서 직접 인용해 다시 답하세요.")]
        if not answer or evidence_problem(answer, evidence, self.question):
            answer = extractive_reply(records)
        return self._finish(answer)

    def _finish(self, text: str) -> RunResult:
        final = Message("assistant", text)
        self.messages.append(final)
        return RunResult(final, self.messages, self.calls, "completed")

    @staticmethod
    def _call(tool: str, arguments: dict) -> LLMToolCall:
        return LLMToolCall(f"plan_{uuid.uuid4().hex[:12]}", tool, arguments)
