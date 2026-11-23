"""메일 저장소 — 플랫폼 PostgreSQL (alembic 0005 의 mail_* 표). MailStore(SQLite)와 같은 메서드.
퇴사(삭제)한 직원의 메일은 alembic 0006 트리거가 mail_archive 로 옮긴다 — 관리자용 조회·삭제 메서드는 맨 아래.

owner_user_id 로 받는 값은 **users.login_id** (하네스가 로그인 사용자로 채운 requester_id) 이다.
SQL 안에서 `(SELECT id FROM users WHERE login_id = $1)` 로 users.id 로 바꿔 쓰므로,
- 모든 조회·쓰기에 주인 조건이 항상 붙는다 (주인 없이 읽는 함수는 없다),
- users 에 없는 로그인 ID 로는 아무것도 읽히지 않고, 쓰기는 거부된다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .models import ChangeKind, MailCheckpoint, MailMessage, MailRecord, SyncFailure
from .store import SyncStateRow, _escape_like, _need_owner

OWNER = "(SELECT id FROM users WHERE login_id = {})"  # {} 자리에 $n


def _owner_sql(n: int) -> str:
    return OWNER.format(f"${n}")


def to_asyncpg_dsn(url: str) -> str:
    """SQLAlchemy 형식(postgresql+asyncpg://)도 받아서 asyncpg 형식으로 바꾼다."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


class PostgresMailStore:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5) -> None:
        self.dsn = to_asyncpg_dsn(dsn)
        self._min, self._max = min_size, max_size
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg

            async def init(conn):
                await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

            self._pool = await asyncpg.create_pool(self.dsn, min_size=self._min, max_size=self._max, init=init)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _fetch(self, sql: str, *args):
        pool = await self._get_pool()
        async with pool.acquire() as c:
            return await c.fetch(sql, *args)

    async def _fetchrow(self, sql: str, *args):
        pool = await self._get_pool()
        async with pool.acquire() as c:
            return await c.fetchrow(sql, *args)

    async def _execute(self, sql: str, *args) -> str:
        pool = await self._get_pool()
        async with pool.acquire() as c:
            return await c.execute(sql, *args)

    @staticmethod
    def _row_to_message(row) -> MailMessage:
        return MailMessage(
            message_id=row["message_id"], thread_id=row["thread_id"], sender=row["sender"],
            to=row["to_addrs"], cc=row["cc_addrs"], bcc=row["bcc_addrs"], subject=row["subject"],
            sent_at=row["sent_at"], labels=row["labels"], body=row["body"], body_omitted=row["body_omitted"],
        )

    # ── 계정 ──
    async def set_account(self, owner_user_id: str, email: str) -> None:
        owner = _need_owner(owner_user_id)
        import asyncpg

        try:
            await self._execute(
                f"INSERT INTO mail_account (owner_user_id, email) VALUES ({_owner_sql(1)}, $2) "
                "ON CONFLICT (owner_user_id) DO UPDATE SET email = EXCLUDED.email, connected_at = now()",
                owner, email.lower())
        except asyncpg.NotNullViolationError as exc:
            raise ValueError(f"users 에 없는 로그인 ID 입니다: {owner}") from exc

    async def get_account_email(self, owner_user_id: str) -> str | None:
        owner = _need_owner(owner_user_id)
        row = await self._fetchrow(f"SELECT email FROM mail_account WHERE owner_user_id = {_owner_sql(1)}", owner)
        return row["email"] if row else None

    async def connected_users(self) -> list[str]:
        rows = await self._fetch("SELECT u.login_id FROM mail_account a JOIN users u ON u.id = a.owner_user_id "
                                 "ORDER BY u.login_id")
        return [r["login_id"] for r in rows]

    # ── 메일 ──
    async def upsert(self, record: MailRecord) -> ChangeKind | None:
        """새 메일이면 NEW, 내용이 바뀌었으면 UPDATED, 같으면 아무것도 쓰지 않고 None."""
        owner = _need_owner(record.owner_user_id)
        pool = await self._get_pool()
        async with pool.acquire() as c, c.transaction():
            row = await c.fetchrow(
                f"SELECT content_hash FROM mail_message WHERE owner_user_id = {_owner_sql(1)} AND message_id = $2 "
                "FOR UPDATE", owner, record.message_id)
            if row and row["content_hash"] == record.content_hash:
                return None
            await c.execute(
                "INSERT INTO mail_message (owner_user_id, message_id, thread_id, sender, to_addrs, cc_addrs, bcc_addrs, "
                "subject, sent_at, labels, body, body_omitted, size_bytes, content_hash, synced_at) "
                f"VALUES ({_owner_sql(1)}, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, now()) "
                "ON CONFLICT (owner_user_id, message_id) DO UPDATE SET thread_id = EXCLUDED.thread_id, "
                "sender = EXCLUDED.sender, to_addrs = EXCLUDED.to_addrs, cc_addrs = EXCLUDED.cc_addrs, "
                "bcc_addrs = EXCLUDED.bcc_addrs, subject = EXCLUDED.subject, sent_at = EXCLUDED.sent_at, "
                "labels = EXCLUDED.labels, body = EXCLUDED.body, body_omitted = EXCLUDED.body_omitted, "
                "size_bytes = EXCLUDED.size_bytes, content_hash = EXCLUDED.content_hash, synced_at = now()",
                owner, record.message_id, record.thread_id, record.sender, record.to, record.cc, record.bcc,
                record.subject, record.sent_at, record.labels, record.body, record.body_omitted, record.size_bytes,
                record.content_hash)
            return ChangeKind.UPDATED if row else ChangeKind.NEW

    async def delete(self, owner_user_id: str, message_ids: Iterable[str]) -> int:
        owner = _need_owner(owner_user_id)
        ids = list(message_ids)
        if not ids:
            return 0
        status = await self._execute(
            f"DELETE FROM mail_message WHERE owner_user_id = {_owner_sql(1)} AND message_id = ANY($2::text[])",
            owner, ids)
        return int(status.rsplit(" ", 1)[-1])

    async def message_ids(self, owner_user_id: str) -> set[str]:
        owner = _need_owner(owner_user_id)
        rows = await self._fetch(f"SELECT message_id FROM mail_message WHERE owner_user_id = {_owner_sql(1)}", owner)
        return {r["message_id"] for r in rows}

    async def search(self, owner_user_id: str, query: str, limit: int) -> list[MailMessage]:
        """제목·보낸 사람·본문·받는 사람에 검색어가 모두 들어 있는 메일 (최신순). 본문은 비워서 돌려준다."""
        owner = _need_owner(owner_user_id)
        words = [w for w in query.split() if w][:10]
        sql = f"SELECT * FROM mail_message WHERE owner_user_id = {_owner_sql(1)}"
        params: list[Any] = [owner]
        for w in words:
            params.append(f"%{_escape_like(w)}%")
            n = len(params)
            sql += (f" AND (subject ILIKE ${n} OR sender ILIKE ${n} OR body ILIKE ${n} OR to_addrs::text ILIKE ${n})")
        params.append(limit)
        sql += f" ORDER BY sent_at DESC NULLS LAST LIMIT ${len(params)}"
        rows = await self._fetch(sql, *params)
        return [self._row_to_message(r).model_copy(update={"body": ""}) for r in rows]

    async def thread(self, owner_user_id: str, thread_id: str) -> list[MailMessage]:
        owner = _need_owner(owner_user_id)
        rows = await self._fetch(
            f"SELECT * FROM mail_message WHERE owner_user_id = {_owner_sql(1)} AND thread_id = $2 "
            "ORDER BY sent_at NULLS LAST", owner, thread_id)
        return [self._row_to_message(r) for r in rows]

    async def thread_counts(self, owner_user_id: str, thread_ids: Iterable[str]) -> dict[str, int]:
        owner = _need_owner(owner_user_id)
        ids = list(thread_ids)
        if not ids:
            return {}
        rows = await self._fetch(
            f"SELECT thread_id, COUNT(*) AS n FROM mail_message WHERE owner_user_id = {_owner_sql(1)} "
            "AND thread_id = ANY($2::text[]) GROUP BY thread_id", owner, ids)
        return {r["thread_id"]: r["n"] for r in rows}

    # ── 동기화 상태 · 기록 ──
    async def get_sync_state(self, owner_user_id: str) -> SyncStateRow:
        owner = _need_owner(owner_user_id)
        row = await self._fetchrow(f"SELECT * FROM mail_sync_state WHERE owner_user_id = {_owner_sql(1)}", owner)
        if not row:
            return SyncStateRow(has_more=False)
        cp = MailCheckpoint.model_validate(row["checkpoint"]) if row["checkpoint"] else None
        return SyncStateRow(
            last_sync_end=row["last_sync_end"], window_start=row["window_start"], window_end=row["window_end"],
            job_id=row["job_id"], in_progress=cp is not None,
            has_more=cp.has_more if cp else False, page_token=cp.page_token if cp else None,
        )

    async def save_sync_state(self, owner_user_id: str, *, last_sync_end: float | None, window_start: float | None,
                              window_end: float | None, checkpoint: MailCheckpoint | None, job_id: str | None) -> None:
        owner = _need_owner(owner_user_id)
        await self._execute(
            "INSERT INTO mail_sync_state (owner_user_id, last_sync_end, window_start, window_end, checkpoint, job_id) "
            f"VALUES ({_owner_sql(1)}, $2, $3, $4, $5, $6) ON CONFLICT (owner_user_id) DO UPDATE SET "
            "last_sync_end = EXCLUDED.last_sync_end, window_start = EXCLUDED.window_start, "
            "window_end = EXCLUDED.window_end, checkpoint = EXCLUDED.checkpoint, job_id = EXCLUDED.job_id",
            owner, last_sync_end, window_start, window_end,
            checkpoint.model_dump(mode="json") if checkpoint else None, job_id)

    async def record_failures(self, owner_user_id: str, job_id: str, failures: list[SyncFailure]) -> None:
        owner = _need_owner(owner_user_id)
        if not failures:
            return
        pool = await self._get_pool()
        async with pool.acquire() as c:
            await c.executemany(
                "INSERT INTO mail_sync_failure (owner_user_id, job_id, message_id, reason, detail) "
                f"VALUES ({_owner_sql(1)}, $2, $3, $4, $5)",
                [(owner, job_id, f.message_id, str(f.reason), f.detail) for f in failures])

    async def failures(self, owner_user_id: str, job_id: str) -> list[dict]:
        owner = _need_owner(owner_user_id)
        rows = await self._fetch(
            f"SELECT message_id, reason, detail FROM mail_sync_failure WHERE owner_user_id = {_owner_sql(1)} "
            "AND job_id = $2 ORDER BY id", owner, job_id)
        return [dict(r) for r in rows]

    async def record_run(self, owner_user_id: str, job_id: str, mode: str, summary: dict) -> None:
        owner = _need_owner(owner_user_id)
        await self._execute(
            "INSERT INTO mail_sync_run (owner_user_id, job_id, mode, summary) "
            f"VALUES ({_owner_sql(1)}, $2, $3, $4)", owner, job_id, mode, summary)

    async def runs(self, owner_user_id: str) -> list[dict]:
        owner = _need_owner(owner_user_id)
        rows = await self._fetch(
            f"SELECT job_id, mode, summary FROM mail_sync_run WHERE owner_user_id = {_owner_sql(1)} ORDER BY id", owner)
        return [{"job_id": r["job_id"], "mode": r["mode"], **r["summary"]} for r in rows]

    # ── 관리자: 퇴사(삭제)한 직원의 메일 보관 (mail_archive) ──
    # 에이전트 툴(search_mail 등)에는 연결하지 않는다. 관리자가 CLI(gmail-archive)로만 본다.
    async def resolve_archive_user(self, ref: str) -> int:
        """former_user_id(숫자) 또는 삭제 당시 로그인 ID → former_user_id. 같은 로그인 ID 가 여럿이면 숫자로 지정하게 한다."""
        ref = (ref or "").strip()
        if ref.isdigit():
            return int(ref)
        rows = await self._fetch("SELECT DISTINCT former_user_id FROM mail_archive WHERE former_login_id = $1", ref)
        if not rows:
            raise ValueError(f"보관된 메일이 없습니다: {ref}")
        if len(rows) > 1:
            ids = ", ".join(str(r["former_user_id"]) for r in rows)
            raise ValueError(f"같은 로그인 ID 로 보관된 직원이 여러 명입니다 ({ids}). former_user_id 로 지정하세요")
        return rows[0]["former_user_id"]

    async def archive_users(self) -> list[dict]:
        rows = await self._fetch(
            "SELECT former_user_id, former_login_id, former_name, account_email, count(*) AS mails, "
            "max(archived_at) AS archived_at FROM mail_archive "
            "GROUP BY former_user_id, former_login_id, former_name, account_email ORDER BY max(archived_at) DESC")
        return [dict(r) for r in rows]

    async def archive_search(self, former_user_id: int, query: str, limit: int) -> list[MailMessage]:
        words = [w for w in query.split() if w][:10]
        sql = "SELECT * FROM mail_archive WHERE former_user_id = $1"
        params: list[Any] = [former_user_id]
        for w in words:
            params.append(f"%{_escape_like(w)}%")
            n = len(params)
            sql += f" AND (subject ILIKE ${n} OR sender ILIKE ${n} OR body ILIKE ${n} OR to_addrs::text ILIKE ${n})"
        params.append(limit)
        sql += f" ORDER BY sent_at DESC NULLS LAST LIMIT ${len(params)}"
        rows = await self._fetch(sql, *params)
        return [self._row_to_message(r).model_copy(update={"body": ""}) for r in rows]

    async def archive_thread(self, former_user_id: int, thread_id: str) -> list[MailMessage]:
        rows = await self._fetch("SELECT * FROM mail_archive WHERE former_user_id = $1 AND thread_id = $2 "
                                 "ORDER BY sent_at NULLS LAST", former_user_id, thread_id)
        return [self._row_to_message(r) for r in rows]

    async def archive_purge(self, former_user_id: int) -> int:
        """보관된 메일을 영구 삭제한다 (보존 기간이 끝난 뒤 관리자가). 되돌릴 수 없다."""
        status = await self._execute("DELETE FROM mail_archive WHERE former_user_id = $1", former_user_id)
        return int(status.rsplit(" ", 1)[-1])
