"""문서 양식(JSON)과 저장된 문서. 모든 서류(양식 문서·시장 리포트)가 이 저장소 하나에 저장되고 같은 내려받기를 쓴다.

양식은 config/document_forms/*.json 에 둔다. 칸(section)마다 누가 채우는지 적는다.
- data : 코드가 조회 결과(source.provider)에서 그대로 옮긴다. 숫자·표는 모두 여기.
- llm  : 모델이 문장을 쓴다 (fill_document 툴 인자). 길이 제한, 숫자 검사(check_numbers) 가능.
- fixed: 양식에 적힌 고정 문구.
채운 결과는 공통 문서 형식(blocks, services/document_render.py)으로 저장한다.
시장 리포트(config/report_templates, 시장 MCP 코드)도 같은 형식이고, 같은 저장소에 들어간다 (MarketRecords).

저장소 두 가지 (메서드 같음, 모두 async)
- DocumentStore    로컬 SQLite (STORE=memory, 테스트)
- DbDocumentStore  PostgreSQL output_file 표 + 파일 저장소 outputs/{user}/… (STORE=db).
                   문서 JSON 은 파일로, 목록·소유자·해시는 표로. 내려받기는 audit_log(output_download) 에 남긴다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from contracts.permission import Principal

FILLS = {"data", "llm", "fixed"}
TYPES = {"text", "keyvalue", "table"}

# 조회 결과를 만드는 함수: (요청자, 인자) → dict. 요청자 권한 필터는 함수 안에서 한다.
Provider = Callable[[Principal, dict], dict]


@dataclass(frozen=True)
class DocumentForm:
    form_id: str
    name: str
    title: str
    description: str
    provider: str
    params: dict
    sections: list[dict]
    has_customer_info: bool = False
    version: int = 1
    page: str = "portrait"

    @property
    def llm_sections(self) -> list[dict]:
        return [s for s in self.sections if s["fill"] == "llm"]

    def guide(self) -> str:
        """툴 설명에 넣을 한 줄: 양식 id, 넘길 인자, 모델이 쓸 칸."""
        params = ", ".join(f"{k}({v.get('label', k)}, {v.get('min')}~{v.get('max')})" for k, v in self.params.items())
        fields = ", ".join(f"{s['key']}({s['label']}, {s.get('max_chars', 500)}자 이내: {s.get('hint', '')})" for s in self.llm_sections)
        return f"{self.form_id}({self.name}) — params: {params or '없음'}; fields: {fields or '없음'}"


def load_forms(directory: Path) -> dict[str, DocumentForm]:
    forms: dict[str, DocumentForm] = {}
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        where = path.name
        for s in raw["sections"]:
            if s.get("fill") not in FILLS or s.get("type", "text") not in TYPES or not s.get("key"):
                raise ValueError(f"{where}: 칸 {s.get('key')!r} 의 fill/type 을 확인하세요")
            if s["fill"] == "data" and not s.get("from"):
                raise ValueError(f"{where}: data 칸 {s['key']!r} 에 from 이 없습니다")
            if s["fill"] == "fixed" and not s.get("text"):
                raise ValueError(f"{where}: fixed 칸 {s['key']!r} 에 text 가 없습니다")
        keys = [s["key"] for s in raw["sections"]]
        if len(keys) != len(set(keys)) or raw["id"] in forms:
            raise ValueError(f"{where}: 칸 key 또는 양식 id 가 겹칩니다")
        forms[raw["id"]] = DocumentForm(
            form_id=raw["id"], name=raw["name"], title=raw["title"], description=raw.get("description", ""),
            provider=raw["source"]["provider"], params=raw["source"].get("params", {}),
            sections=raw["sections"], has_customer_info=bool(raw.get("has_customer_info")),
            version=int(raw.get("version", 1)), page=raw.get("page", "portrait"),
        )
    return forms


def _pick(data: dict, path: str):
    value = data
    for part in path.split("."):
        value = value[part]
    return value


def cell_text(value, column: dict) -> str:
    if column.get("format") == "d_day" and isinstance(value, int):
        return f"{-value}일 지남" if value < 0 else f"D-{value}"
    return "" if value is None else str(value)


def keyvalue_text(value: dict) -> str:
    return " · ".join(f"{key} {count}건" if isinstance(count, int) else f"{key} {count}" for key, count in value.items())


def build_document(form: DocumentForm, data: dict, params: dict, fields: dict[str, str], prepared_by: str) -> dict:
    """양식 + 조회 결과 + 모델이 쓴 칸 → 공통 문서 형식(blocks). fields 는 이미 검사를 마친 값."""
    title = form.title.format(**{**{k: v for k, v in data.items() if isinstance(v, (str, int))}, **params})
    blocks: list[dict] = [{"type": "heading", "level": 1, "text": title},
                          {"type": "field", "label": "작성", "value": prepared_by}]
    for s in form.sections:
        kind = s.get("type", "text")
        if s["fill"] == "data":
            value = _pick(data, s["from"])
        elif s["fill"] == "fixed":
            value = s["text"]
        else:
            value = fields.get(s["key"], "")
        if kind == "text" and not value:
            continue  # 비운 문장 칸은 제목째 뺀다
        if kind == "text" and s["fill"] == "data":
            blocks.append({"type": "field", "label": s["label"], "value": str(value)})
            continue
        blocks.append({"type": "heading", "level": 2, "text": s["label"]})
        if kind == "table":
            columns = s["columns"]
            if value:
                blocks.append({"type": "table", "columns": [c["label"] for c in columns],
                               "rows": [[cell_text(item.get(c["key"]), c) for c in columns] for item in value],
                               "widths": [c.get("width", 1) for c in columns]})
            else:
                blocks.append({"type": "paragraph", "text": "해당 없음"})
        elif kind == "keyvalue":
            blocks.append({"type": "paragraph", "text": keyvalue_text(value)})
        else:
            blocks.append({"type": "notice" if s["fill"] == "fixed" else "paragraph", "text": str(value)})
    document = {"schema_version": 1, "title": title, "page": form.page,
                "template": {"id": form.form_id, "name": form.name, "version": form.version}, "blocks": blocks}
    if isinstance(data.get("as_of"), str):
        document["base_date"] = data["as_of"]
    return document


def new_document_id() -> str:
    return uuid4().hex  # 시장 리포트 번호와 같은 32자리 형식


def _session_uuid(session_id: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return None


_PUBLIC_ID = re.compile(r"[0-9a-f]{32}")


@dataclass
class DocumentStore:
    """저장된 문서·내려받은 기록·시장 조회 기록 (로컬 SQLite).

    문서 레코드: {"document_id", "owner_id", "kind", "created_at", "has_customer_info", "document": 공통 문서 형식, ...}
    """

    path: Path
    forms: dict[str, DocumentForm] = field(default_factory=dict)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                record TEXT NOT NULL,
                has_customer_info INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS document_downloads (
                document_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                format TEXT NOT NULL,
                downloaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_records (
                kind TEXT NOT NULL,
                record_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (kind, record_id)
            );
        """)
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _put(self, record: dict) -> dict:
        record = {"created_at": self._now(), "has_customer_info": False, **record}
        title = (record.get("document") or {}).get("title") or record.get("title") or "문서"
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["document_id"], record["owner_id"], record["kind"], title,
                 json.dumps(record, ensure_ascii=False), int(record["has_customer_info"]), record["created_at"]),
            )
        return record

    async def put(self, record: dict, *, session_id: str | None = None) -> dict:
        """문서 레코드 저장. document_id·owner_id·kind 가 있어야 한다."""
        return await asyncio.to_thread(self._put, record)

    async def save(self, owner_id: str, document: dict, *, form_id: str, has_customer_info: bool,
                   session_id: str | None = None) -> dict:
        return await self.put({"document_id": new_document_id(), "owner_id": owner_id, "kind": "form", "form_id": form_id,
                               "has_customer_info": has_customer_info, "document": document}, session_id=session_id)

    def _list(self, owner_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT document_id, kind, title, created_at FROM documents WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def list(self, owner_id: str) -> list[dict]:
        return await asyncio.to_thread(self._list, owner_id)

    def _get(self, owner_id: str, document_id: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record FROM documents WHERE document_id = ? AND owner_id = ?", (document_id, owner_id),
            ).fetchone()
        return json.loads(row["record"]) if row else None

    async def get(self, owner_id: str, document_id: str) -> dict | None:
        """다른 사람의 문서는 없는 것과 같다 (None → 404)."""
        return await asyncio.to_thread(self._get, owner_id, document_id)

    def _delete(self, owner_id: str, document_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute("DELETE FROM documents WHERE document_id = ? AND owner_id = ?", (document_id, owner_id))
        return cursor.rowcount > 0

    async def delete(self, owner_id: str, document_id: str) -> bool:
        """내 문서함에서 지운다. 본인 문서만 (남의 문서·없는 문서는 False → 404)."""
        return await asyncio.to_thread(self._delete, owner_id, document_id)

    def _log_download(self, owner_id: str, document_id: str, fmt: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT INTO document_downloads VALUES (?, ?, ?, ?)",
                               (document_id, owner_id, fmt, self._now()))

    async def log_download(self, owner_id: str, document_id: str, fmt: str) -> None:
        await asyncio.to_thread(self._log_download, owner_id, document_id, fmt)

    # 문서가 되기 전의 작업 기록 (시장 조회 결과·양식 선택). 문서 근거로 남기려고 본인 것만 읽는다.
    def _put_record(self, kind: str, record_id: str, owner_id: str, payload: dict) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO work_records VALUES (?, ?, ?, ?, ?)",
                               (kind, record_id, owner_id, json.dumps(payload, ensure_ascii=False), self._now()))

    async def put_record(self, kind: str, record_id: str, owner_id: str, payload: dict) -> None:
        await asyncio.to_thread(self._put_record, kind, record_id, owner_id, payload)

    def _get_record(self, kind: str, record_id: str, owner_id: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM work_records WHERE kind = ? AND record_id = ? AND owner_id = ?",
                (kind, record_id, owner_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    async def get_record(self, kind: str, record_id: str, owner_id: str) -> dict | None:
        return await asyncio.to_thread(self._get_record, kind, record_id, owner_id)


class DbDocumentStore:
    """PostgreSQL output_file + 파일 저장소. 문서 JSON 은 outputs/{login_id}/{document_id}.json,
    작업 기록(시장 조회 결과·양식 선택)은 outputs/{login_id}/work/{kind}/{id}.json 에 둔다."""

    def __init__(self, sessions, storage, forms: dict[str, DocumentForm] | None = None) -> None:
        self._sessions, self.storage, self.forms = sessions, storage, forms or {}

    @staticmethod
    def _owner_key(owner_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", owner_id or ""):
            raise ValueError("사용자 ID 형식이 아닙니다.")
        return owner_id

    async def put(self, record: dict, *, session_id: str | None = None) -> dict:
        record = {"created_at": datetime.now(timezone.utc).isoformat(), "has_customer_info": False, **record}
        document_id = record["document_id"]
        if not _PUBLIC_ID.fullmatch(document_id):
            raise ValueError("문서 번호 형식이 아닙니다.")
        title = ((record.get("document") or {}).get("title") or record.get("title") or "문서")[:200]
        key = f"outputs/{self._owner_key(record['owner_id'])}/{document_id}.json"
        data = json.dumps(record, ensure_ascii=False).encode("utf-8")
        stored = await asyncio.to_thread(self.storage.save, key, data)
        try:
            async with self._sessions() as s:
                inserted = await s.scalar(text("""
                    INSERT INTO output_file (owner_user_id, session_id, title, file_name, mime_type, storage_path, sha256,
                                             has_customer_info, public_id, kind)
                    SELECT u.id, (SELECT id FROM chat_session WHERE id = :s), :t, :f, 'application/json', :k, :h, :c, :p, :kind
                    FROM users u WHERE u.login_id = :u RETURNING id"""),
                    {"u": record["owner_id"], "s": _session_uuid(session_id), "t": title, "f": document_id + ".json",
                     "k": stored.key, "h": stored.sha256, "c": bool(record["has_customer_info"]), "p": document_id,
                     "kind": record["kind"]})
                if inserted is None:
                    raise ValueError(f"DB users 표에 없는 사용자입니다: {record['owner_id']}")
                await s.commit()
        except Exception:
            await asyncio.to_thread(self.storage.delete, key)  # 표에 못 넣은 파일은 남기지 않는다
            raise
        return record

    async def save(self, owner_id: str, document: dict, *, form_id: str, has_customer_info: bool,
                   session_id: str | None = None) -> dict:
        return await self.put({"document_id": new_document_id(), "owner_id": owner_id, "kind": "form", "form_id": form_id,
                               "has_customer_info": has_customer_info, "document": document}, session_id=session_id)

    async def list(self, owner_id: str) -> list[dict]:
        async with self._sessions() as s:
            rows = (await s.execute(text("""
                SELECT o.public_id, o.kind, o.title, o.created_at FROM output_file o JOIN users u ON u.id = o.owner_user_id
                WHERE u.login_id = :u AND o.deleted_at IS NULL AND o.public_id IS NOT NULL
                ORDER BY o.created_at DESC"""), {"u": owner_id})).all()
        return [{"document_id": r.public_id, "kind": r.kind, "title": r.title, "created_at": r.created_at.isoformat()}
                for r in rows]

    async def _row(self, owner_id: str, document_id: str):
        if not _PUBLIC_ID.fullmatch(document_id or ""):
            return None
        async with self._sessions() as s:
            return (await s.execute(text("""
                SELECT o.id, o.storage_path, o.sha256, u.id AS user_pk FROM output_file o JOIN users u ON u.id = o.owner_user_id
                WHERE u.login_id = :u AND o.public_id = :p AND o.deleted_at IS NULL"""),
                {"u": owner_id, "p": document_id})).first()

    async def get(self, owner_id: str, document_id: str) -> dict | None:
        """다른 사람의 문서는 없는 것과 같다 (None → 404). 파일 해시가 표와 다르면 읽지 않는다."""
        row = await self._row(owner_id, document_id)
        if row is None:
            return None
        data = await asyncio.to_thread(self.storage.read, row.storage_path)
        if hashlib.sha256(data).hexdigest() != row.sha256:
            raise ValueError("저장된 문서 파일이 바뀌어 열 수 없습니다.")
        return json.loads(data)

    async def delete(self, owner_id: str, document_id: str) -> bool:
        """내 문서함에서 지운다. 행은 지우지 않고 output_file.deleted_at(지운 시각)만 채운다 (파일·내려받은 기록은 남음).
        본인 문서만 (남의 문서·없는 문서는 False → 404)."""
        row = await self._row(owner_id, document_id)
        if row is None:
            return False
        async with self._sessions() as s:
            await s.execute(text("UPDATE output_file SET deleted_at = now() WHERE id = :i AND deleted_at IS NULL"), {"i": row.id})
            await s.commit()
        return True

    async def log_download(self, owner_id: str, document_id: str, fmt: str) -> None:
        row = await self._row(owner_id, document_id)
        if row is None:
            return
        async with self._sessions() as s:
            await s.execute(text("""
                INSERT INTO audit_log (event_type, user_id, arguments, executed, target_type, target_id)
                VALUES ('output_download', :u, CAST(:a AS JSONB), true, 'output_file', :t)"""),
                {"u": row.user_pk, "a": json.dumps({"document_id": document_id, "format": fmt}), "t": row.id})
            await s.commit()

    def _record_key(self, kind: str, record_id: str, owner_id: str) -> str:
        if not re.fullmatch(r"[a-z_]{1,40}", kind) or not _PUBLIC_ID.fullmatch(record_id or ""):
            raise ValueError("작업 기록 번호 형식이 아닙니다.")
        return f"outputs/{self._owner_key(owner_id)}/work/{kind}/{record_id}.json"

    async def put_record(self, kind: str, record_id: str, owner_id: str, payload: dict) -> None:
        key = self._record_key(kind, record_id, owner_id)
        data = json.dumps({**payload, "owner_user_id": owner_id}, ensure_ascii=False).encode("utf-8")
        await asyncio.to_thread(self.storage.save, key, data, True)

    async def get_record(self, kind: str, record_id: str, owner_id: str) -> dict | None:
        try:
            key = self._record_key(kind, record_id, owner_id)
            data = await asyncio.to_thread(self.storage.read, key)
        except (ValueError, OSError):
            return None
        payload = json.loads(data)
        return payload if payload.get("owner_user_id") == owner_id else None
