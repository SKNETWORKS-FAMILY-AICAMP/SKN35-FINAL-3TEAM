"""PostgreSQL 저장소만의 동작 — users 연결 · 삭제 연쇄 · 모르는 로그인 ID. (GMAIL_TEST_DATABASE_URL 이 있을 때만)"""

import asyncpg
import pytest

from gmail_mcp.models import ChangeKind
from gmail_mcp.store_pg import to_asyncpg_dsn
from test_gmail_store import rec


def test_dsn_accepts_sqlalchemy_style():
    assert to_asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"
    assert to_asyncpg_dsn("postgresql://u@h/d") == "postgresql://u@h/d"


async def test_unknown_login_reads_nothing_and_cannot_write(pg_store):
    assert await pg_store.search("nobody", "", 10) == []
    assert await pg_store.thread("nobody", "t1") == []
    assert await pg_store.get_account_email("nobody") is None
    with pytest.raises(ValueError, match="users 에 없는 로그인 ID"):
        await pg_store.set_account("nobody", "x@example.com")
    with pytest.raises(asyncpg.NotNullViolationError):  # users 에 없는 주인으로는 메일을 쓸 수 없다
        await pg_store.upsert(rec("nobody", "a"))


async def test_mail_moves_to_archive_when_user_is_deleted(pg_store):
    await pg_store.set_account("E1", "e1@example.com")
    await pg_store.upsert(rec("E1", "a", thread="t1", subject="연봉 협의"))
    await pg_store.upsert(rec("E1", "b", thread="t1", subject="Re: 연봉 협의"))
    await pg_store.upsert(rec("E2", "z", thread="t9", subject="다른 직원 메일"))
    await pg_store.save_sync_state("E1", last_sync_end=1.0, window_start=None, window_end=None, checkpoint=None,
                                   job_id=None)
    pool = await pg_store._get_pool()
    async with pool.acquire() as c:
        uid = await c.fetchval("SELECT id FROM users WHERE login_id = 'E1'")
        await c.execute("DELETE FROM users WHERE id = $1", uid)
        # 원래 표에서는 사라진다
        for table in ("mail_account", "mail_message", "mail_sync_state"):
            assert await c.fetchval(f"SELECT count(*) FROM {table} WHERE owner_user_id = $1", uid) == 0
        # 보관 표에는 직원 정보와 함께 남는다
        rows = await c.fetch("SELECT * FROM mail_archive WHERE former_user_id = $1 ORDER BY message_id", uid)
    assert [r["message_id"] for r in rows] == ["a", "b"]
    assert {r["former_login_id"] for r in rows} == {"E1"} and rows[0]["former_name"] == "test-E1"
    assert rows[0]["account_email"] == "e1@example.com" and rows[0]["subject"] == "연봉 협의"
    assert await pg_store.message_ids("E2") == {"z"}  # 다른 직원 메일은 그대로


async def test_archive_is_not_readable_through_normal_queries(pg_store):
    await pg_store.upsert(rec("E1", "a", thread="t1", subject="비밀"))
    pool = await pg_store._get_pool()
    async with pool.acquire() as c:
        await c.execute("DELETE FROM users WHERE login_id = 'E1'")
    # 같은 로그인 ID 를 새 직원이 받아도 퇴사자의 메일은 보이지 않는다
    async with pool.acquire() as c:
        dept = await c.fetchval("SELECT dept_id FROM users WHERE login_id = 'E2'")
        await c.execute("INSERT INTO users (login_id, name, dept_id) VALUES ('E1', '신입', $1)", dept)
    assert await pg_store.search("E1", "", 10) == []
    assert await pg_store.thread("E1", "t1") == []


async def test_admin_archive_list_search_read_purge(pg_store):
    await pg_store.set_account("E2", "e2@example.com")
    await pg_store.upsert(rec("E2", "a", thread="t1", subject="인수인계 문서"))
    await pg_store.upsert(rec("E2", "b", thread="t2", subject="경비 정산"))
    pool = await pg_store._get_pool()
    async with pool.acquire() as c:
        await c.execute("DELETE FROM users WHERE login_id = 'E2'")

    users = [u for u in await pg_store.archive_users() if u["former_login_id"] == "E2"]
    assert len(users) == 1 and users[0]["mails"] == 2 and users[0]["account_email"] == "e2@example.com"
    uid = await pg_store.resolve_archive_user("E2")  # 로그인 ID 로도 찾는다
    assert uid == users[0]["former_user_id"] == await pg_store.resolve_archive_user(str(uid))
    assert [m.message_id for m in await pg_store.archive_search(uid, "인수인계", 10)] == ["a"]
    assert all(m.body == "" for m in await pg_store.archive_search(uid, "", 10))  # 목록에는 본문 없음
    assert [m.message_id for m in await pg_store.archive_thread(uid, "t2")] == ["b"]
    with pytest.raises(ValueError, match="보관된 메일이 없습니다"):
        await pg_store.resolve_archive_user("nobody")
    assert await pg_store.archive_purge(uid) == 2
    assert await pg_store.archive_search(uid, "", 10) == []


async def test_connected_users_are_login_ids(pg_store):
    await pg_store.set_account("E1001", "kim@example.com")
    await pg_store.set_account("E2002", "choi@example.com")
    assert await pg_store.connected_users() == ["E1001", "E2002"]


async def test_stored_as_jsonb_and_timestamptz(pg_store):
    await pg_store.upsert(rec("E1", "a", labels=["INBOX"]))
    pool = await pg_store._get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT jsonb_typeof(to_addrs) AS t, pg_typeof(sent_at)::text AS s FROM mail_message")
    assert row["t"] == "array" and row["s"] == "timestamp with time zone"
