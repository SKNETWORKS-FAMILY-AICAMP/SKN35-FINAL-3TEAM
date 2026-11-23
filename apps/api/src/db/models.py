"""ORM 모델 — docs/architecture/schema.postgres.sql (팀 ERD, 24개 표) 와 같은 표·칸.

- 표를 만들고 바꾸는 건 alembic/ 마이그레이션이 한다. 이 파일은 코드에서 표를 읽고 쓰기 위한 지도다.
  그래서 CHECK 제약·트리거·부분 인덱스는 여기 다시 적지 않고 마이그레이션에만 둔다 (한 곳에서만 관리).
- 팀 ERD 에서 바꾼 것은 [DB 추가](0002)·[0003] 처럼 마이그레이션 번호로 표시한다.
- 칸을 바꿀 땐: 마이그레이션 추가 → 이 파일 수정 → tests/db 의 모델·DB 일치 테스트로 확인.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (CHAR, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Identity, Integer, String, Text,
                        text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 2048  # chunk.embedding 차원 — [0004] KURE-v3, halfvec(2048)


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[int]:
    """BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY — 값은 DB 가 정한다."""
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


def _now() -> Mapped[datetime]:
    """TIMESTAMPTZ NOT NULL DEFAULT now()"""
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


# ── 조직 ────────────────────────────────────────────────

class Dept(Base):
    __tablename__ = "dept"
    id: Mapped[int] = _id()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    head_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", name="fk_dept_head",
                                                                               use_alter=True))


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = _id()
    login_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    dept_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("dept.id"), nullable=False)
    position: Mapped[str | None] = mapped_column(String(50))
    security_grade: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))  # 1~5
    # [DB 추가] 사내 통합 로그인(SSO) 연결용. 로그인 방식이 정해지기 전까지 비어 있다.
    auth_provider: Mapped[str | None] = mapped_column(String(30))   # 예: ldap, saml, oidc
    external_id: Mapped[str | None] = mapped_column(String(255))    # SSO 쪽 계정 ID
    # [0007] 로그인. 비밀번호 원문은 저장하지 않는다 (scrypt 해시만)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserRole(Base):
    __tablename__ = "user_role"
    role: Mapped[str] = mapped_column(String(50), primary_key=True)  # admin | dept_head | member
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class LoginSession(Base):
    """[0007] 로그인 세션. 쿠키 토큰은 저장하지 않고 SHA-256 만 둔다."""
    __tablename__ = "login_session"
    token_hash: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))


# ── 에이전트 ────────────────────────────────────────────

class AgentTemplate(Base):
    __tablename__ = "agent_template"
    id: Mapped[int] = _id()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    example: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    updated_at: Mapped[datetime] = _now()


class AgentTemplateTool(Base):
    __tablename__ = "agent_template_tool"
    tool_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    template_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_template.id", ondelete="CASCADE"),
                                             primary_key=True)


class Agent(Base):
    __tablename__ = "agent"
    id: Mapped[int] = _id()
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'personal'"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'active'"))
    template_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_template.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    disabled_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))  # [0008]


class AgentTool(Base):
    __tablename__ = "agent_tool"
    tool_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent.id", ondelete="CASCADE"), primary_key=True)


class AgentShareRequest(Base):
    __tablename__ = "agent_share_request"
    id: Mapped[int] = _id()
    agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent.id", ondelete="CASCADE"), nullable=False)
    requester_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    approver_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    requested_scope: Mapped[str] = mapped_column(String(20), nullable=False)  # dept | company
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    comment: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = _now()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))  # [0008]
    decision_comment: Mapped[str | None] = mapped_column(Text)  # [0008]


class AgentAssignment(Base):
    __tablename__ = "agent_assignment"
    agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent.id", ondelete="CASCADE"), primary_key=True)
    dept_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("dept.id", ondelete="CASCADE"),
                                         primary_key=True)
    assigned_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    assigned_at: Mapped[datetime] = _now()


# ── 대화 ────────────────────────────────────────────────

class ChatSession(Base):
    __tablename__ = "chat_session"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent.id", ondelete="SET NULL"))
    title: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = _now()  # 메시지가 추가되면 DB 트리거가 갱신
    agent_removed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))  # [0008]


class AgentRun(Base):
    __tablename__ = "agent_run"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                  ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    stop_reason: Mapped[str | None] = mapped_column(String(20))  # completed | max_turns | denied
    started_at: Mapped[datetime] = _now()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    denial: Mapped[dict | None] = mapped_column(JSONB)  # 0009: denied 일 때 무엇이 왜 막혔는지


class Message(Base):
    __tablename__ = "message"
    id: Mapped[int] = _id()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True),
                                                  ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_run.id", ondelete="SET NULL"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # system | user | assistant | tool
    content: Mapped[str | None] = mapped_column(Text)
    tool_call_id: Mapped[str | None] = mapped_column(String(100))
    has_work_data: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class ToolCall(Base):
    __tablename__ = "tool_call"
    call_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("message.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments: Mapped[dict | None] = mapped_column(JSONB)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    result_content: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    has_work_data: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class ApprovalRequest(Base):
    __tablename__ = "approval_request"
    id: Mapped[int] = _id()
    call_id: Mapped[str] = mapped_column(String(100), ForeignKey("tool_call.call_id", ondelete="CASCADE"),
                                         nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'pending'"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)


# ── 권한·감사 ───────────────────────────────────────────

class PolicyRule(Base):
    __tablename__ = "policy_rule"
    policy_rule_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    tool: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)  # allow | deny | ask
    conditions: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class AuditLog(Base):
    """추가만 된다. 고치거나 지우려 하면 DB 트리거가 거부한다 (마이그레이션 0002)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = _id()
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'tool_call'"))
    call_id: Mapped[str | None] = mapped_column(String(100))  # 0010: tool_call 외래키 없음 (대화를 지워도 남게)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    tool_name: Mapped[str | None] = mapped_column(String(100))
    arguments: Mapped[dict | None] = mapped_column(JSONB)
    principal_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    decision: Mapped[str | None] = mapped_column(String(10))
    reason: Mapped[str | None] = mapped_column(Text)
    rule_id: Mapped[str | None] = mapped_column(String(100))  # 0010: 정책은 yaml 에 있어 policy_rule 외래키 없음
    approval: Mapped[str | None] = mapped_column(String(20))
    executed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    tool_ok: Mapped[bool | None] = mapped_column(Boolean)
    target_type: Mapped[str | None] = mapped_column(String(30))
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    ts: Mapped[datetime] = _now()
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # 0010: 어느 요청에서 나온 판정인지


