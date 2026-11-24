"""사내 문서 목록 API (화면 '사내 문서'). 에이전트가 이 사람 대신 찾아올 수 있는 문서 (전사 공개 + 내 부서).
관리자는 목록(제목·범위·등록 정보)만 전체를 본다. 규칙은 knowledge/catalog.py.

DOC_STORE=db 일 때만 팀 DB 에서 읽는다. 아니면(file) 문서 검색이 예시 파일을 쓰므로 목록을 내지 않는다 (enabled=false).
'내 문서'(에이전트가 만든 PDF·DOCX, /api/documents) 와는 다른 것이다.
"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from contracts.permission import Principal
from knowledge.catalog import list_docs
from knowledge.retrieval import principal_access
from routers.deps import current_user

router = APIRouter(tags=["knowledge"])


def _session_factory():
    from db.engine import session_scope
    return session_scope()


def doc_store_is_db() -> bool:
    return os.environ.get("DOC_STORE", "file").strip().lower() == "db"


class KnowledgeDocOut(BaseModel):
    doc_id: int
    title: str
    category: str
    file_name: str
    registered_at: datetime
    chunks: int
    scope: str                  # company = 전사 공개, dept = 부서 전용
    dept_id: int | None = None  # 문서 담당 부서
    dept_name: str | None = None
    searchable: bool = True     # 이 사람의 에이전트가 검색해 올 수 있나 (관리자 전체 목록에서 false 가 있다)


class KnowledgeDocsOut(BaseModel):
    enabled: bool  # false = DOC_STORE=db 가 아니라 팀 DB 문서를 쓰지 않음
    docs: list[KnowledgeDocOut]
    admin_view: bool = False  # 관리자 전체 목록 (내용 검색 범위는 그대로)


@router.get("/api/knowledge/docs", response_model=KnowledgeDocsOut)
async def company_docs(user: Principal = Depends(current_user)) -> KnowledgeDocsOut:
    if not doc_store_is_db():
        return KnowledgeDocsOut(enabled=False, docs=[])
    access = principal_access(user)
    if access is None:  # 보안등급이 없는 사용자는 아무 문서도 못 본다 (검색과 같음)
        return KnowledgeDocsOut(enabled=True, docs=[])
    async with _session_factory() as session:
        admin = "admin" in user.roles
        docs = await list_docs(session, dept_id=access[0], security_grade=access[1], admin=admin)
    return KnowledgeDocsOut(enabled=True, docs=[KnowledgeDocOut(**d) for d in docs], admin_view=admin)
