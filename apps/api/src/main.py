"""FastAPI 앱.

실행 (저장소 루트에서):
    uv run python -m uvicorn main:app --app-dir apps/api/src --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from routers import agents, approvals, chat, documents, inbox, templates, users, uploads
from services.container import AppServices, build_from_env


def create_app(build: Callable[[], AppServices] = build_from_env) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.services = build()
        yield

    app = FastAPI(title="Team Agent API", lifespan=lifespan)
    app.include_router(chat.router)
    app.include_router(approvals.router)
    app.include_router(agents.router)
    app.include_router(users.router)
    app.include_router(uploads.router)
    app.include_router(templates.router)
    app.include_router(inbox.router)
    app.include_router(documents.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
