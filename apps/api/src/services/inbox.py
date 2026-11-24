"""직원 알림함 (UC-03). 알림은 받는 사람만 열 수 있다. 지금은 로컬 SQLite, DB가 붙으면 이 클래스만 바꾼다."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class InboxStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("""
            CREATE TABLE IF NOT EXISTS inbox_alerts (
                alert_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                summary_dropped INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT
            )
        """)
        return connection

    def add(self, owner_id: str, kind: str, title: str, summary: str, summary_dropped: bool, payload: dict) -> dict:
        alert = {
            "alert_id": "ALERT-" + uuid4().hex[:12],
            "owner_id": owner_id, "kind": kind, "title": title, "summary": summary,
            "summary_dropped": summary_dropped, "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(), "read_at": None,
        }
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO inbox_alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (alert["alert_id"], owner_id, kind, title, summary, int(summary_dropped),
                 json.dumps(payload, ensure_ascii=False), alert["created_at"], None),
            )
        return alert

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        return {**dict(row), "summary_dropped": bool(row["summary_dropped"]), "payload": json.loads(row["payload"])}

    def list(self, owner_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM inbox_alerts WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def open(self, owner_id: str, alert_id: str) -> dict | None:
        """다른 사람의 알림은 없는 것과 같다 (None → 404). 처음 열면 읽은 시각을 남긴다."""
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM inbox_alerts WHERE alert_id = ? AND owner_id = ?", (alert_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            if row["read_at"] is None:
                connection.execute("UPDATE inbox_alerts SET read_at = ? WHERE alert_id = ?",
                                   (datetime.now(timezone.utc).isoformat(), alert_id))
                row = connection.execute("SELECT * FROM inbox_alerts WHERE alert_id = ?", (alert_id,)).fetchone()
        return self._row(row)
