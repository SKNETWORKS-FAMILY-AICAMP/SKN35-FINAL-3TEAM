"""Alembic 실행 환경 (비동기, asyncpg). `alembic upgrade head` 가 이 파일을 거친다.

- 접속 주소: 환경변수 DATABASE_URL, 없으면 저장소 맨 위 .env 에서 읽는다.
- target_metadata: apps/api/src/db/models.py — `alembic revision --autogenerate` 가 모델과 DB 를 비교할 때 쓴다.
  단 트리거·CHECK·부분 인덱스는 자동 비교가 안 되므로 변경 파일에 직접 쓴다.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))  # db.models 를 읽으려고

from db.models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def _load_dotenv() -> None:
    """.env 의 KEY=VALUE 를 환경변수로 (이미 있는 값은 덮지 않음). 별도 패키지 없이 최소한만."""
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _url() -> str:
    _load_dotenv()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL 이 없습니다 (.env 확인)")
    return url


def run_migrations_offline() -> None:
    """DB 에 붙지 않고 SQL 만 출력 (`alembic upgrade head --sql`)."""
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _url()
    engine = async_engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
