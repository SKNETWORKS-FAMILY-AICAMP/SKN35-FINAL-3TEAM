"""로그인 API (COM-01). AUTH_MODE=session 일 때만 동작한다. 흐름은 services/auth.py 참고."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from routers.deps import get_services
from services.auth import LoginError
from services.container import AppServices

router = APIRouter(tags=["auth"])


class ModeOut(BaseModel):
    mode: Literal["session", "dev"]


class LoginIn(BaseModel):
    login_id: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class LoginOut(BaseModel):
    user_id: str
    name: str | None
    roles: list[str]
    attributes: dict[str, Any]


@router.get("/api/auth/mode", response_model=ModeOut)
async def mode(svc: AppServices = Depends(get_services)) -> ModeOut:
    """화면이 로그인 화면을 띄울지(session), 개발용 사용자 선택을 띄울지(dev) 정한다."""
    return ModeOut(mode="session" if svc.auth is not None else "dev")


@router.post("/api/auth/login", response_model=LoginOut)
async def login(body: LoginIn, request: Request, response: Response,
                svc: AppServices = Depends(get_services)) -> LoginOut:
    if svc.auth is None:
        raise HTTPException(404, "개발 모드(AUTH_MODE=dev)에서는 로그인 대신 X-User-Id 로 사용자를 고릅니다.")
    try:
        token, p = await svc.auth.login(
            body.login_id, body.password,
            client_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except LoginError as e:
        raise HTTPException(e.status, str(e)) from None
    s = svc.auth.settings
    response.set_cookie(
        s.cookie_name, token, max_age=int(s.session_hours * 3600), httponly=True,
        secure=s.cookie_secure, samesite="lax", path="/",
    )
    return LoginOut(user_id=p.user_id, name=p.attributes.get("name"), roles=list(p.roles),
                    attributes={k: v for k, v in p.attributes.items() if k != "user_pk"})


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request, svc: AppServices = Depends(get_services)) -> Response:
    response = Response(status_code=204)
    if svc.auth is not None:
        await svc.auth.logout(request.cookies.get(svc.auth.cookie_name, ""))
        response.delete_cookie(svc.auth.cookie_name, path="/")
    return response
