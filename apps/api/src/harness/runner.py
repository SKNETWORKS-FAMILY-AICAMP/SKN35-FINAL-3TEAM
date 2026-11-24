"""에이전트 루프: LLM ↔ 툴 왕복을 max_turns 안에서 돌린다.

규칙은 contracts/harness.py 의 '에이전트 루프' 주석을 따른다.
"""

from __future__ import annotations

import asyncio
import logging
import json
import uuid
import re
from dataclasses import dataclass

from contracts.harness import (
    Denial,
    DenialKind,
    LLMClient,
    Message,
    RunResult,
    RequiredToolLLMClient,
    ToolRegistry,
    LLMToolCall,
)
from contracts.permission import (
    ApprovalHandler,
    ApprovalOutcome,
    AuditLogger,
    AuditRecord,
    Decision,
    PermissionChecker,
    Principal,
    ToolCall,
)
from harness.denial import DoneTool, template_reply
from harness.answer_check import WRITE_EFFECTS, check_answer, drop_sentences_with, log_blocked
from harness.grounding import LookupRule, RequireSearch
from harness.review_grounding import LOOKUP_TOOL, SUBMISSION_TOOL, ReviewLookupGuard
from harness.attachment_grounding import compact_system, evidence_problem, extractive_reply, parse_record, read_call
from harness.market_grounding import CLARIFY_DATE, market_plan
from harness.regulation import (UNAVAILABLE, MIN_SCORE, SELECT_PROMPT, Evidence, candidates,
                                read_evidence, selected_quotes, render_quotes)
from harness.steps import market_summary

log = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT = 30.0  # HARNESS_TOOL_TIMEOUT_SECONDS (SYS-10)
_VERDICT: dict[str, str] = {  # LLM 에게 돌려주는 거부 메시지 머리말
    "deny": "permission denied",
    "rejected": "permission not approved (user rejected)",
    "timeout": "permission not approved (approval timed out)",
}
SKIPPED_CONTENT = "error: not executed: 앞선 도구 호출이 중단되어 실행하지 않았습니다"


@dataclass(frozen=True)
class _Outcome:
    message: Message  # role="tool"
    ok: bool = False  # 툴이 실행돼 성공했는가
    denial: Denial | None = None
    label: str = ""  # ToolSpec.label


