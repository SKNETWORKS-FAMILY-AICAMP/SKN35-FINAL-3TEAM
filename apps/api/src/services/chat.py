"""채팅 1건 처리: 세션 기록을 붙여 하네스를 돌리고, 화면에 보여줄 툴 실행 내역을 만든다."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from contracts.harness import Denial, Message, RunResult
from contracts.permission import Principal
from harness.son import create_harness
from services.container import AppServices
from services.sessions import ChatSession


@dataclass(frozen=True)
class ToolEvent:
    """COM-03: 에이전트가 실행한 툴과 결과."""

    call_id: str
    tool_name: str
    display_name: str  # ToolSpec.label
    arguments: dict[str, Any]
    result: str
    ok: bool


@dataclass(frozen=True)
class ChatTurn:
    session_id: str
    run_id: str
    stop_reason: str
    message: str
    tool_events: list[ToolEvent]
    denial: Denial | None = None


def tool_events(new_messages: list[Message], labels: dict[str, str]) -> list[ToolEvent]:
    results = {m.tool_call_id: m.content for m in new_messages if m.role == "tool"}
    events = []
    for m in new_messages:
        for tc in m.tool_calls:
            content = results.get(tc.call_id, "")
            label = labels.get(tc.tool_name, tc.tool_name)
            events.append(ToolEvent(tc.call_id, tc.tool_name, label, tc.arguments, content, not content.startswith("error:")))
    return events


async def send_message(svc: AppServices, principal: Principal, session: ChatSession, content: str) -> ChatTurn:
    harness = create_harness(
        llm=svc.llm, tools=svc.tools, checker=svc.checker, approver=svc.approvals,
        audit=svc.audit, max_turns=svc.max_turns, tool_timeout=svc.tool_timeout,
    )
    run_id = str(uuid.uuid4())
    async with session.lock:
        history = session.messages or [Message(role="system", content=svc.system_prompt)]
        result: RunResult = await harness.run(
            content, principal, session.session_id, history,
            agent_id=session.agent_id, run_id=run_id,
        )
        new = result.messages[len(history):]
        session.messages = list(result.messages)

    if result.final_message is not None:
        text = result.final_message.content
    else:
        text = "요청을 처리하는 데 단계가 너무 많아 중단했습니다. 요청을 나눠서 다시 시도해 주세요."
    labels = {s.name: s.label for s in await svc.tools.specs()}
    return ChatTurn(session.session_id, run_id, result.stop_reason, text, tool_events(new, labels), result.denial)
