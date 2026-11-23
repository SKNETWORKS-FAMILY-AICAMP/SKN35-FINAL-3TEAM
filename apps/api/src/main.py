"""FastAPI 앱.

실행 (저장소 루트에서):
    uv run python -m uvicorn main:app --app-dir apps/api/src --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from routers import agents, approvals, auth, chat, templates, users, uploads
from services.container import AppServices, build_from_env
from services.db_stores import UnknownUser


def create_app(build: Callable[[], AppServices] = build_from_env) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.services = build()
        if app.state.services.auth is not None:  # 로그인 모드: DB 연결 확인 + 사용자 목록 읽기
            await app.state.services.auth.startup()
        yield

    app = FastAPI(title="Team Agent API", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(approvals.router)
    app.include_router(agents.router)
    app.include_router(users.router)
    app.include_router(uploads.router)
    app.include_router(templates.router)

    @app.exception_handler(UnknownUser)
    async def unknown_user(_: Request, error: UnknownUser) -> JSONResponse:
        # STORE=db 인데 요청한 사람이 DB users 표에 없음 (개발 모드에서 seed_users.py 를 안 돌린 경우)
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
