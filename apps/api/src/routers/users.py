"""개발용 사용자 API. 로그인(COM-01)이 붙으면 /api/me 만 남기고 목록은 없앤다."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services.container import AppServices

router = APIRouter(tags=["users"])


class UserOut(BaseModel):
    user_id: str
    roles: list[str]
    attributes: dict[str, Any]


def to_out(p: Principal) -> UserOut:
    return UserOut(user_id=p.user_id, roles=list(p.roles), attributes=dict(p.attributes))


@router.get("/api/dev/users", response_model=list[UserOut])
async def list_dev_users(svc: AppServices = Depends(get_services)) -> list[UserOut]:
    """화면의 사용자 선택용 (개발 전용, 인증 없음)."""
    return [to_out(svc.users.get(uid)) for uid in svc.users.ids()]


@router.get("/api/me", response_model=UserOut)
async def me(user: Principal = Depends(current_user)) -> UserOut:
    return to_out(user)
