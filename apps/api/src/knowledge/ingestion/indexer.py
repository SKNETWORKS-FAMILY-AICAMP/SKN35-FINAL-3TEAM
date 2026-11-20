# -*- coding: utf-8 -*-
"""색인: 파서 결과 → 청크 → 임베딩 → 팀 DB chunk 표 (db/models.py, PostgreSQL + pgvector).

    from db.engine import get_session
    from knowledge.ingestion.embedders import KureEmbedder
    from knowledge.ingestion.indexer import index_document_version

    embedder = KureEmbedder("models/KURE-v3")                 # 배포·운영은 CPU 기본
    n = await index_document_version(session, doc_version_id, parsed, embedder=embedder,
                                     tokenizer_path="models/KURE-v3/tokenizer.json")
    await session.commit()                                      # 커밋은 부르는 쪽이 한다(팀 DB 규칙)

- 권한 3개(owner_dept_id, scope, security_grade)는 document 표에서 읽어 청크마다 물려준다.
- 같은 문서 버전을 다시 색인하면 그 버전의 기존 청크를 지우고 새로 넣는다.
- 파싱 실패(status != ok)면 청크 없이 document_version.status = 'failed', 성공이면 'ready'.
- 표 구조는 바꾸지 않는다(표 변경은 DB 담당). 임베딩 차원은 db.models.EMBEDDING_DIM 을 따른다.
"""
from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import EMBEDDING_DIM, Chunk, Document, DocumentVersion

from .chunkers import DEFAULT_MAX_TOKENS, chunk_document


def chunk_rows(chunks: list[dict], vectors: list[list[float]]) -> list[Chunk]:
    """청크 dict + 벡터 → Chunk 행 (DB 칸만)."""
    if len(chunks) != len(vectors):
        raise ValueError(f"청크 {len(chunks)}개, 벡터 {len(vectors)}개 — 개수가 다름")
    rows = []
    for c, v in zip(chunks, vectors):
        if len(v) != EMBEDDING_DIM:
            raise ValueError(f"벡터 차원 {len(v)} ≠ DB chunk.embedding 차원 {EMBEDDING_DIM}")
        rows.append(Chunk(doc_version_id=c["doc_version_id"], chunk_index=c["chunk_index"], page=c["page"],
                          content=c["content"], owner_dept_id=c["owner_dept_id"], scope=c["scope"],
                          security_grade=c["security_grade"], heading_path=c["heading_path"] or None, embedding=v))
    return rows


async def index_document_version(session: AsyncSession, doc_version_id: int, parsed: dict, *, embedder,
                                 count_tokens=None, tokenizer_path=None, max_tokens: int = DEFAULT_MAX_TOKENS) -> int:
    """한 문서 버전을 색인하고 넣은 청크 수를 돌려준다. 커밋은 하지 않는다(flush까지)."""
    dv = await session.get(DocumentVersion, doc_version_id)
    if dv is None:
        raise LookupError(f"document_version {doc_version_id} 없음")
    doc = await session.get(Document, dv.doc_id)
    await session.execute(delete(Chunk).where(Chunk.doc_version_id == doc_version_id))
    if parsed.get("status") != "ok":
        dv.status = "failed"
        await session.flush()
        return 0
    meta = {"owner_dept_id": doc.owner_dept_id, "scope": doc.scope, "security_grade": doc.security_grade}
    chunks = chunk_document({**parsed, "document_version_id": doc_version_id}, max_tokens=max_tokens,
                            count_tokens=count_tokens, tokenizer_path=tokenizer_path, document_meta=meta)
    vectors = embedder.embed_documents([c["embed_text"] for c in chunks]) if chunks else []
    session.add_all(chunk_rows(chunks, vectors))
    dv.status = "ready"
    await session.flush()
    return len(chunks)
