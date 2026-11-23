"""DB 연결: 비동기 엔진 하나를 앱 전체가 같이 쓰고, 요청마다 세션을 하나씩 연다.

드라이버는 asyncpg (비동기 FastAPI 와 맞고, Windows 기본 이벤트 루프에서도 추가 설정 없이 동작).
접속 주소는 .env 의 DATABASE_URL — 예: postgresql+asyncpg://postgres:비밀번호@127.0.0.1:5432/agent_platform
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL 이 없습니다. .env.example 을 참고해 .env 에 넣으세요.")
    return url


def get_engine() -> AsyncEngine:
    """처음 부를 때 한 번 만든다 (연결 풀 공유)."""
    global _engine, _sessions
    if _engine is None:
        # pool_pre_ping: DB 를 껐다 켠 뒤 끊긴 연결을 쓰지 않도록 쓰기 전에 확인
        _engine = create_async_engine(database_url(), pool_pre_ping=True)
        # expire_on_commit=False: 커밋 뒤에도 객체 값을 읽을 수 있게 (비동기에선 지연 로딩이 안 됨)
        _sessions = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 의존성: `session: AsyncSession = Depends(get_session)`. 요청이 끝나면 닫는다.
    커밋은 쓰는 쪽이 명시적으로 한다 (오류가 나면 커밋 안 된 변경은 버려진다)."""
    get_engine()
    assert _sessions is not None
    async with _sessions() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """요청 밖(툴·스크립트)에서 세션 하나를 열고 닫는다. 커밋은 쓰는 쪽이 한다."""
    get_engine()
    assert _sessions is not None
    async with _sessions() as session:
        yield session


async def dispose_engine() -> None:
    """앱 종료 때 연결 풀 정리."""
    global _engine, _sessions
    if _engine is not None:
        await _engine.dispose()
    _engine = _sessions = None
