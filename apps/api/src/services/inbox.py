"""직원 알림함 (UC-03 만기 알림, UC-05 발송 결과). 알림은 받는 사람만 열 수 있다.

두 저장소가 같은 메서드를 가진다 (모두 async).
- InboxStore    로컬 SQLite (STORE=memory, 테스트)
- DbInboxStore  PostgreSQL notification 표 (STORE=db, 마이그레이션 0013)

알림 모양: {"alert_id", "kind", "title", "summary", "summary_dropped", "payload", "created_at", "read_at"}
kind: maturity_alert (만기 알림) · notice_result (고객 안내 발송·반려 결과)
같은 알림(payload.key 가 같은 것)을 다시 만들면 새로 만들지 않고 있던 것을 돌려준다 (find).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

KINDS = ("maturity_alert", "notice_result")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_uuid(session_id: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return None


class InboxStore:
    """로컬 SQLite 알림함. 번호는 1, 2, 3 … (DB 와 같은 모양)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE IF NOT EXISTS notification (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                body_dropped INTEGER NOT NULL,
                payload TEXT NOT NULL,
                has_customer_info INTEGER NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                read_at TEXT,
                deleted_at TEXT
            )
        """)
        return connection

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        return {"alert_id": str(row["id"]), "kind": row["kind"], "title": row["title"], "summary": row["body"],
                "summary_dropped": bool(row["body_dropped"]), "payload": json.loads(row["payload"]),
                "created_at": row["created_at"], "read_at": row["read_at"]}

    def _add(self, owner_id, kind, title, summary, summary_dropped, payload, has_customer_info, session_id) -> dict:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO notification (owner_id, kind, title, body, body_dropped, payload, has_customer_info,"
                " session_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (owner_id, kind, title, summary, int(summary_dropped), json.dumps(payload, ensure_ascii=False),
                 int(has_customer_info), session_id, _now()),
            )
            row = connection.execute("SELECT * FROM notification WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row(row)

    async def add(self, owner_id: str, kind: str, title: str, summary: str, summary_dropped: bool, payload: dict, *,
                  has_customer_info: bool = False, session_id: str | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"알림 종류가 아닙니다: {kind}")
        return await asyncio.to_thread(self._add, owner_id, kind, title, summary, summary_dropped, payload,
                                       has_customer_info, session_id)

    def _select(self, sql: str, args: tuple) -> list[dict]:
        with closing(self._connect()) as connection:
            return [self._row(row) for row in connection.execute(sql, args).fetchall()]

    async def find(self, owner_id: str, kind: str, key: str) -> dict | None:
        rows = await asyncio.to_thread(
            self._select, "SELECT * FROM notification WHERE owner_id = ? AND kind = ? AND deleted_at IS NULL"
                          " AND json_extract(payload, '$.key') = ? ORDER BY id DESC LIMIT 1", (owner_id, kind, key))
        return rows[0] if rows else None

    async def list(self, owner_id: str) -> list[dict]:
        return await asyncio.to_thread(
            self._select, "SELECT * FROM notification WHERE owner_id = ? AND deleted_at IS NULL ORDER BY id DESC",
            (owner_id,))

    async def unread_count(self, owner_id: str) -> int:
        rows = await self.list(owner_id)
        return sum(1 for row in rows if row["read_at"] is None)

    def _open(self, owner_id: str, alert_id: str) -> dict | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM notification WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
                                     (alert_id, owner_id)).fetchone()
            if row is None:
                return None
            if row["read_at"] is None:
                connection.execute("UPDATE notification SET read_at = ? WHERE id = ?", (_now(), row["id"]))
                row = connection.execute("SELECT * FROM notification WHERE id = ?", (row["id"],)).fetchone()
        return self._row(row)

    async def open(self, owner_id: str, alert_id: str) -> dict | None:
        """다른 사람의 알림은 없는 것과 같다 (None → 404). 처음 열면 읽은 시각을 남긴다."""
        if not str(alert_id).isdigit():
            return None
        return await asyncio.to_thread(self._open, owner_id, alert_id)

    def _delete(self, owner_id: str, alert_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE notification SET deleted_at = ? WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
                (_now(), alert_id, owner_id))
        return cursor.rowcount == 1

    async def delete(self, owner_id: str, alert_id: str) -> bool:
        if not str(alert_id).isdigit():
            return False
        return await asyncio.to_thread(self._delete, owner_id, alert_id)


_SELECT = """
    SELECT n.id, n.kind, n.title, n.body, n.body_dropped, n.payload, n.created_at, n.read_at
    FROM notification n JOIN users u ON u.id = n.user_id
