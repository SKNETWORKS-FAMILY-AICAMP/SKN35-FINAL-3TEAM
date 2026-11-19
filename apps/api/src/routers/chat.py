from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from openai import APIConnectionError, APIError, APITimeoutError
from pydantic import BaseModel, Field

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services import agents as agent_rules
from services.agents import Agent
from services.chat import send_message
from services.container import AppServices

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    # Swagger 예시에 session_id 를 넣지 않는다 ("string" 이 그대로 가면 404)
    model_config = {"json_schema_extra": {"examples": [{"content": "123 곱하기 45 계산해줘"}]}}

    content: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None  # 없으면 새 세션
    agent_id: str | None = None  # 새 세션을 열 때 쓸 에이전트 (없으면 기본 에이전트). 이어 쓸 때는 무시


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


def usable_agent(svc: AppServices, user: Principal, agent_id: str | None, existing: bool = False) -> Agent | None:
    """채팅에 쓸 에이전트. 매 요청마다 다시 검사한다 (그사이 비활성화·공유 해제될 수 있다)."""
    if agent_id is None:
        return None
    agent = svc.agents.get(agent_id)
    if agent is None or not agent_rules.can_view(user, agent):
        if existing:
            raise HTTPException(409, "이 대화의 에이전트가 삭제됐거나 더 이상 쓸 수 없습니다")
        raise HTTPException(404, "에이전트를 찾을 수 없습니다")
    if not agent_rules.can_use(user, agent):
        raise HTTPException(409, "비활성화된 에이전트입니다")
    return agent


@router.post("/messages", response_model=ChatResponse)
async def post_message(
    req: ChatRequest,
    user: Principal = Depends(current_user),
    svc: AppServices = Depends(get_services),
) -> ChatResponse:
    if req.session_id is None:
        agent = usable_agent(svc, user, req.agent_id)
        session = svc.sessions.create(user.user_id, agent.agent_id if agent else None)
    else:
        session = svc.sessions.get_owned(req.session_id, user.user_id)
        if session is None:
            raise HTTPException(404, "세션을 찾을 수 없습니다")
        agent = usable_agent(svc, user, session.agent_id, existing=True)
    try:
        turn = await send_message(svc, user, session, req.content, agent)
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
