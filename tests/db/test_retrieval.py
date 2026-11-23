"""벡터 검색(knowledge.retrieval) 확인.

- SQL 모양 테스트: DB 없이 항상 돈다 (권한·문서 상태 조건이 빠지지 않았는지).
- 실제 DB 테스트: .env 의 DATABASE_URL + 마이그레이션 끝난 DB 일 때만. 트랜잭션 안에서 하고 되돌린다.
임베딩은 가짜(test_indexer 의 FakeEmbedder), 가상 글만 쓴다.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import EMBEDDING_DIM
from knowledge.ingestion.indexer import index_doc_version
from knowledge.retrieval import build_query, search_for_principal
from tests.db.test_indexer import COUNT, FakeEmbedder, parsed_law
from tests.db.test_schema import _user, conn  # noqa: F401


def _sql(**kw) -> str:
    q = build_query([0.0] * EMBEDDING_DIM, **kw)
    return str(q.compile(dialect=postgresql.dialect()))


def test_query_has_permission_and_status_filters():
    s = _sql(dept_id=7, security_grade=3, k=5)
    for part in ("chunk.security_grade <=", "chunk.scope =", "chunk.owner_dept_id =", "doc_version.is_current IS true",
                 "doc_version.status =", "doc.status =", "<=>", "LIMIT"):
        assert part in s, part


def test_no_dept_means_company_only():
    s = _sql(dept_id=None, security_grade=3, k=5)
    assert "chunk.owner_dept_id" not in s and "chunk.scope =" in s


async def test_principal_without_grade_sees_nothing():
    p = SimpleNamespace(attributes={"dept_id": 1})
    assert await search_for_principal(None, "아무거나", p, embedder=FakeEmbedder()) == []


class QueryEmbedder(FakeEmbedder):
    def embed_query(self, t):
        return self.embed_docs([t])[0]


async def _doc(conn, uid, dept, scope, grade, current=True):  # noqa: F811
    doc_id = await conn.scalar(text(
        "INSERT INTO doc (owner_user_id, owner_dept_id, title, category, scope, security_grade) "
        "VALUES (:u, :d, '가상 규정', '내규', :s, :g) RETURNING id"), {"u": uid, "d": dept, "s": scope, "g": grade})
    return await conn.scalar(text(
        "INSERT INTO doc_version (doc_id, version, is_current, file_name, mime_type, storage_path, sha256) "
        "VALUES (:d, 1, :c, '가상.hwpx', 'application/hwp+zip', 'docs/가상.hwpx', :h) RETURNING id"),
        {"d": doc_id, "c": current, "h": "0" * 64})


async def test_search_respects_permissions_in_real_db(conn):  # noqa: F811
    uid = await _user(conn)
    dept = await conn.scalar(text("SELECT dept_id FROM users WHERE id = :u"), {"u": uid})
    other = await conn.scalar(text("INSERT INTO dept (name) VALUES ('다른 부서') RETURNING id"))
    emb = QueryEmbedder()
    async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
        mine = await _doc(conn, uid, dept, "dept", 2)          # 내 부서 문서
        theirs = await _doc(conn, uid, other, "dept", 2)       # 다른 부서 문서 → 안 보여야 함
        secret = await _doc(conn, uid, dept, "company", 5)     # 등급 5 → 등급 2 사용자에게 안 보여야 함
        old = await _doc(conn, uid, dept, "company", 1, current=False)   # 옛 버전 → 안 보여야 함
        for v in (mine, theirs, secret, old):
            await index_doc_version(session, v, parsed_law(), embedder=emb, count_tokens=COUNT)
        target = (await session.execute(text("SELECT content FROM chunk WHERE doc_version_id = :v ORDER BY chunk_index LIMIT 1"),
                                        {"v": mine})).scalar()
        p = SimpleNamespace(attributes={"dept_id": dept, "security_grade": 2})
        hits = await search_for_principal(session, target, p, embedder=emb, k=50)
        assert hits and {h["doc_version_id"] for h in hits} == {mine}
        assert hits[0]["content"] == target and hits[0]["score"] > 0.99      # 같은 글이 1등
