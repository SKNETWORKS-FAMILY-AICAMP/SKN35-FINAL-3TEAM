"""색인(파서 결과 → 청크 → 임베딩 → chunk 표) 확인.

- 가짜 세션 테스트: DB 없이 항상 돈다 (칸 매핑·권한 상속·상태값).
- 실제 DB 테스트: test_schema.py 와 같은 조건(.env 의 DATABASE_URL, 마이그레이션 끝난 DB)일 때만. 트랜잭션 안에서 하고 되돌린다.
임베딩은 가짜(글로 정해지는 단위 벡터), 길이 세기는 글자 수 — 모델 파일 없이 돈다. 가상 글만 쓴다.
"""

from __future__ import annotations

import hashlib
import math
import random

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.ingestion.indexer import chunk_rows, index_doc_version
from db.models import EMBEDDING_DIM, Chunk, Doc, DocVersion
from tests.db.test_schema import _user, conn  # noqa: F401  (conn 은 pytest fixture)

COUNT = lambda t: len(t) if t else 0  # noqa: E731


class FakeEmbedder:
    def embed_docs(self, texts):
        out = []
        for t in texts:
            r = random.Random(hashlib.sha1(t.encode("utf-8")).digest())
            v = [r.gauss(0, 1) for _ in range(EMBEDDING_DIM)]
            n = math.sqrt(sum(x * x for x in v))
            out.append([x / n for x in v])
        return out


def parsed_law():
    el = lambda t, s: {"type": t, "text": s, "page": 1, "pages": [1], "bbox": None, "heading_path": []}  # noqa: E731
    body = "회원은 대출 연체가 생기면 7일 안에 통지를 받는다. " * 6
    return {"status": "ok", "file_name": "가상_여신규정.hwpx",
            "elements": [el("title", "가상 여신규정"), el("section_header", "제1장 총칙"),
                         el("text", "제1조(목적) " + body), el("text", "제2조(연체 통지) " + body),
                         el("section_header", "제2장 상환"), el("text", "제3조(상환 방법) " + body)]}


class FakeSession:
    """index_doc_version 이 쓰는 메서드만 흉내."""
    def __init__(self):
        self.doc = Doc(id=5, owner_user_id=1, owner_dept_id=9, title="가상", category="내규", scope="dept", security_grade=3)
        self.dv = DocVersion(id=11, doc_id=5, version=1, file_name="가상.hwpx", mime_type="x", storage_path="x",
                                  sha256="0" * 64, status="processing")
        self.added, self.deleted = [], 0

    async def get(self, model, pk):
        return {(Doc, 5): self.doc, (DocVersion, 11): self.dv}.get((model, pk))

    async def execute(self, stmt):
        self.deleted += 1

    def add_all(self, rows):
        self.added.extend(rows)

    async def flush(self):
        pass


async def test_index_maps_chunks_to_rows_with_inherited_permissions():
    s = FakeSession()
    n = await index_doc_version(s, 11, parsed_law(), embedder=FakeEmbedder(), count_tokens=COUNT)
    assert n == len(s.added) > 0
    assert s.dv.status == "ready" and s.deleted == 1
    assert [r.chunk_index for r in s.added] == list(range(n))
    assert all(r.doc_version_id == 11 and (r.owner_dept_id, r.scope, r.security_grade) == (9, "dept", 3) for r in s.added)
    assert all(len(r.embedding) == EMBEDDING_DIM for r in s.added)
    assert any(r.heading_path and r.heading_path[-1].startswith("제2조(연체 통지)") for r in s.added)


async def test_failed_parse_marks_version_failed():
    s = FakeSession()
    assert await index_doc_version(s, 11, {"status": "failed", "elements": []}, embedder=FakeEmbedder()) == 0
    assert s.dv.status == "failed" and not s.added


def test_vector_dimension_must_match_db():
    c = {"doc_version_id": 1, "chunk_index": 0, "page": None, "content": "가", "owner_dept_id": 1, "scope": "dept",
         "security_grade": 1, "heading_path": []}
    with pytest.raises(ValueError, match="차원"):
        chunk_rows([c], [[0.0] * (EMBEDDING_DIM + 1)])


async def test_index_into_real_db_and_search(conn):  # noqa: F811
    """실제 chunk 표에 넣고, pgvector 코사인 거리로 같은 글을 1등으로 찾는다."""
    uid = await _user(conn)
    dept = await conn.scalar(text("SELECT dept_id FROM users WHERE id = :u"), {"u": uid})
    doc_id = await conn.scalar(text(
        "INSERT INTO doc (owner_user_id, owner_dept_id, title, category, scope, security_grade) "
        "VALUES (:u, :d, '가상 여신규정', '내규', 'dept', 2) RETURNING id"), {"u": uid, "d": dept})
    dv_id = await conn.scalar(text(
        "INSERT INTO doc_version (doc_id, version, file_name, mime_type, storage_path, sha256) "
        "VALUES (:d, 1, '가상.hwpx', 'application/hwp+zip', 'docs/가상.hwpx', :h) RETURNING id"), {"d": doc_id, "h": "0" * 64})
    async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as session:
        emb = FakeEmbedder()
        n = await index_doc_version(session, dv_id, parsed_law(), embedder=emb, count_tokens=COUNT)
        rows = (await session.scalars(select(Chunk).where(Chunk.doc_version_id == dv_id).order_by(Chunk.chunk_index))).all()
        assert len(rows) == n > 0
        assert all((r.owner_dept_id, r.scope, r.security_grade) == (dept, "dept", 2) for r in rows)
        assert (await session.get(DocVersion, dv_id)).status == "ready"
        target = rows[-1]
        q = emb.embed_docs([target.content])[0]                  # 가짜 임베딩: 같은 글 = 같은 벡터
        q_doc = (await session.scalars(select(Chunk).where(Chunk.doc_version_id == dv_id)
                                       .order_by(Chunk.embedding.cosine_distance(q)).limit(1))).first()
        assert q_doc is not None
        # 다시 색인하면 그 버전의 청크를 갈아 끼운다(늘어나지 않음)
        n2 = await index_doc_version(session, dv_id, parsed_law(), embedder=emb, count_tokens=COUNT)
        assert n2 == n
        assert await session.scalar(text("SELECT count(*) FROM chunk WHERE doc_version_id = :v"), {"v": dv_id}) == n
