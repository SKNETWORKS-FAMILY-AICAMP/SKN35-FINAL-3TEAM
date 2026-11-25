"""사내 문서 목록 API (/api/knowledge/docs). DB 없이 도는 부분: 설정에 따라 켜고 끄기, 사용자 등급 전달."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import routers.knowledge_docs as knowledge_docs
from tests.test_api import KIM, make


async def test_off_unless_doc_store_is_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_STORE", "file")
    client, _ = make([], tmp_path)
    r = await client.get("/api/knowledge/docs", headers=KIM)
    assert r.status_code == 200 and r.json() == {"enabled": False, "docs": []}


async def test_lists_company_docs_with_user_grade(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_STORE", "db")
    seen = {}

    @asynccontextmanager
    async def no_session():
        yield None

    async def fake_list(session, *, security_grade):
        seen["grade"] = security_grade
        return [{"doc_id": 1, "title": "기업은행 예금규정", "category": "내규", "file_name": "예금규정.hwp",
                 "registered_at": datetime(2026, 11, 23, tzinfo=timezone.utc), "chunks": 81}]

    monkeypatch.setattr(knowledge_docs, "_session_factory", no_session)
    monkeypatch.setattr(knowledge_docs, "list_company_docs", fake_list)
    client, _ = make([], tmp_path)
    body = (await client.get("/api/knowledge/docs", headers=KIM)).json()
    assert seen["grade"] == 2                                   # kim 의 보안등급
    assert body["enabled"] and body["docs"][0]["title"] == "기업은행 예금규정" and body["docs"][0]["chunks"] == 81


async def test_needs_login(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_STORE", "db")
    client, _ = make([], tmp_path)
    assert (await client.get("/api/knowledge/docs")).status_code in (400, 401)