# ── 문서 (사내 지식) ────────────────────────────────────

class Doc(Base):
    __tablename__ = "doc"
    id: Mapped[int] = _id()
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    owner_dept_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("dept.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)  # 내규 | 매뉴얼 | 상품 | 법령 | 공문
    scope: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'company'"))
    security_grade: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'active'"))


class DocVersion(Base):
    __tablename__ = "doc_version"
    id: Mapped[int] = _id()
    doc_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("doc.id", ondelete="CASCADE"), nullable=False)  # [0003] document_id → doc_id
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)  # STORAGE_ROOT 기준 상대 경로
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'processing'"))
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date)
    parse_profile: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'accurate'"))
    parsed_path: Mapped[str | None] = mapped_column(String(500))
    registered_at: Mapped[datetime] = _now()


class Chunk(Base):
    """청킹 확정 때 바뀔 수 있는 표 (벡터 위치·칸 구성)."""
    __tablename__ = "chunk"
    id: Mapped[int] = _id()
    doc_version_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("doc_version.id", ondelete="CASCADE"),
                                                nullable=False)  # [0003] document_version_id → doc_version_id
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    owner_dept_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    security_grade: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_DIM))  # [0004] vector(1024) → halfvec(2048)


class Citation(Base):
    __tablename__ = "citation"
    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("message.id", ondelete="CASCADE"), primary_key=True)
    chunk_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chunk.id", ondelete="CASCADE"), primary_key=True)
    score: Mapped[float | None] = mapped_column(Float(24))  # REAL


class AgentKnowledge(Base):
    __tablename__ = "agent_knowledge"
    id: Mapped[int] = _id()
    agent_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent.id", ondelete="CASCADE"), nullable=False)
    doc_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("doc.id", ondelete="CASCADE"))  # [0003] document_id → doc_id
    category: Mapped[str | None] = mapped_column(String(30))


# ── 작업 파일 ───────────────────────────────────────────

class WorkFile(Base):
    __tablename__ = "work_file"
    id: Mapped[int] = _id()
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True),
                                                         ForeignKey("chat_session.id", ondelete="SET NULL"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(500))
    original_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parsed_path: Mapped[str | None] = mapped_column(String(500))
    pii_found: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    pii_summary: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'uploaded'"))
    uploaded_at: Mapped[datetime] = _now()
    last_used_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── 산출물 ──────────────────────────────────────────────

class OutputFile(Base):
    __tablename__ = "output_file"
    id: Mapped[int] = _id()
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True),
                                                         ForeignKey("chat_session.id", ondelete="SET NULL"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_run.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    has_customer_info: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _now()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutputSource(Base):
    __tablename__ = "output_source"
    id: Mapped[int] = _id()
    output_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("output_file.id", ondelete="CASCADE"), nullable=False)
    work_file_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("work_file.id"))
    doc_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("doc_version.id"))  # [0003]
