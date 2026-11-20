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
from harness.denial import DoneTool, template_reply, wrapup_instruction
from harness.grounding import RequireSearch

log = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT = 30.0  # HARNESS_TOOL_TIMEOUT_SECONDS (SYS-10)
_VERDICT: dict[str, str] = {  # LLM 에게 돌려주는 거부 메시지 머리말
    "deny": "permission denied",
    "rejected": "permission not approved (user rejected)",
    "timeout": "permission not approved (approval timed out)",
}
SKIPPED_CONTENT = "error: not executed: 앞선 도구 호출이 권한 문제로 거부되어 실행하지 않았습니다"


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
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.checker = checker
        self.approver = approver
        self.audit = audit
        self.max_turns = max_turns
        self.tool_timeout = tool_timeout  # None 이면 제한 없음
        self.require_search = require_search  # 문서 질문이면 검색 강제 (harness/grounding.py)

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

        done: list[DoneTool] = []  # 이번 run 에서 성공한 툴 실행 (거부 시 마무리 답변에 씀)
        rs = self.require_search
        search_spec = next((s for s in specs if rs and s.name == rs.search_tool), None)
        grounded = False  # 이번 run 에서 문서 툴이 성공했는가
        force_search = False  # 다음 호출은 검색 강제
        forced = False  # run 당 한 번만

        for turn in range(1, self.max_turns + 1):
            if force_search:
                force_search = False
                reply = rs.forced_call(await self._ask_search(messages, rs, search_spec), user_input)
            else:
                reply = (await self.llm.chat(list(messages), specs)).message

            if not reply.tool_calls:
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
                outcome = await self._handle_tool_call(call)
                messages.append(outcome.message)
                if outcome.ok:
                    done.append(DoneTool(outcome.label, outcome.message.content))
                    grounded = grounded or bool(rs and call.tool_name in rs.grounded_tools)
                denial = outcome.denial

            if denial is not None:
                return await self._stop_denied(messages, denial, done, llm_calls=turn)

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
        """규칙 8-c: 성공한 툴이 있으면 tools=[] 로 마무리 답변 1회, 아니면 고정 문구."""
        text = ""
        if done:
            llm_calls += 1
            prompt = Message("user", wrapup_instruction(denial, done))  # 기록에는 남기지 않는다
            try:
                text = (await self.llm.chat([*messages, prompt], [])).message.content.strip()
            except Exception:  # 마무리 답변 실패는 고정 문구로 대신한다
                log.warning("거부 후 마무리 LLM 호출 실패, 고정 문구로 대체", exc_info=True)
        final = Message("assistant", text or template_reply(denial, done))
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
) -> AgentRunner:
    return AgentRunner(
        llm=llm, tools=tools, checker=checker,
        approver=approver, audit=audit, max_turns=max_turns,
        tool_timeout=tool_timeout, require_search=require_search,
    )
