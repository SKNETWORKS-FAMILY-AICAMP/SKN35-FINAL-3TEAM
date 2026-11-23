from __future__ import annotations

import logging
import asyncio
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from openai import APIConnectionError, APIError, APITimeoutError
from pydantic import BaseModel, Field, model_validator

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services import agents as agent_rules
from services.agents import Agent
from services.chat import past_turns, send_message
from services.container import AppServices
from services.uploads import MAX_ATTACHMENTS, attachment_context

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    # Swagger 예시에 session_id 를 넣지 않는다 ("string" 이 그대로 가면 404)
    model_config = {"json_schema_extra": {"examples": [{"content": "123 곱하기 45 계산해줘"}]}}

    content: str = Field(default="", max_length=4000)
    session_id: str | None = None  # 없으면 새 세션
    agent_id: str | None = None  # 새 세션을 열 때 쓸 에이전트 (없으면 기본 에이전트). 이어 쓸 때는 무시
    file_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)

    @model_validator(mode="after")
    def require_message(self):
        if not self.content.strip() and not self.file_ids:
            raise ValueError("메시지 또는 첨부 파일이 필요합니다.")
        if len(set(self.file_ids)) != len(self.file_ids):
            raise ValueError("같은 파일을 중복 첨부할 수 없습니다.")
        return self


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


class SourceOut(BaseModel):
    title: str  # 문서명
    location: str  # 조항·페이지
    opened: bool  # 원문을 열어 읽었는지
    previewed: bool = False
    partial: bool = False


class ChatResponse(BaseModel):
    session_id: str
    run_id: str
    stop_reason: str  # completed | max_turns | denied
    message: str
    tool_events: list[ToolEventOut]
    denial: DenialOut | None = None
    sources: list[SourceOut] = []  # COM-04: 이번 답에 쓰인 문서


class SessionSummaryOut(BaseModel):
    """최근 대화 목록 한 줄"""

    session_id: str
    agent_id: str | None  # None = 기본 에이전트, "removed" = 지워진 에이전트
    title: str
    updated_at: datetime | None = None


class AttachmentOut(BaseModel):
    file_id: str
    filename: str


class PastTurnOut(BaseModel):
    """저장된 대화를 다시 열 때 질문 1개와 그 답"""

    question: str
    attachments: list[AttachmentOut] = []
    message: str  # 끝까지 못 간 요청이면 빈 문자열
    tool_events: list[ToolEventOut]
    sources: list[SourceOut] = []
    denial: DenialOut | None = None  # 권한 거부로 멈춘 요청


def denial_out(denial) -> DenialOut | None:
    if denial is None:
        return None
    return DenialOut(call_id=denial.call_id, tool_name=denial.tool_name, display_name=denial.label,
                     kind=denial.kind, reason=denial.reason)


class MessageOut(BaseModel):
    role: str
    content: str
    tool_call_id: str | None = None


async def usable_agent(svc: AppServices, user: Principal, agent_id: str | None, existing: bool = False) -> Agent | None:
    """채팅에 쓸 에이전트. 매 요청마다 다시 검사한다 (그사이 비활성화·공유 해제될 수 있다)."""
    if agent_id is None:
        return None
    agent = await svc.agents.get(agent_id)
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
        agent = await usable_agent(svc, user, req.agent_id)
        session = None
    else:
        session = await svc.sessions.get_owned(req.session_id, user.user_id)
        if session is None:
            raise HTTPException(404, "세션을 찾을 수 없습니다")
        agent = await usable_agent(svc, user, session.agent_id, existing=True)
    attachments = []
    if req.file_ids:
        if await svc.tools.get("parse_doc") is None or (agent is not None and "parse_doc" not in agent.tools):
            raise HTTPException(409, "이 에이전트에 업로드 문서 읽기 도구를 추가해 주세요.")
        try:
            for file_id in req.file_ids:
                item = await asyncio.to_thread(svc.uploads.get_owned, file_id, user.user_id)
                if item is None:
                    raise HTTPException(404, "열람 가능한 첨부 파일을 찾을 수 없습니다.")
                attachments.append(item)
        except (OSError, sqlite3.Error) as error:
            raise HTTPException(503, "첨부 파일 정보를 읽을 수 없습니다.") from error
    if session is None:
        session = await svc.sessions.create(user.user_id, agent.agent_id if agent else None)
    try:
        turn = await send_message(svc, user, session, attachment_context(req.content, attachments), agent,
                                  attachments=attachments)
    except (APITimeoutError, APIConnectionError, APIError) as e:  # NFR-06: LLM 장애는 사용자에게 알린다
        log.warning("LLM 호출 실패: %s", e)
        raise HTTPException(502, "LLM 서버 응답이 없습니다. 잠시 후 다시 시도해 주세요.") from e
    except (OSError, sqlite3.Error) as e:
        raise HTTPException(503, "첨부 파일 정보를 읽을 수 없습니다.") from e
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
        sources=[SourceOut(**s.__dict__) for s in turn.sources],
    )


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(
    session_id: str,
    user: Principal = Depends(current_user),
    svc: AppServices = Depends(get_services),
) -> list[MessageOut]:
    session = await svc.sessions.get_owned(session_id, user.user_id)
    if session is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    return [
        MessageOut(role=m.role, content=m.content, tool_call_id=m.tool_call_id)
        for m in session.messages
        if m.role != "system"
    ]


@router.get("/sessions", response_model=list[SessionSummaryOut])
async def list_sessions(
    user: Principal = Depends(current_user),
    svc: AppServices = Depends(get_services),
) -> list[SessionSummaryOut]:
    """내 대화 목록 (최근 것부터). 서버를 다시 켜도 STORE=db 면 남아 있다."""
    return [SessionSummaryOut(**s.__dict__) for s in await svc.sessions.summaries(user.user_id)]


@router.get("/sessions/{session_id}/turns", response_model=list[PastTurnOut])
async def get_turns(
    session_id: str,
    user: Principal = Depends(current_user),
    svc: AppServices = Depends(get_services),
) -> list[PastTurnOut]:
    """저장된 대화를 화면에 다시 그릴 수 있게 질문·답·툴 실행·참고 문서로 묶어 돌려준다."""
    session = await svc.sessions.get_owned(session_id, user.user_id)
    if session is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    labels = {spec.name: spec.label for spec in await svc.tools.specs()}
    return [PastTurnOut(
        question=t.question, attachments=[AttachmentOut(**f) for f in t.attachments], message=t.message,
        tool_events=[ToolEventOut(**e.__dict__) for e in t.tool_events],
        sources=[SourceOut(**x.__dict__) for x in t.sources], denial=denial_out(t.denial),
    ) for t in past_turns(session.messages, labels, session.denials)]
