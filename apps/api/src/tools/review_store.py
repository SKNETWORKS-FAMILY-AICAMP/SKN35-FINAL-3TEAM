"""Local review-system adapter shared by lookup and submission tools."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from contracts.permission import Principal


class ReviewCase(BaseModel):
    model_config = ConfigDict(strict=True)

    case_id: str = Field(min_length=1)
    applicant_name: str
    product_name: str
    requested_amount: int = Field(ge=0)
    status: str
    dept_id: int
    security_grade: int = Field(ge=0)
    submitted_documents: list[str]
    missing_documents: list[str]


def load_review_cases(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as file:
        cases = json.load(file)
    if not isinstance(cases, list):
        raise ValueError("심사 데이터는 리스트여야 합니다.")
    validated = [ReviewCase.model_validate(case).model_dump() for case in cases]
    ids = [case["case_id"] for case in validated]
    if len(ids) != len(set(ids)):
        raise ValueError("중복된 심사 건 번호가 있습니다.")
    return validated


def find_review_case(cases: list[dict], case_id: str) -> dict | None:
    return next((case for case in cases if case["case_id"] == case_id), None)


def can_read_case(case: dict, principal: Principal) -> bool:
    department = principal.attributes.get("dept_id")
    grade = principal.attributes.get("security_grade")
    return (
        type(department) is int
        and department == case["dept_id"]
        and type(grade) is int
        and grade >= case["security_grade"]
    )


class ReviewStore:
    def __init__(self, case_path: Path, request_path: Path) -> None:
        self.case_path = case_path
        self.request_path = request_path

    def visible_case(self, case_id: str, principal: Principal) -> dict:
        case = find_review_case(load_review_cases(self.case_path), case_id)
        if case is None or not can_read_case(case, principal):
            raise ValueError("조회 가능한 심사 건을 찾을 수 없습니다.")
        return case

    def _connect(self) -> sqlite3.Connection:
        self.request_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.request_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS review_requests (
                    request_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL UNIQUE,
                    requested_by TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    case_snapshot TEXT NOT NULL
                )
            """)
        except BaseException:
            connection.close()
            raise
        return connection

    def get_case(self, case_id: str, principal: Principal) -> dict:
        case = self.visible_case(case_id, principal)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT request_id, requested_by, notes, created_at, status "
                "FROM review_requests WHERE case_id = ?", (case_id,),
            ).fetchone()
        return {**case, "review_request": dict(row) if row else None}

    def submit(self, case_id: str, notes: str, principal: Principal) -> dict:
        # Recheck record access after the approval wait.
        case = self.visible_case(case_id, principal)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM review_requests WHERE case_id = ?", (case_id,),
            ).fetchone()
            if existing:
                if existing["requested_by"] != principal.user_id or existing["notes"] != notes:
                    raise ValueError("이미 등록된 심사 요청입니다. 조회 도구로 확인해 주세요.")
                return self._submission_result(existing, already_registered=True)

            request_id = "REQ-" + uuid4().hex
            created_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO review_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
                (request_id, case_id, principal.user_id, notes, created_at,
                 "registered", json.dumps(case, ensure_ascii=False)),
            )
            row = connection.execute(
                "SELECT * FROM review_requests WHERE request_id = ?", (request_id,),
            ).fetchone()
            return self._submission_result(row, already_registered=False)

    @staticmethod
    def _submission_result(row: sqlite3.Row, *, already_registered: bool) -> dict:
        return {
            key: row[key]
            for key in ("request_id", "case_id", "requested_by", "notes", "created_at", "status")
        } | {
            "already_registered": already_registered,
            "storage": "local_demo",
            "missing_documents": json.loads(row["case_snapshot"])["missing_documents"],
        }
