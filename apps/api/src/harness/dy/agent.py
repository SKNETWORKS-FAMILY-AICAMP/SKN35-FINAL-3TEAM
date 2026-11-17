from __future__ import annotations

import os
from dataclasses import dataclass

from contracts.context import RunContext
from contracts.harness import LLMClient, LLMToolCall, Message, ToolRegistry, ToolResult, RunResult
from contracts.permission import (
    ApprovalHandler,
    AuditLogger,
    AuditRecord,
    Decision,
    PermissionChecker,
    Principal,
    ToolCall,
)
from apps.api.src.harness.dy.session import Session

DEFAULT_MAX_TURNS = int(os.environ.get("HARNESS_MAX_TURNS", "25"))


class MaxTurnsExceeded(RuntimeError):
    pass


@dataclass
class Agent:
    llm: LLMClient
    registry: ToolRegistry
    checker: PermissionChecker
    approver: ApprovalHandler
    audit: AuditLogger
    system_prompt: str = ""
    max_turns: int = DEFAULT_MAX_TURNS

    def run(self, ctx: RunContext, session: Session, user_input: str) -> RunResult:
        """사용자 입력 1건을 처리하고 최종 답변 텍스트를 돌려준다. (= 1 run)"""
        if self.system_prompt and not session.get_history():
            session.add_message(Message(role="system", content=self.system_prompt))
        session.add_message(Message(role="user", content=user_input))

        specs = self.registry.specs()

        for _ in range(self.max_turns):
            reply = self.llm.chat(session.get_history(), specs).message
            session.add_message(reply)  # tool_calls 가 든 assistant 메시지를 결과보다 먼저 저장

            if not reply.tool_calls:
                return RunResult(status="completed", message=reply.content)

            for index, llm_call in enumerate(reply.tool_calls):
                result = self._run_tool_call(ctx, llm_call)
                session.add_message(Message(role="tool", content=result.content, tool_call_id=result.call_id))

                if result.blocked:
                    for pending_call in reply.tool_calls[index + 1:]:
                        session.add_message(
                            Message(
                            role="tool",
                            content="앞선 도구 호출이 차단되어 실행하지 않았습니다.",
                            tool_call_id=pending_call.call_id,
                        )
                    )

                    stopped_message = f"작업을 중단했습니다. {result.content}"
                    session.add_message(Message(role="assistant", content=stopped_message))
                    return RunResult(status="blocked", message=stopped_message)

        raise MaxTurnsExceeded(f"Agent loop exceeded {self.max_turns} turns")

    # ------------------------------------------------------------------
    def _run_tool_call(self, ctx: RunContext, llm_call: LLMToolCall) -> ToolResult:
        """권한 확인 → (승인) → 실행 → 감사 기록. 실패는 예외가 아니라 ToolResult(ok=False)로 돌려준다."""
        tool = self.registry.get(llm_call.tool_name)
        if tool is None:
            return ToolResult(llm_call.call_id, False, f"Unknown tool: {llm_call.tool_name}")

        # RunContext(내부 신분증) → ToolCall(권한 계약이 아는 형태) 변환은 여기 한 곳에서만 한다.
        call = ToolCall(
            call_id=llm_call.call_id,
            tool_name=llm_call.tool_name,
            arguments=llm_call.arguments,
            principal=Principal(user_id=ctx.user_id, roles=ctx.roles),
            session_id=ctx.session_id,
        )

        perm = self.checker.check(call)
        approved: bool | None = None
        executed = False

        if perm.decision == Decision.DENY:
            result = ToolResult(call.call_id, False, f"Permission denied: {perm.reason}", blocked=True)
        else:
            if perm.decision == Decision.ASK:
                approved = self.approver.request_approval(call, perm)
            if perm.decision == Decision.ASK and not approved:
                result = ToolResult(call.call_id, False, f"Rejected by user: {perm.reason}", blocked=True)
            else:
                executed = True
                try:
                    result = tool.run(call)
                except Exception as err:  # noqa: BLE001 - 툴 실패는 모델이 읽고 복구하도록 텍스트로 돌려준다
                    result = ToolResult(call.call_id, False, f"Tool error ({call.tool_name}): {err}")

        self.audit.log(AuditRecord(call=call, result=perm, approved=approved, executed=executed, tool_ok=result.ok if executed else None,))
        return result
