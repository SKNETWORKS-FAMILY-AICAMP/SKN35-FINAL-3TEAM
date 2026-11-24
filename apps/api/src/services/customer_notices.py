"""고객 안내 메일 발송 기록 (UC-05). 같은 대출·같은 만기일 안내는 한 번만 보낸다.

상태: sending(보내는 중, 결과를 모름 포함) → sent(보냄) / failed(실패, 다시 보낼 수 있음)
     rejected(승인 단계에서 반려, 다시 요청할 수 있음)
- 승인 카드를 띄우기 전에 active() 로 확인한다. 보냈거나 보내는 중이면 카드 없이 거부한다 (permission/customer_mail.py).
- 승인 뒤 발송 직전에 reserve() 로 자리를 잡는다. 동시에 두 번 눌러도 한 건만 잡힌다 (DB 는 부분 유일 인덱스).
- 발송 응답이 끊겨 결과를 모르면 sending 그대로 둔다 → 다시 보내지 않고 보낸편지함을 확인하라고 안내한다.

두 저장소가 같은 메서드를 가진다 (모두 async).
- NoticeLog    로컬 SQLite (STORE=memory, 테스트)
- DbNoticeLog  PostgreSQL customer_notice 표 (STORE=db, 마이그레이션 0013)
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

ACTIVE = ("sending", "sent")
FINISHED = ("sent", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NoticeLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS customer_notice (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                loan_id TEXT NOT NULL,
                maturity_date TEXT NOT NULL,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL,
                message_id TEXT,
                call_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_customer_notice_once ON customer_notice (loan_id, maturity_date)
                WHERE status IN ('sending', 'sent');
        """)
        return connection

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return None if row is None else {key: row[key] for key in row.keys() if key != "owner_id"}

    def _active(self, loan_id: str, maturity_date: str) -> dict | None:
        with closing(self._connect()) as connection:
            return self._row(connection.execute(
                "SELECT * FROM customer_notice WHERE loan_id = ? AND maturity_date = ? AND status IN ('sending', 'sent')",
                (loan_id, maturity_date)).fetchone())

    async def active(self, loan_id: str, maturity_date: str) -> dict | None:
        """보냈거나 보내는 중인 기록 (없으면 None)."""
        return await asyncio.to_thread(self._active, loan_id, maturity_date)

    def _reserve(self, owner_id, loan_id, maturity_date, recipient, subject, call_id) -> int | None:
        now = _now()
        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    "INSERT INTO customer_notice (owner_id, loan_id, maturity_date, recipient, subject, status, call_id,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'sending', ?, ?, ?)",
                    (owner_id, loan_id, maturity_date, recipient, subject, call_id, now, now))
                return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    async def reserve(self, owner_id: str, loan_id: str, maturity_date: str, recipient: str, subject: str,
                      call_id: str) -> int | None:
        """발송 자리를 잡는다. 이미 보냈거나 보내는 중이면 None."""
        return await asyncio.to_thread(self._reserve, owner_id, loan_id, maturity_date, recipient, subject, call_id)

    def _finish(self, notice_id: int, status: str, message_id: str | None, error: str | None) -> None:
        now = _now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE customer_notice SET status = ?, message_id = ?, error = ?, sent_at = ?, updated_at = ?"
                " WHERE id = ? AND status = 'sending'",
                (status, message_id, error, now if status == "sent" else None, now, notice_id))

    async def finish(self, notice_id: int, status: str, *, message_id: str | None = None, error: str | None = None) -> None:
        if status not in FINISHED:
            raise ValueError(status)
        await asyncio.to_thread(self._finish, notice_id, status, message_id, error)

    def _reject(self, owner_id, loan_id, maturity_date, recipient, subject, call_id, error) -> None:
        now = _now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO customer_notice (owner_id, loan_id, maturity_date, recipient, subject, status, call_id, error,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'rejected', ?, ?, ?, ?)",
                (owner_id, loan_id, maturity_date, recipient, subject, call_id, error, now, now))

    async def reject(self, owner_id: str, loan_id: str, maturity_date: str, recipient: str, subject: str,
                     call_id: str, reason: str) -> None:
        """승인 단계에서 반려(또는 시간 초과)된 안내. 보내지 않았으므로 다시 요청할 수 있다."""
        await asyncio.to_thread(self._reject, owner_id, loan_id, maturity_date, recipient, subject, call_id, reason)

    def _history(self, owner_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM customer_notice WHERE owner_id = ? ORDER BY id DESC",
                                      (owner_id,)).fetchall()
        return [self._row(row) for row in rows]

    async def history(self, owner_id: str) -> list[dict]:
        return await asyncio.to_thread(self._history, owner_id)


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _db_row(row: Any) -> dict | None:
    if row is None:
        return None
    out = dict(row._mapping)
    for key in ("maturity_date", "created_at", "sent_at", "updated_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    return out


_COLUMNS = "n.id, n.loan_id, n.maturity_date, n.recipient, n.subject, n.status, n.message_id, n.call_id, n.error, " \
           "n.created_at, n.sent_at, n.updated_at"


class DbNoticeLog:
    def __init__(self, sessions) -> None:
        self._sessions = sessions

    async def active(self, loan_id: str, maturity_date: str) -> dict | None:
        async with self._sessions() as s:
            row = (await s.execute(text(f"""SELECT {_COLUMNS} FROM customer_notice n
                WHERE n.loan_id = :l AND n.maturity_date = :d AND n.status IN ('sending', 'sent')"""),
                {"l": loan_id, "d": _date(maturity_date)})).first()
        return _db_row(row)

    async def reserve(self, owner_id: str, loan_id: str, maturity_date: str, recipient: str, subject: str,
                      call_id: str) -> int | None:
        async with self._sessions() as s:
            try:
                notice_id = await s.scalar(text("""
                    INSERT INTO customer_notice (user_id, loan_id, maturity_date, recipient, subject, status, call_id)
                    SELECT u.id, :l, :d, :r, :t, 'sending', :c FROM users u WHERE u.login_id = :u RETURNING id"""),
                    {"u": owner_id, "l": loan_id, "d": _date(maturity_date), "r": recipient, "t": subject[:255],
                     "c": call_id})
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return None
        if notice_id is None:
            raise ValueError(f"DB users 표에 없는 사용자입니다: {owner_id}")
        return notice_id

    async def finish(self, notice_id: int, status: str, *, message_id: str | None = None, error: str | None = None) -> None:
        if status not in FINISHED:
            raise ValueError(status)
        async with self._sessions() as s:
            await s.execute(text("""
                UPDATE customer_notice SET status = :s, message_id = :m, error = :e, updated_at = now(),
                       sent_at = CASE WHEN :sent THEN now() END
                WHERE id = :i AND status = 'sending'"""),
                {"s": status, "sent": status == "sent", "m": message_id, "e": error, "i": notice_id})
            await s.commit()

    async def reject(self, owner_id: str, loan_id: str, maturity_date: str, recipient: str, subject: str,
                     call_id: str, reason: str) -> None:
        async with self._sessions() as s:
            await s.execute(text("""
                INSERT INTO customer_notice (user_id, loan_id, maturity_date, recipient, subject, status, call_id, error)
                SELECT u.id, :l, :d, :r, :t, 'rejected', :c, :e FROM users u WHERE u.login_id = :u"""),
                {"u": owner_id, "l": loan_id, "d": _date(maturity_date), "r": recipient, "t": subject[:255],
                 "c": call_id, "e": reason})
            await s.commit()

    async def history(self, owner_id: str) -> list[dict]:
        async with self._sessions() as s:
            rows = (await s.execute(text(f"""SELECT {_COLUMNS} FROM customer_notice n JOIN users u ON u.id = n.user_id
                WHERE u.login_id = :u ORDER BY n.id DESC"""), {"u": owner_id})).all()
        return [_db_row(row) for row in rows]
