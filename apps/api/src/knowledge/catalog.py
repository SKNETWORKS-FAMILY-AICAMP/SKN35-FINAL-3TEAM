"""사내 문서 목록: 플랫폼에 어떤 문서가 들어 있는지 사용자에게 보여 준다 (화면 '사내 문서').

원칙: **에이전트가 나 대신 찾아올 수 있는 문서 = 사내 문서 화면에서 내가 보는 문서** (2026-11-24).
  - 직원(부서장 포함): 전사 공개 + 내 부서 전용. 검색(knowledge/retrieval.py)과 같은 조건이라
    목록에는 있는데 검색은 안 되거나, 검색은 되는데 목록에는 없는 일이 없다.
  - 관리자(roles 에 admin): 문서 관리를 위해 **목록(제목·범위·등록 정보)은 전체**를 본다.
    내용은 보지 않는다 — 에이전트 검색·원문 열람 규칙은 그대로라, 다른 부서 문서 내용은 관리자에게도 안 나온다.
    그래서 문서마다 searchable(이 사람의 에이전트가 찾아올 수 있나)을 같이 준다.
공통 조건 (검색과 같음):
  - 보안등급: 사용자 security_grade ≥ 문서 security_grade
  - 문서 사용 중(doc.status = 'active'), 현재 버전(is_current) · 처리 끝남(status = 'ready')
"""
from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Chunk, Dept, Doc, DocVersion


def readable_by(dept_id: int | None):
    """검색과 같은 공개 범위 조건: 전사 공개는 누구나, 부서 전용은 같은 부서만."""
    if dept_id is None:
        return Doc.scope == "company"
    return or_(Doc.scope == "company", and_(Doc.scope == "dept", Doc.owner_dept_id == dept_id))


def build_docs_query(*, dept_id: int | None, security_grade: int, admin: bool = False):
    """사내 문서 목록 SQL. 문서마다 현재 버전 하나와 그 조각 수, 공개 범위와 담당 부서."""
    chunks = func.count(Chunk.id).label("chunks")
    conditions = [Doc.status == "active", Doc.security_grade <= security_grade,
                  DocVersion.is_current.is_(True), DocVersion.status == "ready"]
    if not admin:
        conditions.append(readable_by(dept_id))
    return (select(Doc.id, Doc.title, Doc.category, Doc.scope, Doc.owner_dept_id, Dept.name.label("dept_name"),
                   DocVersion.file_name, DocVersion.registered_at, chunks)
            .join(DocVersion, DocVersion.doc_id == Doc.id)
            .outerjoin(Dept, Dept.id == Doc.owner_dept_id)
            .outerjoin(Chunk, Chunk.doc_version_id == DocVersion.id)
            .where(*conditions)
            .group_by(Doc.id, DocVersion.id, Dept.name)
            .order_by(Doc.scope, Doc.title))   # 'company' 가 'dept' 보다 앞: 전사 공개 먼저


async def list_docs(session: AsyncSession, *, dept_id: int | None, security_grade: int, admin: bool = False) -> list[dict]:
    rows = (await session.execute(build_docs_query(dept_id=dept_id, security_grade=security_grade, admin=admin))).mappings().all()
    return [{"doc_id": r["id"], "title": r["title"], "category": r["category"], "file_name": r["file_name"],
             "registered_at": r["registered_at"], "chunks": int(r["chunks"]),
             "scope": r["scope"], "dept_id": r["owner_dept_id"], "dept_name": r["dept_name"],
             "searchable": r["scope"] == "company" or (dept_id is not None and r["owner_dept_id"] == dept_id)}
            for r in rows]
