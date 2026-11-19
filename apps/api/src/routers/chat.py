from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from openai import APIConnectionError, APIError, APITimeoutError
from pydantic import BaseModel, Field

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services.chat import send_message
from services.container import AppServices

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None  # 없으면 새 세션


class ToolEventOut(BaseModel):
    call_id: str
    tool_name: str
    display_name: str  # 화면에 보여줄 툴 이름
    arguments: dict[str, Any]
    result: str
    ok: bool


class DenialOut(BaseModel):
    """stop_reason="denied" 일 때 무엇이 왜 막혔는지.

    kind: deny(정책) | rejected(사용자가 거절) | timeout(승인 시간 초과)
    """

    call_id: str
    tool_name: str
    display_name: str
    kind: str
    reason: str


class ChatResponse(BaseModel):
    session_id: str
    run_id: str
    stop_reason: str  # completed | max_turns | denied
    message: str
    tool_events: list[ToolEventOut]
    denial: DenialOut | None = None


class MessageOut(BaseModel):
    role: str
    content: str
    tool_call_id: str | None = None


@router.post("/messages", response_model=ChatResponse)
async def post_message(
    req: ChatRequest,
    user: Principal = Depends(current_user),
    svc: AppServices = Depends(get_services),
) -> ChatResponse:
    if req.session_id is None:
        session = svc.sessions.create(user.user_id)
    else:
        session = svc.sessions.get_owned(req.session_id, user.user_id)
        if session is None:
            raise HTTPException(404, "세션을 찾을 수 없습니다")
    try:
        turn = await send_message(svc, user, session, req.content)
    except (APITimeoutError, APIConnectionError, APIError) as e:  # NFR-06: LLM 장애는 사용자에게 알린다
        log.warning("LLM 호출 실패: %s", e)
        raise HTTPException(502, "LLM 서버 응답이 없습니다. 잠시 후 다시 시도해 주세요.") from e
    return ChatResponse(
        session_id=turn.session_id,
        run_id=turn.run_id,
        stop_reason=turn.stop_reason,
        message=turn.message,
        tool_events=[ToolEventOut(**e.__dict__) for e in turn.tool_events],
        denial=DenialOut(
            call_id=turn.denial.call_id, tool_name=turn.denial.tool_name, display_name=turn.denial.label,
            kind=turn.denial.kind, reason=turn.denial.reason,
        ) if turn.denial else None,
    )


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(
    session_id: str,
    user: Principal = Depends(current_user),
    svc: AppServices = Depends(get_services),
) -> list[MessageOut]:
    session = svc.sessions.get_owned(session_id, user.user_id)
    if session is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    return [
        MessageOut(role=m.role, content=m.content, tool_call_id=m.tool_call_id)
        for m in session.messages
        if m.role != "system"
    ]
