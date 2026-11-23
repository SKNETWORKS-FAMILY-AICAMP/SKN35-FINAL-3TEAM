"""사내 문서 목록: 플랫폼에 어떤 문서가 들어 있는지 사용자에게 보여 준다 (화면 '사내 문서').

지금은 1단계로 **전사 공개(scope='company') 문서만** 보여 준다 (2026-11-23).
부서 전용(scope='dept') 문서는 목록에 넣지 않는다 (검색에서는 그 부서 사람에게 그대로 나온다).

검색(knowledge/retrieval.py)과 같은 조건을 쓴다. 목록에는 있는데 검색은 안 되는 일이 없도록:
  - 보안등급: 사용자 security_grade ≥ 문서 security_grade
  - 문서 사용 중(doc.status = 'active'), 현재 버전(is_current) · 처리 끝남(status = 'ready')
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Chunk, Doc, DocVersion


def build_company_docs_query(*, security_grade: int):
    """전사 공개 문서 목록 SQL. 문서마다 현재 버전 하나와 그 조각 수."""
    chunks = func.count(Chunk.id).label("chunks")
    return (select(Doc.id, Doc.title, Doc.category, DocVersion.file_name, DocVersion.registered_at, chunks)
            .join(DocVersion, DocVersion.doc_id == Doc.id)
            .outerjoin(Chunk, Chunk.doc_version_id == DocVersion.id)
            .where(Doc.status == "active", Doc.scope == "company", Doc.security_grade <= security_grade,
                   DocVersion.is_current.is_(True), DocVersion.status == "ready")
            .group_by(Doc.id, DocVersion.id)
            .order_by(Doc.title))


async def list_company_docs(session: AsyncSession, *, security_grade: int) -> list[dict]:
    rows = (await session.execute(build_company_docs_query(security_grade=security_grade))).mappings().all()
    return [{"doc_id": r["id"], "title": r["title"], "category": r["category"], "file_name": r["file_name"],
             "registered_at": r["registered_at"], "chunks": int(r["chunks"])} for r in rows]