class AgentRunner:
    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: ToolRegistry,
        checker: PermissionChecker,
        approver: ApprovalHandler,
        audit: AuditLogger,
        max_turns: int,
        tool_timeout: float | None = DEFAULT_TOOL_TIMEOUT,
        require_search: RequireSearch | None = None,
        require_review_lookup: bool = False,
        lookup_rules: tuple[LookupRule, ...] | None = None,
        check_answers: bool = True,
        planner: bool = False,
        regulation_mode: bool = False,
        regulation_min_score: float = MIN_SCORE,
        mail_recipients=None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.checker = checker
        self.approver = approver
        self.audit = audit
        self.max_turns = max_turns
        self.tool_timeout = tool_timeout  # None 이면 제한 없음
        # 조회 규칙 (harness/grounding.py). lookup_rules 가 없으면 예전 설정(require_search)만 쓴다
        self.rules: tuple[LookupRule, ...] = (lookup_rules if lookup_rules is not None
                                              else (require_search,) if require_search else ())
        self.require_review_lookup = require_review_lookup
        self.check_answers = check_answers  # 최종 답 검사 (harness/answer_check.py)
        # True 면 플래너가 계획을 세우고 단계마다 툴 하나씩 붙여 실행한다 (harness/plan_runner.py).
        # False 면 예전 흐름 (LLM 이 툴을 고르는 루프 + 시장 고정 순서)
        self.planner = planner
        # 규정 모드 (harness/regulation.py): 원문 인용만 답한다. 플래너 흐름도 이 값을 따른다
        self.regulation_mode = regulation_mode
        if not 0 <= regulation_min_score <= 1:
            raise ValueError("DOC_EVIDENCE_MIN_SCORE는 0~1이어야 합니다")
        self.regulation_min_score = regulation_min_score
        # 메일 받는 사람 목록 (services/mail_recipients.py). async (principal) -> [{"user_id","name","email"}]
        # 플래너의 메일 보내기 단계가 받는 사람을 이 목록에서만 고른다. None 이면 그 단계를 건너뛴다
        self.mail_recipients = mail_recipients

    async def run(
        self,
        user_input: str,
        principal: Principal,
        session_id: str,
        history: list[Message] | None = None,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
        attachments: list[dict] | None = None,
        attachment_role: str = "",
    ) -> RunResult:
        messages: list[Message] = list(history or [])
        messages.append(Message(role="user", content=user_input))
        specs = await self.tools.specs()
        has_parser = any(s.name == "parse_doc" for s in specs)
        review = ReviewLookupGuard.from_request(user_input, history or []) if self.require_review_lookup else None
        if self.planner and review is None:  # 심사 건(UC-04)은 이번 범위 밖이라 예전 흐름을 쓴다
            from harness.plan_runner import PlannedRun

            return await PlannedRun(self, messages, user_input, specs, principal, session_id, agent_id, run_id,
                                    files=attachments if has_parser else [], role=attachment_role).run()
        if attachments and has_parser:
            return await self._read_attachments(messages, user_input, attachments, principal, session_id,
                                                agent_id, run_id, attachment_role)
        market = market_plan(user_input, specs)
        if market:
            return await self._run_market(messages, user_input, market, principal, session_id, agent_id, run_id)
        if self.regulation_mode:
            return await self._run_regulation(user_input, messages, principal, session_id, agent_id, run_id)
        review_spec = next((spec for spec in specs if spec.name == LOOKUP_TOOL), None)
        submission_spec = next((spec for spec in specs if spec.name == SUBMISSION_TOOL), None)
        if review and review_spec is None:
            final = Message("assistant", review.unavailable(tool_missing=True))
            messages.append(final)
            return RunResult(final, messages, 0, "completed")

        done: list[DoneTool] = []  # 이번 run 에서 성공한 툴 실행 (거부 시 마무리 답변에 씀)
        by_name = {s.name: s for s in specs}
        rules = [(rule, by_name[name]) for rule in self.rules if (name := rule.available(by_name))]
        rule_tools = frozenset().union(*(rule.grounded_tools for rule, _ in rules)) if rules else frozenset()
        doc_rule = next((rule for rule, _ in rules if isinstance(rule, RequireSearch)), None)
        succeeded: set[str] = set()  # 이번 run 에서 성공한 툴 이름
        wrote = False  # 이번 run 에서 쓰기(write·send) 툴이 성공했는가
        forced_rules: set[str] = set()  # 규칙마다 run 당 한 번만 강제
        force_rule: tuple[LookupRule, object] | None = None  # 다음 호출은 이 규칙의 조회 강제
        search_hits: list[int] = []  # 문서 검색 결과 건수 (0건이면 한 번 더 찾는다)
        tried_queries: set[str] = set()
        research_call: LLMToolCall | None = None
        researched = False
        answer_nudge: str | None = None  # 답 검사에 걸려 다시 쓰게 할 때의 안내
        answer_retried = False
        evidence_sources: list[str] = []  # 이번 요청에서 성공한 도구 결과만 검증 근거로 쓴다.
        retry_review = False
        retry_submission: tuple[str, ...] = ()

        for turn in range(1, self.max_turns + 1):
            if retry_submission:
                pending = retry_submission
                retry_submission = ()
                submission_messages = review.submission_messages(messages, pending)
                if isinstance(self.llm, RequiredToolLLMClient):
                    reply = (await self.llm.chat_required_tool(submission_messages, submission_spec)).message
                else:
                    reply = (await self.llm.chat(submission_messages, [submission_spec])).message
                calls = review.submission_calls(reply, pending)
                if not calls:
                    final = Message("assistant", review.summary())
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")
                reply = Message("assistant", "", tool_calls=calls)
            elif retry_review:
                retry_review = False
                reply = (await self.llm.chat([*messages, review.nudge()], [review_spec])).message
                calls = review.retry_calls(reply)
                if not calls:
                    final = Message("assistant", review.unavailable())
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")
                reply = Message("assistant", "", tool_calls=calls)
            elif force_rule is not None:
                rule, spec = force_rule
                force_rule = None
                reply = rule.forced_call(await self._ask_tool(messages, rule, spec), user_input, spec.name)
                if reply is None:
                    final = Message("assistant", rule.unavailable)
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")
            elif research_call is not None:
                reply = Message("assistant", "", tool_calls=(research_call,))
                research_call = None
            elif answer_nudge is not None:
                # 검사에 걸린 답은 기록에 넣지 않고, 이유를 붙여 한 번 더 쓰게 한다 (툴도 같이 준다)
                reply = (await self.llm.chat([*messages, Message("user", answer_nudge)], specs)).message
                answer_nudge = None
            else:
                reply = (await self.llm.chat(list(messages), specs)).message

            if not reply.tool_calls:
                if review:
                    if review.missing:
                        if not review.retried and turn + 2 <= self.max_turns:
                            review.retried = retry_review = True
                            continue  # 확인하지 않은 모델 답변은 대화 기록에 넣지 않는다.
                        final = Message("assistant", review.unavailable())
                    else:
                        pending = review.pending_submissions()
                        if pending and submission_spec and turn + 2 <= self.max_turns:
                            review.submission_retry_requested = True
                            retry_submission = pending
                            continue
                        final = Message("assistant", review.summary())
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")
                # 조회 없이 쓴 답은 버리고 그 조회부터 시킨다 (강제 조회 + 답 쓰기 2회가 남아 있을 때만)
                pending = next(((rule, spec) for rule, spec in rules
                                if rule.tool not in forced_rules and not (succeeded & rule.grounded_tools)
                                and rule.needed(user_input, reply.content,
                                                used_other_tools=bool(succeeded - rule.grounded_tools))), None)
                if pending and turn + 2 <= self.max_turns:
                    forced_rules.add(pending[0].tool)
                    force_rule = pending
                    log.info("조회 없이 쓴 답이라 %s 를 강제합니다 (run %s)", pending[1].name, run_id)
                    continue
                if pending:
                    final = Message("assistant", pending[0].unavailable)
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")
                # 문서 검색이 모두 0건이면 검색어를 바꿔 한 번 더, 그래도 없으면 고정 문구 (결정 사항 1번)
                if (doc_rule and search_hits and not any(search_hits)
                        and doc_rule.needed(user_input, reply.content)):
                    args = None if researched else doc_rule.retry_arguments(user_input, tried_queries)
                    if args and turn + 2 <= self.max_turns:
                        researched = True
                        research_call = LLMToolCall(f"research_{uuid.uuid4().hex[:12]}", doc_rule.tool, args)
                        continue
                    final = Message("assistant", doc_rule.unavailable)
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")
                if self.check_answers:
                    verdict = check_answer(reply.content, sources=evidence_sources,
                                           grounded=bool(succeeded & rule_tools), wrote=wrote)
                    if not verdict.ok:
                        await log_blocked({"run_id": run_id, "session_id": session_id, "user_id": principal.user_id,
                                           "problems": verdict.problems(), "answer": reply.content[:1000],
                                           "retried": answer_retried})
                        if not answer_retried and turn < self.max_turns:
                            answer_retried = True
                            answer_nudge = verdict.nudge()
                            continue
                        reply = Message("assistant", self._fallback(reply.content, verdict, doc_rule))
                messages.append(reply)
                return RunResult(reply, messages, turn, "completed")
            messages.append(reply)
            if turn == self.max_turns:
                break

            denial: Denial | None = None
            lookup_before_submission = False
            for llm_call in reply.tool_calls:
                if denial is not None:  # 규칙 8-b: 거부 뒤 남은 호출은 검사·실행하지 않는다
                    messages.append(Message("tool", SKIPPED_CONTENT, tool_call_id=llm_call.call_id))
                    continue
                call = ToolCall(
                    call_id=llm_call.call_id,
                    tool_name=llm_call.tool_name,
                    arguments=llm_call.arguments,
                    principal=principal,
                    session_id=session_id,
                    agent_id=agent_id,
                    run_id=run_id,
                )
                if review and call.tool_name == SUBMISSION_TOOL:
                    case_id = call.arguments.get("case_id")
                    if (not review.registration_requested or not isinstance(case_id, str)
                            or case_id not in review.records):
                        if review.registration_requested and isinstance(case_id, str) and case_id in review.case_ids:
                            lookup_before_submission = True
                        messages.append(Message("tool", "error: 심사 요청 등록은 명시적인 등록 요청과 해당 건의 조회 성공 후에만 진행할 수 있습니다.", tool_call_id=call.call_id))
                        continue
                    if case_id in review.submission_attempted:
                        messages.append(Message("tool", "error: 같은 심사 건의 등록을 이번 요청에서 이미 시도했습니다. 실행 결과를 확인해 주세요.", tool_call_id=call.call_id))
                        continue
                    review.submission_attempted.add(case_id)
                outcome = await self._handle_tool_call(call)
                messages.append(outcome.message)
                if outcome.ok:
                    evidence_sources.append(outcome.message.content)
                    # 성공한 작업의 식별자·수치 인자는 완료 답변에서 사용할 수 있다.
                    # 질문 전체/메일 본문/문서 검색어를 규정 근거로 섞지는 않는다.
                    if call.tool_name not in {"search_doc", "get_doc_chunk", "parse_doc"}:
                        verified_inputs = {k: v for k, v in call.arguments.items()
                                           if type(v) in (int, float) or (k.endswith("_id") and isinstance(v, str)
                                                                          and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", v))}
                        if verified_inputs:
                            evidence_sources.append(json.dumps(verified_inputs))
                    if review:
                        review.record(call.tool_name, call.arguments, outcome.message.content)
                    done.append(DoneTool(outcome.label, outcome.message.content, call.tool_name))
                    succeeded.add(call.tool_name)
                    spec = by_name.get(call.tool_name)
                    wrote = wrote or bool(spec and spec.effect in WRITE_EFFECTS)
                    if doc_rule and call.tool_name == doc_rule.tool:
                        tried_queries.add(str(call.arguments.get(doc_rule.query_arg, "")))
                        search_hits.append(_hit_count(outcome.message.content))
                denial = outcome.denial

            if denial is not None:
                return await self._stop_denied(messages, denial, done, llm_calls=turn)
            if (review and review.missing and lookup_before_submission and not review.retried
                    and turn + 1 < self.max_turns):
                review.retried = retry_review = True
            if review and not review.missing and not review.registration_requested:
                # 단순 조회는 실제 결과로 바로 답해 불필요한 모델 호출과 재생성 오류를 줄인다.
                final = Message("assistant", review.summary())
                messages.append(final)
                return RunResult(final, messages, turn, "completed")
            if review and not review.missing:
                pending = review.pending_submissions()
                if pending and submission_spec and turn + 1 < self.max_turns:
                    review.submission_retry_requested = True
                    retry_submission = pending
                elif not pending or submission_spec is None or turn + 1 >= self.max_turns:
                    final = Message("assistant", review.summary())
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")

        return RunResult(None, messages, self.max_turns, "max_turns")

    async def _run_regulation(self, question, messages, principal, session_id, agent_id, run_id):
        """규정 모드: 권한을 통과한 원문 인용만 반환하며 무근거 자유 답변은 차단한다."""
        done, llm_calls = [], 0

        def finish(text):
            final = Message("assistant", text)
            messages.append(final)
            return RunResult(final, messages, llm_calls, "completed")

        async def execute(name, arguments):
            ident = "regulation_" + uuid.uuid4().hex
            messages.append(Message("assistant", "", tool_calls=(LLMToolCall(ident, name, arguments),)))
            outcome = await self._handle_tool_call(ToolCall(ident, name, arguments, principal, session_id,
                                                            agent_id=agent_id, run_id=run_id))
            messages.append(outcome.message)
            if outcome.ok:
                done.append(DoneTool(outcome.label, outcome.message.content, name))
            return outcome

        if await self.tools.get("search_doc") is None:
            return finish("이 에이전트에 사내 문서 검색 도구가 없어 규정의 근거를 확인할 수 없습니다.")
        if self.max_turns <= 0:
            return finish(UNAVAILABLE)
        rule = RequireSearch()
        queries = list(dict.fromkeys([question.strip()[:2000], rule.arguments(question)[rule.query_arg]]))
        matches = []
        for query in queries:
            outcome = await execute("search_doc", {"query": query})
            if outcome.denial:
                return await self._stop_denied(messages, outcome.denial, done, llm_calls)
            if not outcome.ok:
                return finish("문서 검색을 완료하지 못해 규정의 근거를 확인할 수 없습니다. 잠시 뒤 다시 시도해 주세요.")
            matches = candidates(outcome.message.content, question, self.regulation_min_score)
            if matches:
                break
        if not matches:
            return finish(UNAVAILABLE)
        records = []
        can_read = await self.tools.get("get_doc_chunk") is not None
        for match in matches:
            if can_read:
                outcome = await execute("get_doc_chunk", {"chunk_id": match["chunk_id"]})
                if outcome.denial:
                    return await self._stop_denied(messages, outcome.denial, done, llm_calls)
                if not outcome.ok:
                    continue
                record = read_evidence(outcome.message.content, match)
                if record:
                    records.append(record)
            elif match.get("is_truncated") is False and isinstance(match.get("title"), str):
                records.append(Evidence(match["chunk_id"], match["title"], str(match.get("location") or ""), match["preview"]))
        if not records:
            return finish(UNAVAILABLE)
        # 예전 대화·사용자 인용·개인 역할 프롬프트는 검증 가능한 규정 원문이 아니다.
        request = {"question": question, "documents": [
            {"chunk_id": r.chunk_id, "title": r.title, "location": r.location, "text": r.text} for r in records]}
        try:
            llm_calls = 1
            reply = (await self.llm.chat([Message("system", SELECT_PROMPT),
                                          Message("user", json.dumps(request, ensure_ascii=False))], [])).message
            quotes = [] if reply.tool_calls else selected_quotes(reply.content, records, question)
        except Exception:
            log.warning("규정 원문 선택 실패, 추측 답변을 차단합니다 (run %s)", run_id)
            return finish(UNAVAILABLE)
        if not quotes:
            await log_blocked({"run_id": run_id, "session_id": session_id, "user_id": principal.user_id,
                               "problems": ["규정 원문 인용 검증 실패 또는 근거 부족"], "answer": reply.content[:1000]})
            return finish(UNAVAILABLE)
        # 모델 처리 중 권한/버전이 바뀌었을 수 있으므로 선택된 원문을 다시 확인한다.
        if can_read:
            checked = []
            for record, quote in quotes:
                outcome = await execute("get_doc_chunk", {"chunk_id": record.chunk_id})
                if outcome.denial:
                    return await self._stop_denied(messages, outcome.denial, [], llm_calls)
                if not outcome.ok:
                    return finish(UNAVAILABLE)
                fresh = read_evidence(outcome.message.content, {"chunk_id": record.chunk_id})
                if fresh is None:
                    return finish(UNAVAILABLE)
                selected = selected_quotes(json.dumps({"status": "supported", "evidence": [
                    {"chunk_id": record.chunk_id, "quote": quote}]}), [fresh], question)
                if not selected:
                    return finish(UNAVAILABLE)
                checked.extend(selected)
            quotes = checked
        return finish(render_quotes(quotes))

    async def _run_market(self, messages, question, plan, principal, session_id, agent_id, run_id):
        action, arguments, wants_report = plan
        done, calls_used = [], 0

        def finish(text):
            final = Message("assistant", text)
            messages.append(final)
            return RunResult(final, messages, calls_used, "completed")

        async def execute(name, args):
            ident = "market_" + uuid.uuid4().hex
            messages.append(Message("assistant", "", tool_calls=(LLMToolCall(ident, name, args),)))
            outcome = await self._handle_tool_call(ToolCall(ident, name, args, principal, session_id,
                                                            agent_id=agent_id, run_id=run_id))
            messages.append(outcome.message)
            if outcome.ok:
                done.append(DoneTool(outcome.label, outcome.message.content, name))
            return outcome

        if action == "clarify":
            return finish(CLARIFY_DATE)
        selection = None
        if wants_report and await self.tools.get("search_report_templates") and await self.tools.get("get_report_template"):
            found = await execute("search_report_templates", {"query": question[:2000]})
            if found.denial:
                return await self._stop_denied(messages, found.denial, done, llm_calls=calls_used)
            if not found.ok:
                return finish("보고서 양식을 검색하지 못했습니다. " + found.message.content.removeprefix("error: "))
            candidates = json.loads(found.message.content)
            matches = candidates.get("matches", [])
            if not matches:
                return finish(candidates["markdown"] + "\n\n사용할 양식이 없어 보고서를 저장하지 않았습니다.")
            if len(matches) > 1 and matches[0]["score"] == matches[1]["score"]:
                return finish(candidates["markdown"] + "\n\n어느 양식을 사용할지 양식 이름을 알려 주세요.")
            chosen = matches[0]
            full = await execute("get_report_template", {"template_id": chosen["template_id"], "expected_hash": chosen["template_hash"]})
            if full.denial:
                return await self._stop_denied(messages, full.denial, done, llm_calls=calls_used)
            if not full.ok:
                return finish("선택한 보고서 양식을 읽지 못했습니다. " + full.message.content.removeprefix("error: "))
            selection = json.loads(full.message.content)
        outcome = await execute(action, arguments)
        if outcome.denial:
            return await self._stop_denied(messages, outcome.denial, done, llm_calls=calls_used)
        if not outcome.ok:
            return finish("시장 도구 실행을 완료하지 못했습니다.\n\n" + outcome.message.content.removeprefix("error: "))
        data = json.loads(outcome.message.content)
        if action in ("get_morning_report", "search_report_templates") or not wants_report:
            return finish(data.get("text", data.get("markdown", "")))
        if await self.tools.get("save_morning_report") is None:
            return finish(data["markdown"] + "\n\n이 에이전트에 리포트 저장 도구가 없어 조회 결과만 표시했습니다.")
        summary = ""
        if not re.search(r"표만|요약\s*없이", question) and self.max_turns > 0:
            summary, calls_used = await market_summary(self.llm, data, selection)
        save_args = {"snapshot_id": data["snapshot_id"], "summary": summary}
        if selection:
            save_args["template_selection_id"] = selection["template_selection_id"]
        outcome = await execute("save_morning_report", save_args)
        if outcome.denial:
            return await self._stop_denied(messages, outcome.denial, done, llm_calls=calls_used)
        if not outcome.ok:
            return finish(data["markdown"] + "\n\n리포트 저장 성공을 확인하지 못했습니다. " + outcome.message.content.removeprefix("error: "))
        saved = json.loads(outcome.message.content)
        return finish(saved.get("text", saved.get("markdown", "")))

    async def _read_attachments(self, messages, question, files, principal, session_id, agent_id, run_id, role):
        """첨부 질의는 서버가 파일·문단을 선택한다. 모든 읽기는 기존 권한/감사 경로를 지난다."""
        question = question.split("[사용자 첨부 파일 목록]", 1)[0]
        known = {f["file_id"]: f for f in files}
        records, done = [], []
        focus = [Message("system", compact_system(role)), Message("user", question)]

        async def execute(calls):
            assistant = Message("assistant", "", tool_calls=tuple(calls))
            messages.append(assistant)
            focus.append(assistant)
            for index, request in enumerate(calls):
                call = ToolCall(request.call_id, request.tool_name, request.arguments, principal, session_id,
                                agent_id=agent_id, run_id=run_id)
                outcome = await self._handle_tool_call(call)
                messages.append(outcome.message)
                focus.append(outcome.message)
                if outcome.denial:
                    for pending in calls[index + 1:]:
                        skipped = Message("tool", SKIPPED_CONTENT, tool_call_id=pending.call_id)
                        messages.append(skipped)
                        focus.append(skipped)
                    return outcome.denial
                if outcome.ok:
                    record = parse_record(outcome.message.content, known[call.arguments["file_id"]]["filename"])
                    if record:
                        records.append(record)
                    done.append(DoneTool(outcome.label, outcome.message.content, call.tool_name))
            return None

        denial = await execute([read_call(file, question) for file in files])
        if denial:
            return await self._stop_denied(messages, denial, done, llm_calls=0)
        if len(records) != len(files):
            final = Message("assistant", "첨부 문서를 읽는 과정에서 오류가 발생했습니다. 아래 도구 실행 결과를 확인해 주세요. 문서에 내용이 없다는 뜻은 아닙니다.")
            messages.append(final)
            return RunResult(final, messages, 0, "completed")
        if not any(r["text"].strip() for r in records):
            final = Message("assistant", extractive_reply(records))
            messages.append(final)
            return RunResult(final, messages, 0, "completed")

        # 서버가 관련 문단을 이미 읽었다. 답변 생성에는 불필요한 도구 스키마를 보내지 않아
        # 작은 컨텍스트에서도 원문이 잘리지 않게 한다. 잘못된 호출을 돌려주는 구현도 한 번만 처리한다.
        answer, calls_used, reread = "", 0, False
        for _ in range(min(self.max_turns, 2)):
            reply = (await self.llm.chat(focus, [])).message
            calls_used += 1
            if reply.tool_calls:
                reads = []
                if not reread:
                    for call in reply.tool_calls:
                        if (call.tool_name == "parse_doc" and isinstance(call.arguments.get("file_id"), str)
                                and call.arguments["file_id"] in known):
                            args = {**call.arguments, "max_chars": 1800}
                            args.setdefault("query", question[:2000])
                            reads.append(LLMToolCall(call.call_id, call.tool_name, args))
                if reads:
                    reread = True
                    denial = await execute(reads[:len(files)])
                    if denial:
                        return await self._stop_denied(messages, denial, done, llm_calls=calls_used)
                    continue
                focus.append(Message("system", "사내 검색이나 파일 ID 요청으로 바꾸지 말고, 이미 확인한 첨부 원문만으로 답하세요."))
                continue
            answer = reply.content
            issue = evidence_problem(answer, records, question)
            if issue is None:
                break
            log.info("첨부 답변 재검증: %s", issue)
            focus += [reply, Message("system", f"이 답은 사용할 수 없습니다: {issue} 확인한 text에서 직접 인용해 다시 답하세요.")]
        if not answer or evidence_problem(answer, records, question):
            answer = extractive_reply(records)
        final = Message("assistant", answer)
        messages.append(final)
        return RunResult(final, messages, calls_used, "completed")

    async def _ask_tool(self, messages: list[Message], rule: LookupRule, spec) -> Message | None:
        """조회 툴 하나만 주고 다시 묻는다. 안내 문구는 기록에 남기지 않는다. 실패하면 None (하네스가 직접 조회).

        LLM 이 특정 툴 호출 강제(tool_choice)를 지원하면 그걸 쓴다 (심사 등록 강제와 같은 방식).
        지원하지 않거나 그래도 안 부르면 rule.forced_call 이 질문에서 뽑은 인자로 직접 부른다.
        """
        prompt = [*messages, rule.nudge(spec.name)]
        try:
            if isinstance(self.llm, RequiredToolLLMClient):
                return (await self.llm.chat_required_tool(prompt, spec)).message
            return (await self.llm.chat(prompt, [spec])).message
        except Exception:
            log.warning("조회 강제 LLM 호출 실패, 질문으로 직접 조회 (%s)", rule.tool, exc_info=True)
            return None

    @staticmethod
    def _fallback(answer: str, verdict, doc_rule: RequireSearch | None) -> str:
        """다시 써도 검사에 걸린 답. 숫자만 틀렸으면 그 문장만 빼고, 아니면 고정 문구."""
        if verdict.completion:
            return "요청한 작업을 실제로 실행하지 못했습니다. 아직 보내거나 저장한 것이 없으니 다시 요청해 주세요."
        if verdict.citations:
            return doc_rule.unavailable if doc_rule else "조회 결과에서 근거를 확인하지 못해 답할 수 없습니다."
        kept = drop_sentences_with(answer, verdict.numbers)
        return kept or "조회 결과와 맞지 않는 숫자가 있어 답을 드릴 수 없습니다. 다시 요청해 주세요."

    async def _stop_denied(
        self, messages: list[Message], denial: Denial, done: list[DoneTool], llm_calls: int
    ) -> RunResult:
        """실행하지 않은 작업의 성공을 생성하지 않도록 서버가 최종 안내를 확정한다."""
        final = Message("assistant", template_reply(denial, done))
        messages.append(final)
        return RunResult(final, messages, llm_calls, "denied", denial)

    async def execute_tool(self, call: ToolCall) -> _Outcome:
        """화면에서 확정한 호출도 모델 호출과 같은 권한·승인·감사 경로로 실행한다."""
        return await self._handle_tool_call(call)

    async def _handle_tool_call(self, call: ToolCall) -> _Outcome:
        def tool_msg(content: str) -> Message:
            return Message(role="tool", content=content, tool_call_id=call.call_id)

        tool = await self.tools.get(call.tool_name)
        if tool is None:
            return _Outcome(tool_msg(f"error: unknown tool '{call.tool_name}'"))

        result = await self.checker.check(call)
        spec = tool.spec

        approval: ApprovalOutcome | None = None
        if result.decision is Decision.ASK:
            approval = await self.approver.request_approval(call, result)
            allowed = approval == "approved"
        else:
            allowed = result.decision is Decision.ALLOW

        if not allowed:
            await self.audit.log(AuditRecord(call, result, approval=approval, executed=False))
            hook = getattr(tool, "on_not_approved", None)
            if approval is not None and hook is not None:  # 승인 카드에서 반려·시간 초과 (예: 고객 안내 반려 기록)
                try:
                    await hook(call, approval)
                except Exception:
                    log.warning("반려 처리 실패 (%s)", call.tool_name, exc_info=True)
            kind: DenialKind = "deny" if result.decision is Decision.DENY else (approval or "rejected")
            return _Outcome(
                tool_msg(f"error: {_VERDICT[kind]}: {result.reason}"),
                denial=Denial(call.call_id, call.tool_name, kind, result.reason, spec.display_name),
                label=spec.label,
            )

        ok = False
        # 툴이 자기 제한시간(timeout 속성)을 갖고 있으면 그걸 쓴다 (예: 문서 파싱은 오래 걸림). 없으면 하네스 기본값
        seconds = getattr(tool, "timeout", self.tool_timeout)
        limit = asyncio.timeout(seconds)
        try:
            async with limit:
                out = await tool.run(call)
            ok = out.ok
            content = out.content if out.ok else f"error: {out.content}"
        except TimeoutError as e:
            if limit.expired():  # 하네스 제한에 걸림. 툴이 스스로 던진 TimeoutError 와 구분한다
                content = f"error: tool '{call.tool_name}' timed out after {seconds:g}s"
            else:
                content = f"error: tool '{call.tool_name}' raised {type(e).__name__}: {e}"
        except Exception as e:  # 툴이 죽어도 루프는 계속 돈다 (CancelledError 는 BaseException 이라 그대로 전파)
            content = f"error: tool '{call.tool_name}' raised {type(e).__name__}: {e}"

        await self.audit.log(AuditRecord(call, result, approval=approval, executed=True, tool_ok=ok))
        return _Outcome(tool_msg(content or "(empty result)"), ok=ok, label=spec.label)


def create_harness(
    *,
    llm: LLMClient,
    tools: ToolRegistry,
    checker: PermissionChecker,
    approver: ApprovalHandler,
    audit: AuditLogger,
    max_turns: int,
    tool_timeout: float | None = DEFAULT_TOOL_TIMEOUT,
    require_search: RequireSearch | None = None,
    require_review_lookup: bool = False,
    lookup_rules: tuple[LookupRule, ...] | None = None,
    check_answers: bool = True,
    planner: bool = False,
    regulation_mode: bool = False,
    regulation_min_score: float = MIN_SCORE,
    mail_recipients=None,
) -> AgentRunner:
    return AgentRunner(
        llm=llm, tools=tools, checker=checker,
        approver=approver, audit=audit, max_turns=max_turns,
        tool_timeout=tool_timeout, require_search=require_search, require_review_lookup=require_review_lookup,
        lookup_rules=lookup_rules, check_answers=check_answers, planner=planner,
        regulation_mode=regulation_mode, regulation_min_score=regulation_min_score,
        mail_recipients=mail_recipients,
    )


def _hit_count(content: str) -> int:
    """문서 검색 결과의 건수. 모양을 모르면 1 (0건으로 잘못 보지 않게)."""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return 1
    matches = data.get("matches") if isinstance(data, dict) else None
    return len(matches) if isinstance(matches, list) else 1
