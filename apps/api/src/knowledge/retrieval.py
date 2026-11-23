# -*- coding: utf-8 -*-
"""사내 문서 벡터 검색: 질문 → KURE-v3 질문 벡터 → 팀 DB chunk 표(pgvector)에서 코사인 거리 순.

    from knowledge.retrieval import search_chunks, search_for_principal
    hits = await search_for_principal(session, "연체되면 언제 알려줘야 해?", principal, embedder=embedder, k=5)

권한(사용자가 볼 수 있는 청크만) — tools/read_doc.can_read_chunk 와 같은 규칙, DB 값 이름에 맞춤:
  - 보안등급: 사용자 security_grade ≥ 청크 security_grade
  - 공개 범위: scope = 'company' 이면 누구나, 'dept' 이면 owner_dept_id = 사용자 dept_id
문서 상태: 현재 버전(doc_version.is_current) · 처리 끝남(status = 'ready') · 문서 사용 중(doc.status = 'active')만.

pgvector HNSW 는 거리 순으로 후보를 뽑은 뒤 WHERE 조건을 거르므로, 권한 조건이 많이 걸러내면 결과가 k개보다 적을 수 있다.
그래서 pgvector 0.8 의 반복 탐색(hnsw.iterative_scan = strict_order)을 이 트랜잭션에서만 켠다(SET LOCAL).
"""
from __future__ import annotations

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Chunk, Doc, DocVersion


def build_query(qvec: list[float], *, dept_id: int | None, security_grade: int, k: int):
    """검색 SQL (권한·문서 상태 조건 포함). 테스트에서 SQL 모양을 확인할 수 있게 따로 둔다."""
    dist = Chunk.embedding.cosine_distance(qvec).label("distance")
    readable_scope = Chunk.scope == "company" if dept_id is None else or_(Chunk.scope == "company", Chunk.owner_dept_id == dept_id)
    return (select(Chunk.id, Chunk.doc_version_id, Chunk.chunk_index, Chunk.page, Chunk.content, Chunk.heading_path,
                   Doc.id.label("doc_id"), Doc.title, DocVersion.file_name, dist)
            .join(DocVersion, Chunk.doc_version_id == DocVersion.id)
            .join(Doc, DocVersion.doc_id == Doc.id)
            .where(DocVersion.is_current.is_(True), DocVersion.status == "ready", Doc.status == "active",
                   Chunk.embedding.is_not(None), Chunk.security_grade <= security_grade, readable_scope)
            .order_by(dist)
            .limit(k))


async def search_chunks(session: AsyncSession, query: str | list[float], *, dept_id: int | None, security_grade: int,
                        k: int = 5, embedder=None, iterative_scan: bool = True) -> list[dict]:
    """질문(글 또는 이미 만든 벡터) → 볼 수 있는 청크 상위 k개. score = 1 - 코사인 거리."""
    if isinstance(query, str):
        if embedder is None:
            raise ValueError("질문이 글이면 embedder 가 필요함 (KureEmbedder)")
        qvec = embedder.embed_query(query)
    else:
        qvec = list(query)
    if iterative_scan:
        await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    rows = (await session.execute(build_query(qvec, dept_id=dept_id, security_grade=security_grade, k=k))).mappings().all()
    return [{"chunk_id": r["id"], "doc_id": r["doc_id"], "doc_version_id": r["doc_version_id"], "title": r["title"],
             "file_name": r["file_name"], "chunk_index": r["chunk_index"], "page": r["page"],
             "heading_path": list(r["heading_path"] or []), "content": r["content"], "score": 1.0 - float(r["distance"])}
            for r in rows]


async def search_for_principal(session: AsyncSession, query: str, principal, **kw) -> list[dict]:
    """contracts.permission.Principal 의 attributes(dept_id, security_grade)로 검색. 등급이 없으면 아무것도 안 보여 줌."""
    grade = principal.attributes.get("security_grade")
    dept = principal.attributes.get("dept_id")
    if type(grade) is not int:
        return []
    return await search_chunks(session, query, dept_id=dept if type(dept) is int else None, security_grade=grade, **kw)
