"""COM-07: ASK 판정 승인/거절.

채팅 요청은 승인 응답이 올 때까지 기다린다. 화면은 그동안 GET /api/approvals 로
대기 중인 요청을 가져와 보여주고, 사용자가 누르면 POST 로 응답한다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services.container import AppServices

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class ApprovalOut(BaseModel):
    call_id: str
    session_id: str
    tool_name: str
    display_name: str
    arguments: dict[str, Any]
    reason: str


class ApprovalAnswer(BaseModel):
    approved: bool


@router.get("", response_model=list[ApprovalOut])
async def list_pending(
    user: Principal = Depends(current_user),
    svc: AppServices = Depends(get_services),
) -> list[ApprovalOut]:
    labels = {s.name: s.label for s in await svc.tools.specs()}
    return [
        ApprovalOut(
            call_id=p.call.call_id,
            session_id=p.call.session_id,
            tool_name=p.call.tool_name,
            display_name=labels.get(p.call.tool_name, p.call.tool_name),
            arguments=p.call.arguments,
            reason=p.result.reason,
        )
        for p in svc.approvals.pending()
        if p.call.principal.user_id == user.user_id
    ]


@router.post("/{call_id}", status_code=204)
async def answer(
    call_id: str,
    body: ApprovalAnswer,
    user: Principal = Depends(current_user),
    svc: AppServices = Depends(get_services),
) -> None:
    if not svc.approvals.resolve(call_id, body.approved, user_id=user.user_id):
        raise HTTPException(404, "대기 중인 승인 요청이 없습니다")
