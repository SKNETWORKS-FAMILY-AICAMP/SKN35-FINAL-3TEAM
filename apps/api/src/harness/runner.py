"""에이전트 루프: LLM ↔ 툴 왕복을 max_turns 안에서 돌린다.

규칙은 contracts/harness.py 의 '에이전트 루프' 주석을 따른다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from contracts.harness import (
    Denial,
    DenialKind,
    LLMClient,
    Message,
    RunResult,
    RequiredToolLLMClient,
    ToolRegistry,
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
from harness.grounding import RequireSearch
from harness.review_grounding import LOOKUP_TOOL, SUBMISSION_TOOL, ReviewLookupGuard

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
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.checker = checker
        self.approver = approver
        self.audit = audit
        self.max_turns = max_turns
        self.tool_timeout = tool_timeout  # None 이면 제한 없음
        self.require_search = require_search  # 문서 질문이면 검색 강제 (harness/grounding.py)
        self.require_review_lookup = require_review_lookup

    async def run(
        self,
        user_input: str,
        principal: Principal,
        session_id: str,
        history: list[Message] | None = None,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        messages: list[Message] = list(history or [])
        messages.append(Message(role="user", content=user_input))
        specs = await self.tools.specs()
        review = ReviewLookupGuard.from_request(user_input, history or []) if self.require_review_lookup else None
        review_spec = next((spec for spec in specs if spec.name == LOOKUP_TOOL), None)
        submission_spec = next((spec for spec in specs if spec.name == SUBMISSION_TOOL), None)
        if review and review_spec is None:
            final = Message("assistant", review.unavailable(tool_missing=True))
            messages.append(final)
            return RunResult(final, messages, 0, "completed")

        done: list[DoneTool] = []  # 이번 run 에서 성공한 툴 실행 (거부 시 마무리 답변에 씀)
        rs = self.require_search
        search_spec = next((s for s in specs if rs and s.name == rs.search_tool), None)
        grounded = False  # 이번 run 에서 문서 툴이 성공했는가
        force_search = False  # 다음 호출은 검색 강제
        forced = False  # run 당 한 번만
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
            elif force_search:
                force_search = False
                reply = rs.forced_call(await self._ask_search(messages, rs, search_spec), user_input)
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
                # 문서 근거 없이 규정을 답했으면 그 답은 버리고 검색부터 시킨다 (강제 검색 + 답 쓰기 2회가 남아 있을 때만)
                if (search_spec and not grounded and not forced and turn + 2 <= self.max_turns
                        and rs.needed(user_input, reply.content, used_other_tools=bool(done))):
                    forced = force_search = True
                    log.info("문서 근거 없는 답이라 검색을 강제합니다 (run %s)", run_id)
                    continue
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
                    if review:
                        review.record(call.tool_name, call.arguments, outcome.message.content)
                    done.append(DoneTool(outcome.label, outcome.message.content, call.tool_name))
                    grounded = grounded or bool(rs and call.tool_name in rs.grounded_tools)
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

    async def _ask_search(self, messages: list[Message], rs: RequireSearch, search_spec) -> Message | None:
        """검색 툴 하나만 주고 다시 묻는다. 안내 문구는 기록에 남기지 않는다. 실패하면 None (하네스가 직접 검색)."""
        try:
            return (await self.llm.chat([*messages, rs.nudge()], [search_spec])).message
        except Exception:
            log.warning("검색 강제 LLM 호출 실패, 질문으로 직접 검색", exc_info=True)
            return None

    async def _stop_denied(
        self, messages: list[Message], denial: Denial, done: list[DoneTool], llm_calls: int
    ) -> RunResult:
        """실행하지 않은 작업의 성공을 생성하지 않도록 서버가 최종 안내를 확정한다."""
        final = Message("assistant", template_reply(denial, done))
        messages.append(final)
        return RunResult(final, messages, llm_calls, "denied", denial)

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
) -> AgentRunner:
    return AgentRunner(
        llm=llm, tools=tools, checker=checker,
        approver=approver, audit=audit, max_turns=max_turns,
        tool_timeout=tool_timeout, require_search=require_search, require_review_lookup=require_review_lookup,
    )
