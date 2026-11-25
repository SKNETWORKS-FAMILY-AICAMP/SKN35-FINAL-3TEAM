"""화면의 발송 요청 키를 영구 예약해 중복 클릭·응답 유실 후 재발송을 막는다."""

import json
import sqlite3
from pathlib import Path


class MailDispatchStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def reserve(self, owner: str, key: str, fingerprint: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS mail_dispatch (owner TEXT, request_key TEXT, fingerprint TEXT, result TEXT, PRIMARY KEY(owner, request_key))")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint FROM mail_dispatch WHERE owner=? AND request_key=?", (owner, key)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError("이미 사용한 발송 요청의 내용을 변경할 수 없습니다.")
                return False
            db.execute("INSERT INTO mail_dispatch VALUES (?, ?, ?, NULL)", (owner, key, fingerprint))
            return True

    def finish(self, owner: str, key: str, result: dict):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE mail_dispatch SET result=? WHERE owner=? AND request_key=?", (json.dumps(result, ensure_ascii=False), owner, key))

    def result(self, owner: str, key: str) -> dict | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT result FROM mail_dispatch WHERE owner=? AND request_key=?", (owner, key)).fetchone()
            return json.loads(row[0]) if row and row[0] else None
