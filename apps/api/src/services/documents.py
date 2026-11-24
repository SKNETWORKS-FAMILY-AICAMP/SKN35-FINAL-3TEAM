"""문서 양식(JSON)과 저장된 문서. 모든 서류(양식 문서·시장 리포트)가 이 저장소 하나에 저장되고 같은 내려받기를 쓴다.

양식은 config/document_forms/*.json 에 둔다. 칸(section)마다 누가 채우는지 적는다.
- data : 코드가 조회 결과(source.provider)에서 그대로 옮긴다. 숫자·표는 모두 여기.
- llm  : 모델이 문장을 쓴다 (fill_document 툴 인자). 길이 제한, 숫자 검사(check_numbers) 가능.
- fixed: 양식에 적힌 고정 문구.
채운 결과는 공통 문서 형식(blocks, services/document_render.py)으로 저장한다.
시장 리포트(config/report_templates, 시장 MCP 코드)도 같은 형식이고, 같은 저장소에 들어간다 (MarketRecords).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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


@dataclass
class DocumentStore:
    """저장된 문서·내려받은 기록·시장 조회 기록. ERD 의 output_file 에 해당 (DB 가 붙으면 이 클래스만 바꾼다).

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

    def put(self, record: dict) -> dict:
        """문서 레코드 저장. document_id·owner_id·kind 가 있어야 한다."""
        record = {"created_at": self._now(), "has_customer_info": False, **record}
        title = (record.get("document") or {}).get("title") or record.get("title") or "문서"
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["document_id"], record["owner_id"], record["kind"], title,
                 json.dumps(record, ensure_ascii=False), int(record["has_customer_info"]), record["created_at"]),
            )
        return record

    def save(self, owner_id: str, document: dict, *, form_id: str, has_customer_info: bool) -> dict:
        return self.put({"document_id": new_document_id(), "owner_id": owner_id, "kind": "form", "form_id": form_id,
                         "has_customer_info": has_customer_info, "document": document})

    def list(self, owner_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT document_id, kind, title, created_at FROM documents WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, owner_id: str, document_id: str) -> dict | None:
        """다른 사람의 문서는 없는 것과 같다 (None → 404)."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record FROM documents WHERE document_id = ? AND owner_id = ?", (document_id, owner_id),
            ).fetchone()
        return json.loads(row["record"]) if row else None

    def log_download(self, owner_id: str, document_id: str, fmt: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT INTO document_downloads VALUES (?, ?, ?, ?)",
                               (document_id, owner_id, fmt, self._now()))

    # 문서가 되기 전의 작업 기록 (시장 조회 결과·양식 선택). 문서 근거로 남기려고 본인 것만 읽는다.
    def put_record(self, kind: str, record_id: str, owner_id: str, payload: dict) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO work_records VALUES (?, ?, ?, ?, ?)",
                               (kind, record_id, owner_id, json.dumps(payload, ensure_ascii=False), self._now()))

    def get_record(self, kind: str, record_id: str, owner_id: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM work_records WHERE kind = ? AND record_id = ? AND owner_id = ?",
                (kind, record_id, owner_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None