"""


class DbInboxStore:
    """PostgreSQL notification 표. 사람은 login_id 로 받고 DB 에는 users.id 로 넣는다."""

    def __init__(self, sessions) -> None:
        self._sessions = sessions

    @staticmethod
    def _row(row: Any) -> dict:
        return {"alert_id": str(row.id), "kind": row.kind, "title": row.title, "summary": row.body,
                "summary_dropped": row.body_dropped, "payload": row.payload,
                "created_at": row.created_at.isoformat(), "read_at": row.read_at.isoformat() if row.read_at else None}

    async def add(self, owner_id: str, kind: str, title: str, summary: str, summary_dropped: bool, payload: dict, *,
                  has_customer_info: bool = False, session_id: str | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"알림 종류가 아닙니다: {kind}")
        async with self._sessions() as s:
            row = (await s.execute(text("""
                INSERT INTO notification (user_id, kind, title, body, body_dropped, payload, has_customer_info, session_id)
                SELECT u.id, :k, :t, :b, :d, CAST(:p AS JSONB), :c, (SELECT id FROM chat_session WHERE id = :s)
                FROM users u WHERE u.login_id = :u
                RETURNING id, kind, title, body, body_dropped, payload, created_at, read_at"""),
                {"u": owner_id, "k": kind, "t": title[:200], "b": summary, "d": summary_dropped,
                 "p": json.dumps(payload, ensure_ascii=False), "c": has_customer_info,
                 "s": _session_uuid(session_id)})).first()
            if row is None:
                raise ValueError(f"DB users 표에 없는 사용자입니다: {owner_id}")
            await s.commit()
        return self._row(row)

    async def find(self, owner_id: str, kind: str, key: str) -> dict | None:
        async with self._sessions() as s:
            row = (await s.execute(text(_SELECT + """
                WHERE u.login_id = :u AND n.kind = :k AND n.deleted_at IS NULL AND n.payload->>'key' = :key
                ORDER BY n.id DESC LIMIT 1"""), {"u": owner_id, "k": kind, "key": key})).first()
        return self._row(row) if row else None

    async def list(self, owner_id: str) -> list[dict]:
        async with self._sessions() as s:
            rows = (await s.execute(text(_SELECT + " WHERE u.login_id = :u AND n.deleted_at IS NULL ORDER BY n.id DESC"),
                                    {"u": owner_id})).all()
        return [self._row(row) for row in rows]

    async def unread_count(self, owner_id: str) -> int:
        async with self._sessions() as s:
            return await s.scalar(text("""
                SELECT count(*) FROM notification n JOIN users u ON u.id = n.user_id
                WHERE u.login_id = :u AND n.deleted_at IS NULL AND n.read_at IS NULL"""), {"u": owner_id})

    async def open(self, owner_id: str, alert_id: str) -> dict | None:
        if not str(alert_id).isdigit():
            return None
        async with self._sessions() as s:
            await s.execute(text("""
                UPDATE notification n SET read_at = now() FROM users u
                WHERE u.id = n.user_id AND u.login_id = :u AND n.id = :i AND n.read_at IS NULL AND n.deleted_at IS NULL"""),
                {"u": owner_id, "i": int(alert_id)})
            row = (await s.execute(text(_SELECT + " WHERE u.login_id = :u AND n.id = :i AND n.deleted_at IS NULL"),
                                   {"u": owner_id, "i": int(alert_id)})).first()
            await s.commit()
        return self._row(row) if row else None

    async def delete(self, owner_id: str, alert_id: str) -> bool:
        if not str(alert_id).isdigit():
            return False
        async with self._sessions() as s:
            result = await s.execute(text("""
                UPDATE notification n SET deleted_at = now() FROM users u
                WHERE u.id = n.user_id AND u.login_id = :u AND n.id = :i AND n.deleted_at IS NULL"""),
                {"u": owner_id, "i": int(alert_id)})
            await s.commit()
        return result.rowcount == 1
