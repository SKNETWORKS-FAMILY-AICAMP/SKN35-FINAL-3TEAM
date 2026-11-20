"""메일 저장소 — 관계형 DB(개발용 SQLite). 벡터DB · 청킹에는 넣지 않는다.

권한 원칙: **모든 함수가 owner_user_id 를 필수로 받는다.** 주인 조건 없이 메일을 읽는 함수는 두지 않는다.
LLM 이 고른 값이 아니라 플랫폼이 넘겨준 로그인 사용자만 owner_user_id 로 들어온다 (manifest 의 inject).

플랫폼 DB 가 정해지면 같은 메서드를 가진 구현으로 바꾼다 (테이블 정의: sql/mail_schema.sql).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ChangeKind, MailCheckpoint, MailMessage, MailRecord, SyncFailure

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_account (
    owner_user_id  TEXT PRIMARY KEY,
    email          TEXT NOT NULL,
    connected_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_message (
    owner_user_id  TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    sender         TEXT NOT NULL DEFAULT '',
    to_addrs       TEXT NOT NULL DEFAULT '[]',
    cc_addrs       TEXT NOT NULL DEFAULT '[]',
    bcc_addrs      TEXT NOT NULL DEFAULT '[]',
    subject        TEXT NOT NULL DEFAULT '',
    sent_at        TEXT,
    labels         TEXT NOT NULL DEFAULT '[]',
    body           TEXT NOT NULL DEFAULT '',
    body_omitted   INTEGER NOT NULL DEFAULT 0,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    content_hash   TEXT NOT NULL,
    synced_at      TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, message_id)
);
CREATE INDEX IF NOT EXISTS ix_mail_owner_thread ON mail_message (owner_user_id, thread_id);
CREATE INDEX IF NOT EXISTS ix_mail_owner_sent   ON mail_message (owner_user_id, sent_at);
CREATE TABLE IF NOT EXISTS mail_sync_state (
    owner_user_id  TEXT PRIMARY KEY,
    last_sync_end  REAL,
    window_start   REAL,
    window_end     REAL,
    checkpoint     TEXT,
    job_id         TEXT
);
CREATE TABLE IF NOT EXISTS mail_sync_failure (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id  TEXT NOT NULL,
    job_id         TEXT NOT NULL,
    message_id     TEXT,
    reason         TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_sync_run (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id  TEXT NOT NULL,
    job_id         TEXT NOT NULL,
    mode           TEXT NOT NULL,
    summary        TEXT NOT NULL,
    finished_at    TEXT NOT NULL
);
"""

