from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from contracts.permission import Principal
from services.container import AppServices

log = logging.getLogger("uvicorn.error")

# Swagger 오른쪽 위 Authorize 버튼에 사용자 ID 를 한 번 넣으면 모든 요청에 X-User-Id 헤더로 붙는다.
dev_user_header = APIKeyHeader(
    name="X-User-Id",
    auto_error=False,
    scheme_name="개발용 사용자",
    description="개발용 로그인: config/dev_users.yaml 의 user_id (kim, lee, park, admin)",
)


def get_services(request: Request) -> AppServices:
    return request.app.state.services


def current_user(request: Request, x_user_id: str | None = Depends(dev_user_header)) -> Principal:
    """개발용 인증: X-User-Id 로 사용자만 고르고, 속성은 서버가 채운다."""
    users = get_services(request).users
    known = ", ".join(users.ids())
    if not x_user_id:
        log.warning("401: X-User-Id 헤더 없음 (%s %s)", request.method, request.url.path)
        raise HTTPException(401, f"X-User-Id 헤더가 필요합니다. Swagger 는 오른쪽 위 Authorize 에 사용자 ID 를 넣으세요 ({known})")
    principal = users.get(x_user_id.strip())
    if principal is None:
        log.warning("401: 모르는 사용자 %r (%s %s)", x_user_id, request.method, request.url.path)
        raise HTTPException(401, f"알 수 없는 사용자입니다: '{x_user_id}'. 사용할 수 있는 사용자: {known}")
    return principal
