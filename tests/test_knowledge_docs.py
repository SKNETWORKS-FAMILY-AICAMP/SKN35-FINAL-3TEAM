"""사내 문서 목록 API (/api/knowledge/docs). DB 없이 도는 부분: 설정에 따라 켜고 끄기, 사용자 등급 전달."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import routers.knowledge_docs as knowledge_docs
from contracts.permission import Principal
from tests.test_api import KIM, make


async def test_off_unless_doc_store_is_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_STORE", "file")
    client, _ = make([], tmp_path)
    r = await client.get("/api/knowledge/docs", headers=KIM)
    assert r.status_code == 200 and r.json() == {"enabled": False, "docs": [], "admin_view": False}


async def test_lists_docs_with_user_dept_and_grade(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_STORE", "db")
    seen = {}

    @asynccontextmanager
    async def no_session():
        yield None

    async def fake_list(session, *, dept_id, security_grade, admin):
        seen.update(dept=dept_id, grade=security_grade, admin=admin)
        return [{"doc_id": 1, "title": "기업은행 예금규정", "category": "내규", "file_name": "예금규정.hwp",
                 "registered_at": datetime(2026, 11, 23, tzinfo=timezone.utc), "chunks": 81,
                 "scope": "company", "dept_id": 1, "dept_name": "부서 1", "searchable": True},
                {"doc_id": 2, "title": "개인채무자 FAQ", "category": "공문", "file_name": "FAQ.hwpx",
                 "registered_at": datetime(2026, 11, 23, tzinfo=timezone.utc), "chunks": 12,
                 "scope": "dept", "dept_id": 10, "dept_name": "부서 10", "searchable": True}]

    monkeypatch.setattr(knowledge_docs, "_session_factory", no_session)
    monkeypatch.setattr(knowledge_docs, "list_docs", fake_list)
    client, _ = make([], tmp_path)
    body = (await client.get("/api/knowledge/docs", headers=KIM)).json()
    assert seen == {"dept": 10, "grade": 2, "admin": False}     # kim: 부서 10, 보안등급 2, 관리자 아님
    assert body["enabled"] and not body["admin_view"]
    assert body["docs"][0]["chunks"] == 81 and body["docs"][1]["scope"] == "dept" and body["docs"][1]["dept_name"] == "부서 10"


async def test_admin_gets_full_list_flag(tmp_path, monkeypatch):
    """관리자는 목록 전체 (admin=True 로 부른다). 내용 검색 범위는 바뀌지 않는다 (catalog.py searchable)."""
    monkeypatch.setenv("DOC_STORE", "db")
    seen = {}

    @asynccontextmanager
    async def no_session():
        yield None

    async def fake_list(session, *, dept_id, security_grade, admin):
        seen.update(dept=dept_id, grade=security_grade, admin=admin)
        return []

    monkeypatch.setattr(knowledge_docs, "_session_factory", no_session)
    monkeypatch.setattr(knowledge_docs, "list_docs", fake_list)
    client, svc = make([], tmp_path)
    svc.users._users["boss"] = Principal("boss", ("admin",), {"dept_id": 1, "security_grade": 5})
    body = (await client.get("/api/knowledge/docs", headers={"X-User-Id": "boss"})).json()
    assert seen == {"dept": 1, "grade": 5, "admin": True} and body["admin_view"] is True


async def test_needs_login(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_STORE", "db")
    client, _ = make([], tmp_path)
    assert (await client.get("/api/knowledge/docs")).status_code in (400, 401)
