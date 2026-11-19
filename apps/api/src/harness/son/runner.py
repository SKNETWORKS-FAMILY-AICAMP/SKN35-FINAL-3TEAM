"""에이전트 루프: LLM ↔ 툴 왕복을 max_turns 안에서 돌린다.

규칙은 contracts/harness.py 의 '에이전트 루프' 주석을 따른다.
"""

from __future__ import annotations

from contracts.harness import (
    LLMClient,
    LLMToolCall,
    Message,
    RunResult,
    ToolRegistry,
)
from contracts.permission import (
    ApprovalHandler,
    AuditLogger,
    AuditRecord,
    Decision,
    PermissionChecker,
    Principal,
    ToolCall,
)


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
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.checker = checker
        self.approver = approver
        self.audit = audit
        self.max_turns = max_turns

    async def run(
        self,
        user_input: str,
        principal: Principal,
        session_id: str,
        history: list[Message] | None = None,
    ) -> RunResult:
        messages: list[Message] = list(history or [])
        messages.append(Message(role="user", content=user_input))
        specs = await self.tools.specs()

        for turn in range(1, self.max_turns + 1):
            reply = (await self.llm.chat(list(messages), specs)).message
            messages.append(reply)

            if not reply.tool_calls:
                return RunResult(reply, messages, turn, "completed")
            if turn == self.max_turns:
                break

            for llm_call in reply.tool_calls:
                messages.append(await self._handle_tool_call(llm_call, principal, session_id))

        return RunResult(None, messages, self.max_turns, "max_turns")

    async def _handle_tool_call(
        self, llm_call: LLMToolCall, principal: Principal, session_id: str
    ) -> Message:
        def tool_msg(content: str) -> Message:
            return Message(role="tool", content=content, tool_call_id=llm_call.call_id)

        tool = await self.tools.get(llm_call.tool_name)
        if tool is None:
            return tool_msg(f"error: unknown tool '{llm_call.tool_name}'")

        call = ToolCall(
            call_id=llm_call.call_id,
            tool_name=llm_call.tool_name,
            arguments=llm_call.arguments,
            principal=principal,
            session_id=session_id,
        )
        result = await self.checker.check(call)

        approved: bool | None = None
        if result.decision is Decision.ASK:
            approved = await self.approver.request_approval(call, result)
            allowed = approved
        else:
            allowed = result.decision is Decision.ALLOW

        if not allowed:
            await self.audit.log(AuditRecord(call, result, approved=approved, executed=False))
            verdict = "denied" if result.decision is Decision.DENY else "not approved"
            return tool_msg(f"error: permission {verdict}: {result.reason}")

        try:
            out = await tool.run(call)
            content = out.content if out.ok else f"error: {out.content}"
        except Exception as e:  # 툴이 죽어도 루프는 계속 돈다 (CancelledError 는 BaseException 이라 그대로 전파)
            content = f"error: tool '{call.tool_name}' raised {type(e).__name__}: {e}"

        await self.audit.log(AuditRecord(call, result, approved=approved, executed=True))
        return tool_msg(content or "(empty result)")


def create_harness(
    *,
    llm: LLMClient,
    tools: ToolRegistry,
    checker: PermissionChecker,
    approver: ApprovalHandler,
    audit: AuditLogger,
    max_turns: int,
) -> AgentRunner:
    return AgentRunner(
        llm=llm, tools=tools, checker=checker,
        approver=approver, audit=audit, max_turns=max_turns,
    )
