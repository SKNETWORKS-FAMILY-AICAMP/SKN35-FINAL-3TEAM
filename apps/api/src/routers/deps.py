from __future__ import annotations

from fastapi import Header, HTTPException, Request

from contracts.permission import Principal
from services.container import AppServices


def get_services(request: Request) -> AppServices:
    return request.app.state.services


def current_user(request: Request, x_user_id: str | None = Header(default=None)) -> Principal:
    """개발용 인증: X-User-Id 로 사용자만 고르고, 속성은 서버가 채운다."""
    if not x_user_id:
        raise HTTPException(401, "X-User-Id 헤더가 필요합니다")
    principal = get_services(request).users.get(x_user_id)
    if principal is None:
        raise HTTPException(401, "알 수 없는 사용자입니다")
    return principal