_COLUMNS = ("owner_user_id", "message_id", "thread_id", "sender", "to_addrs", "cc_addrs", "bcc_addrs", "subject",
            "sent_at", "labels", "body", "body_omitted", "size_bytes", "content_hash", "synced_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _need_owner(owner_user_id: str) -> str:
    if not owner_user_id or not str(owner_user_id).strip():
        raise ValueError("owner_user_id 가 비어 있습니다 — 주인 없이 메일을 조회할 수 없습니다")
    return str(owner_user_id)


def _escape_like(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SyncStateRow(MailCheckpoint):
    last_sync_end: float | None = None
    window_start: float | None = None
    window_end: float | None = None
    job_id: str | None = None
    in_progress: bool = False


class MailStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._ready = False

    # ── 내부 ──
    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if not self._ready:
            conn.executescript(SCHEMA)
            self._ready = True
        return conn

    async def _run(self, fn):
        def work():
            conn = self._connect()
            try:
                with conn:  # 트랜잭션: 성공하면 커밋, 예외면 롤백
                    return fn(conn)
            finally:
                conn.close()

        return await asyncio.to_thread(work)

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MailMessage:
        return MailMessage(
            message_id=row["message_id"], thread_id=row["thread_id"], sender=row["sender"],
            to=json.loads(row["to_addrs"]), cc=json.loads(row["cc_addrs"]), bcc=json.loads(row["bcc_addrs"]),
            subject=row["subject"], sent_at=row["sent_at"], labels=json.loads(row["labels"]),
            body=row["body"], body_omitted=bool(row["body_omitted"]),
        )

    # ── 계정 ──
    async def set_account(self, owner_user_id: str, email: str) -> None:
        owner = _need_owner(owner_user_id)
        await self._run(lambda c: c.execute(
            "INSERT INTO mail_account VALUES (?,?,?) ON CONFLICT(owner_user_id) DO UPDATE SET email=excluded.email,"
            " connected_at=excluded.connected_at", (owner, email.lower(), _now())))

    async def get_account_email(self, owner_user_id: str) -> str | None:
        owner = _need_owner(owner_user_id)
        row = await self._run(lambda c: c.execute(
            "SELECT email FROM mail_account WHERE owner_user_id=?", (owner,)).fetchone())
        return row["email"] if row else None

    async def connected_users(self) -> list[str]:
        rows = await self._run(lambda c: c.execute("SELECT owner_user_id FROM mail_account ORDER BY 1").fetchall())
        return [r["owner_user_id"] for r in rows]

    # ── 메일 ──
    async def upsert(self, record: MailRecord) -> ChangeKind | None:
        """새 메일이면 NEW, 내용이 바뀌었으면 UPDATED, 같으면 아무것도 쓰지 않고 None."""
        owner = _need_owner(record.owner_user_id)
        values = (owner, record.message_id, record.thread_id, record.sender, json.dumps(record.to),
                  json.dumps(record.cc), json.dumps(record.bcc), record.subject,
                  record.sent_at.isoformat() if record.sent_at else None, json.dumps(record.labels), record.body,
                  int(record.body_omitted), record.size_bytes, record.content_hash, _now())

        def work(c: sqlite3.Connection):
            row = c.execute("SELECT content_hash FROM mail_message WHERE owner_user_id=? AND message_id=?",
                            (owner, record.message_id)).fetchone()
            if row and row["content_hash"] == record.content_hash:
                return None
            c.execute(f"INSERT OR REPLACE INTO mail_message ({','.join(_COLUMNS)}) VALUES ({','.join('?' * len(_COLUMNS))})",
                      values)
            return ChangeKind.UPDATED if row else ChangeKind.NEW

        return await self._run(work)

    async def delete(self, owner_user_id: str, message_ids: Iterable[str]) -> int:
        owner = _need_owner(owner_user_id)
        ids = list(message_ids)

        def work(c: sqlite3.Connection):
            return sum(c.execute("DELETE FROM mail_message WHERE owner_user_id=? AND message_id=?",
                                 (owner, mid)).rowcount for mid in ids)

        return await self._run(work)

    async def message_ids(self, owner_user_id: str) -> set[str]:
        owner = _need_owner(owner_user_id)
        rows = await self._run(lambda c: c.execute(
            "SELECT message_id FROM mail_message WHERE owner_user_id=?", (owner,)).fetchall())
        return {r["message_id"] for r in rows}

    async def search(self, owner_user_id: str, query: str, limit: int) -> list[MailMessage]:
        """제목·보낸 사람·본문에 검색어가 모두 들어 있는 메일 (최신순). 본문은 비워서 돌려준다."""
        owner = _need_owner(owner_user_id)
        words = [w for w in query.split() if w][:10]
        sql = "SELECT * FROM mail_message WHERE owner_user_id=?"
        params: list[Any] = [owner]
        for w in words:
            sql += (" AND (subject LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\'"
                    " OR to_addrs LIKE ? ESCAPE '\\')")
            like = f"%{_escape_like(w)}%"
            params += [like, like, like, like]
        sql += " ORDER BY sent_at DESC LIMIT ?"
        params.append(limit)
        rows = await self._run(lambda c: c.execute(sql, params).fetchall())
        return [self._row_to_message(r).model_copy(update={"body": ""}) for r in rows]

    async def thread(self, owner_user_id: str, thread_id: str) -> list[MailMessage]:
        owner = _need_owner(owner_user_id)
        rows = await self._run(lambda c: c.execute(
            "SELECT * FROM mail_message WHERE owner_user_id=? AND thread_id=? ORDER BY sent_at", (owner, thread_id)
        ).fetchall())
        return [self._row_to_message(r) for r in rows]

    async def thread_counts(self, owner_user_id: str, thread_ids: Iterable[str]) -> dict[str, int]:
        owner = _need_owner(owner_user_id)
        ids = list(thread_ids)
        if not ids:
            return {}
        rows = await self._run(lambda c: c.execute(
            f"SELECT thread_id, COUNT(*) n FROM mail_message WHERE owner_user_id=? AND thread_id IN "
            f"({','.join('?' * len(ids))}) GROUP BY thread_id", [owner, *ids]).fetchall())
        return {r["thread_id"]: r["n"] for r in rows}

    # ── 동기화 상태 · 기록 ──
    async def get_sync_state(self, owner_user_id: str) -> SyncStateRow:
        owner = _need_owner(owner_user_id)
        row = await self._run(lambda c: c.execute(
            "SELECT * FROM mail_sync_state WHERE owner_user_id=?", (owner,)).fetchone())
        if not row:
            return SyncStateRow(has_more=False)
        cp = MailCheckpoint.model_validate_json(row["checkpoint"]) if row["checkpoint"] else None
        return SyncStateRow(
            last_sync_end=row["last_sync_end"], window_start=row["window_start"], window_end=row["window_end"],
            job_id=row["job_id"], in_progress=cp is not None,
            has_more=cp.has_more if cp else False, page_token=cp.page_token if cp else None,
        )

    async def save_sync_state(self, owner_user_id: str, *, last_sync_end: float | None, window_start: float | None,
                              window_end: float | None, checkpoint: MailCheckpoint | None, job_id: str | None) -> None:
        owner = _need_owner(owner_user_id)
        await self._run(lambda c: c.execute(
            "INSERT OR REPLACE INTO mail_sync_state VALUES (?,?,?,?,?,?)",
            (owner, last_sync_end, window_start, window_end,
             checkpoint.model_dump_json() if checkpoint else None, job_id)))

    async def record_failures(self, owner_user_id: str, job_id: str, failures: list[SyncFailure]) -> None:
        owner = _need_owner(owner_user_id)
        if failures:
            await self._run(lambda c: c.executemany(
                "INSERT INTO mail_sync_failure (owner_user_id, job_id, message_id, reason, detail, created_at)"
                " VALUES (?,?,?,?,?,?)",
                [(owner, job_id, f.message_id, str(f.reason), f.detail, _now()) for f in failures]))

    async def failures(self, owner_user_id: str, job_id: str) -> list[dict]:
        owner = _need_owner(owner_user_id)
        rows = await self._run(lambda c: c.execute(
            "SELECT message_id, reason, detail FROM mail_sync_failure WHERE owner_user_id=? AND job_id=?",
            (owner, job_id)).fetchall())
        return [dict(r) for r in rows]

    async def record_run(self, owner_user_id: str, job_id: str, mode: str, summary: dict) -> None:
        owner = _need_owner(owner_user_id)
        await self._run(lambda c: c.execute(
            "INSERT INTO mail_sync_run (owner_user_id, job_id, mode, summary, finished_at) VALUES (?,?,?,?,?)",
            (owner, job_id, mode, json.dumps(summary, ensure_ascii=False), _now())))

    async def runs(self, owner_user_id: str) -> list[dict]:
        owner = _need_owner(owner_user_id)
        rows = await self._run(lambda c: c.execute(
            "SELECT job_id, mode, summary FROM mail_sync_run WHERE owner_user_id=? ORDER BY id", (owner,)).fetchall())
        return [{"job_id": r["job_id"], "mode": r["mode"], **json.loads(r["summary"])} for r in rows]
