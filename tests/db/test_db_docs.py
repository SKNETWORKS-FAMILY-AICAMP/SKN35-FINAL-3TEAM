"""DB 문서 도구(tools/db_docs.py) 확인.

- 항상 도는 테스트: 결과 모양이 파일 도구(search_doc·get_doc_chunk)와 같은지, 권한 없는 사용자·잘못된 입력 처리.
- 실제 DB 테스트: .env 의 DATABASE_URL + 마이그레이션 끝난 DB 일 때만. 트랜잭션 안에서 하고 되돌린다.
"""

from __future__ import annotations

import json

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import knowledge.retrieval as retrieval
from contracts.permission import Principal, ToolCall
from knowledge.ingestion.indexer import index_doc_version
from tests.db.test_indexer import COUNT, parsed_law
from tests.db.test_retrieval import QueryEmbedder, _doc
from tests.db.test_schema import _user, conn  # noqa: F401
from tools.db_docs import BackgroundEmbedder, DbReadDocTool, DbSearchDocsTool
from tools.read_doc import READ_DOC_SPEC
from tools.search_doc import PREVIEW_LENGTH, SEARCH_DOC_SPEC

SEARCH_KEYS = {"doc_id", "chunk_id", "title", "location", "preview", "is_truncated"}
READ_KEYS = {"doc_id", "chunk_id", "title", "location", "text"}


def _call(args: dict, attrs: dict) -> ToolCall:
    return ToolCall(call_id="c1", tool_name="x", arguments=args, principal=Principal(user_id="u", attributes=attrs),
                    session_id="s1")


@asynccontextmanager
async def _no_session():
    yield None


ROW = {"chunk_id": 42, "doc_id": 7, "doc_version_id": 9, "title": "가상 여신규정", "file_name": "가상.hwpx",
       "chunk_index": 0, "page": 3, "heading_path": ["가상 여신규정", "제2장 여신", "제12조(사후관리)"],
       "content": "가" * (PREVIEW_LENGTH + 50), "score": 0.9}


def test_location_falls_back_to_page_then_paragraph():
    from tools.db_docs import _location
    assert _location({**ROW, "heading_path": ["가상 여신규정"]}) == "3쪽"
    assert _location({**ROW, "heading_path": [], "page": None, "chunk_index": 4}) == "5번째 문단"


def test_same_tool_names_as_file_tools():
    assert DbSearchDocsTool.spec is SEARCH_DOC_SPEC and DbReadDocTool.spec is READ_DOC_SPEC


async def test_search_result_shape(monkeypatch):
    async def fake_search(session, q, *, dept_id, security_grade, k):
        assert (dept_id, security_grade, k) == (10, 2, 5) and isinstance(q, list)
        return [ROW]
    monkeypatch.setattr(retrieval, "search_chunks", fake_search)
    tool = DbSearchDocsTool(QueryEmbedder(), session_factory=_no_session)
    r = await tool.run(_call({"query": " 사후관리 "}, {"dept_id": 10, "security_grade": 2}))
    assert r.ok and r.data["query"] == "사후관리"
    m = r.data["matches"][0]
    assert set(m) == SEARCH_KEYS
    assert (m["doc_id"], m["chunk_id"], m["location"]) == ("7", "42", "제2장 여신 > 제12조(사후관리)")
    assert len(m["preview"]) == PREVIEW_LENGTH and m["is_truncated"] is True


async def test_search_without_grade_returns_nothing(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("권한 없는 사용자는 DB 를 부르지 않아야 함")
    monkeypatch.setattr(retrieval, "search_chunks", boom)
    tool = DbSearchDocsTool(QueryEmbedder(), session_factory=_no_session)
    r = await tool.run(_call({"query": "아무거나"}, {"dept_id": 10}))
    assert r.ok and r.data["matches"] == []


async def test_search_rejects_bad_query():
    tool = DbSearchDocsTool(QueryEmbedder(), session_factory=_no_session)
    assert not (await tool.run(_call({"query": "  "}, {"security_grade": 1}))).ok
    assert not (await tool.run(_call({"query": 3}, {"security_grade": 1}))).ok


async def test_read_result_shape_and_hidden_chunks(monkeypatch):
    async def fake_get(session, chunk_id, *, dept_id, security_grade):
        return ROW if chunk_id == 42 else None
    monkeypatch.setattr(retrieval, "get_chunk", fake_get)
    tool = DbReadDocTool(session_factory=_no_session)
    r = await tool.run(_call({"chunk_id": "42"}, {"dept_id": 10, "security_grade": 2}))
    assert r.ok and set(r.data) == READ_KEYS and r.data["text"] == ROW["content"]
    for bad in ("43", "doc-001-01", ""):                       # 없는 문단, 파일 예시 ID, 빈 값
        assert not (await tool.run(_call({"chunk_id": bad}, {"dept_id": 10, "security_grade": 2}))).ok


def test_background_embedder_reports_load_failure():
    def broken():
        raise OSError("모델 폴더 없음")
    e = BackgroundEmbedder(broken)
    try:
        e.embed_query("가")
    except RuntimeError as err:
        assert "모델 폴더 없음" in str(err)
    else:
        raise AssertionError("실패를 알려야 함")


async def test_tools_in_real_db(conn):  # noqa: F811
    uid = await _user(conn)
    dept = await conn.scalar(text("SELECT dept_id FROM users WHERE id = :u"), {"u": uid})
    other = await conn.scalar(text("INSERT INTO dept (name) VALUES ('다른 부서') RETURNING id"))
    emb = QueryEmbedder()
    async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
        mine = await _doc(conn, uid, dept, "dept", 2)
        theirs = await _doc(conn, uid, other, "dept", 2)
        for v in (mine, theirs):
            await index_doc_version(session, v, parsed_law(), embedder=emb, count_tokens=COUNT)
        tid, target, tvec = (await session.execute(text("SELECT id, content, embedding FROM chunk WHERE doc_version_id = :v "
                                                        "ORDER BY chunk_index LIMIT 1"), {"v": mine})).one()
        hidden = (await session.execute(text("SELECT id FROM chunk WHERE doc_version_id = :v LIMIT 1"), {"v": theirs})).scalar()

        @asynccontextmanager
        async def same_session():
            yield session

        me = {"dept_id": dept, "security_grade": 2}
        class FixedQuery:                 # 질문 벡터 = 찾을 청크의 저장된 벡터
            def embed_query(self, _):
                return json.loads(tvec)   # 글자 '[…]' 로 옴

        search = DbSearchDocsTool(FixedQuery(), session_factory=same_session, top_k=50)
        r = await search.run(_call({"query": "연체 통지"}, me))
        assert r.ok and r.data["matches"] and r.data["matches"][0]["chunk_id"] == str(tid)
        assert r.data["matches"][0]["preview"] == target[:PREVIEW_LENGTH]
        ids = {m["chunk_id"] for m in r.data["matches"]}
        assert str(hidden) not in ids                                   # 다른 부서 문서는 안 보임

        read = DbReadDocTool(session_factory=same_session)
        ok = await read.run(_call({"chunk_id": r.data["matches"][0]["chunk_id"]}, me))
        assert ok.ok and ok.data["text"] == target
        assert not (await read.run(_call({"chunk_id": str(hidden)}, me))).ok   # 다른 부서 문단은 못 엶
