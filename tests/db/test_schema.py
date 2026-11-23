"""실제 DB 확인 — `alembic upgrade head` 를 마친 DB(.env 의 DATABASE_URL)가 켜져 있을 때만 돈다. 없으면 건너뜀.

각 테스트는 트랜잭션 안에서 하고 되돌린다 (DB 에 아무것도 남기지 않음).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from db.models import Base

ROOT = Path(__file__).resolve().parents[2]


def _url() -> str | None:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip() or None
    return None


@pytest_asyncio.fixture
async def conn():
    url = _url()
    if not url:
        pytest.skip("DATABASE_URL 없음")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as c:
            if not await c.scalar(text("SELECT to_regclass('alembic_version') IS NOT NULL")):
                pytest.skip("마이그레이션 전 DB")
            await c.rollback()          # 위 확인 쿼리가 연 트랜잭션을 닫고, 테스트용 트랜잭션을 새로 연다
            tx = await c.begin()
            yield c
            await tx.rollback()
    except (OSError, DBAPIError) as e:   # DB 가 꺼져 있음
        pytest.skip(f"DB 에 붙을 수 없음: {type(e).__name__}")
    finally:
        await engine.dispose()


async def test_models_match_database_columns(conn):
    """ORM 모델과 실제 DB 의 표·칸 이름이 같다 (마이그레이션만 고치고 모델을 잊는 일 방지)."""
    def read(sync_conn):
        insp = inspect(sync_conn)
        return {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names() if t != "alembic_version"}
    db = await conn.run_sync(read)
    models = {t.name: {c.name for c in t.columns} for t in Base.metadata.sorted_tables}
    assert set(db) == set(models), (set(db) ^ set(models))
    diff = {t: db[t] ^ models[t] for t in db if db[t] != models[t]}
    assert not diff, diff
    assert len(db) == 25  # 팀 ERD 24 + [0007] login_session


async def _user(conn, grade=2) -> int:
    dept = await conn.scalar(text("INSERT INTO dept (name) VALUES ('여신심사팀') RETURNING id"))
    return await conn.scalar(text(
        "INSERT INTO users (login_id, name, dept_id, security_grade) VALUES (:l, 'A대리', :d, :g) RETURNING id"),
        {"l": f"t-{uuid.uuid4().hex[:8]}", "d": dept, "g": grade})


async def test_message_insert_touches_session(conn):
    uid = await _user(conn)
    sid = await conn.scalar(text(
        "INSERT INTO chat_session (user_id, updated_at) VALUES (:u, now() - interval '1 day') RETURNING id"), {"u": uid})
    before = await conn.scalar(text("SELECT updated_at FROM chat_session WHERE id = :s"), {"s": sid})
    await conn.execute(text("INSERT INTO message (session_id, seq, role, content) VALUES (:s, 1, 'user', '안녕')"),
                       {"s": sid})
    after = await conn.scalar(text("SELECT updated_at FROM chat_session WHERE id = :s"), {"s": sid})
    assert after > before


async def test_security_grade_limited_to_1_to_5(conn):
    with pytest.raises(DBAPIError):
        async with conn.begin_nested():
            await _user(conn, grade=6)


@pytest.mark.parametrize("stmt", ["UPDATE audit_log SET reason = '바꿈' WHERE id = :i",
                                  "DELETE FROM audit_log WHERE id = :i",
                                  "TRUNCATE audit_log"])
async def test_audit_log_is_append_only(conn, stmt):
    uid = await _user(conn)
    aid = await conn.scalar(text(
        "INSERT INTO audit_log (event_type, user_id, target_type, target_id) VALUES ('doc_register', :u, 'doc', 1) "
        "RETURNING id"), {"u": uid})
    with pytest.raises(DBAPIError, match="추가만"):
        async with conn.begin_nested():
            await conn.execute(text(stmt), {"i": aid})


async def test_pgvector_column_works(conn):
    dist = await conn.scalar(text("SELECT '[1,2,3]'::vector <=> '[1,2,4]'::vector"))
    assert 0 < dist < 0.1
