"""팀 ERD 그대로 (docs/architecture/schema.postgres.sql, son 브랜치 b46fbe5 · 2026-11-20 · 24개 표)

이 파일은 팀 ERD 를 옮긴 것이다. 손으로 고치지 말고, 바꿀 게 있으면 새 변경 파일(0003…)을 만든다.
SQL 원문을 문장 단위로 나눠 순서대로 실행한다 (asyncpg 는 한 번에 한 문장만 받는다).

Revision ID: 0001
Revises:
Create Date: 2026-11-20
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    'CREATE EXTENSION IF NOT EXISTS vector',
    'CREATE TABLE department (\n    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    name          VARCHAR(100) NOT NULL,\n    head_user_id  BIGINT\n)',
    'CREATE TABLE users (\n    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    login_id        VARCHAR(100) NOT NULL UNIQUE,\n    name            VARCHAR(100) NOT NULL,\n    dept_id         BIGINT NOT NULL REFERENCES department (id),\n    position        VARCHAR(50),\n    security_grade  INT NOT NULL DEFAULT 1\n)',
    'ALTER TABLE department\n    ADD CONSTRAINT fk_department_head FOREIGN KEY (head_user_id) REFERENCES users (id)',
    "CREATE TABLE user_role (\n    role     VARCHAR(50) NOT NULL CHECK (role IN ('admin', 'dept_head', 'member')),\n    user_id  BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,\n    PRIMARY KEY (role, user_id)\n)",
    'CREATE TABLE agent_template (\n    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    name           VARCHAR(100) NOT NULL,\n    description    TEXT,\n    system_prompt  TEXT NOT NULL,\n    example        TEXT,\n    created_by     BIGINT NOT NULL REFERENCES users (id),\n    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()\n)',
    'CREATE TABLE agent_template_tool (\n    tool_name    VARCHAR(100) NOT NULL,\n    template_id  BIGINT NOT NULL REFERENCES agent_template (id) ON DELETE CASCADE,\n    PRIMARY KEY (tool_name, template_id)\n)',
    "CREATE TABLE agent (\n    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    owner_user_id  BIGINT NOT NULL REFERENCES users (id),\n    name           VARCHAR(100) NOT NULL,\n    description    TEXT,\n    system_prompt  TEXT NOT NULL,\n    scope          VARCHAR(20) NOT NULL DEFAULT 'personal' CHECK (scope IN ('personal', 'dept', 'company')),\n    status         VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),\n    template_id    BIGINT REFERENCES agent_template (id) ON DELETE SET NULL,\n    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),\n    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()\n)",
    'CREATE TABLE agent_tool (\n    tool_name  VARCHAR(100) NOT NULL,\n    agent_id   BIGINT NOT NULL REFERENCES agent (id) ON DELETE CASCADE,\n    PRIMARY KEY (tool_name, agent_id)\n)',
    "CREATE TABLE agent_share_request (\n    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    agent_id         BIGINT NOT NULL REFERENCES agent (id) ON DELETE CASCADE,\n    requester_id     BIGINT NOT NULL REFERENCES users (id),\n    approver_id      BIGINT NOT NULL REFERENCES users (id),\n    requested_scope  VARCHAR(20) NOT NULL CHECK (requested_scope IN ('dept', 'company')),\n    status           VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),\n    comment          TEXT,\n    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),\n    decided_at       TIMESTAMPTZ\n)",
    'CREATE TABLE agent_assignment (\n    agent_id     BIGINT NOT NULL REFERENCES agent (id) ON DELETE CASCADE,\n    dept_id      BIGINT NOT NULL REFERENCES department (id) ON DELETE CASCADE,\n    assigned_by  BIGINT NOT NULL REFERENCES users (id),\n    assigned_at  TIMESTAMPTZ NOT NULL DEFAULT now(),\n    PRIMARY KEY (agent_id, dept_id)\n)',
    'CREATE TABLE chat_session (\n    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),\n    user_id     BIGINT NOT NULL REFERENCES users (id),\n    agent_id    BIGINT REFERENCES agent (id) ON DELETE SET NULL,\n    title       VARCHAR(200),\n    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()\n)',
    "CREATE TABLE agent_run (\n    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),\n    session_id   UUID NOT NULL REFERENCES chat_session (id) ON DELETE CASCADE,\n    llm_calls    INT NOT NULL DEFAULT 0,\n    stop_reason  VARCHAR(20) CHECK (stop_reason IN ('completed', 'max_turns', 'denied')),\n    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),\n    ended_at     TIMESTAMPTZ\n)",
    "CREATE TABLE message (\n    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    session_id     UUID NOT NULL REFERENCES chat_session (id) ON DELETE CASCADE,\n    run_id         UUID REFERENCES agent_run (id) ON DELETE SET NULL,\n    seq            INT NOT NULL,\n    role           VARCHAR(20) NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),\n    content        TEXT,\n    tool_call_id   VARCHAR(100),\n    has_work_data  BOOLEAN NOT NULL DEFAULT FALSE,\n    UNIQUE (session_id, seq)\n)",
    'CREATE FUNCTION touch_chat_session() RETURNS trigger\nLANGUAGE plpgsql AS $$\nBEGIN\n    UPDATE chat_session SET updated_at = now() WHERE id = NEW.session_id;\n    RETURN NULL;\nEND;\n$$',
    'CREATE TRIGGER trg_message_touch_session\n    AFTER INSERT ON message\n    FOR EACH ROW EXECUTE FUNCTION touch_chat_session()',
    'CREATE TABLE tool_call (\n    call_id         VARCHAR(100) PRIMARY KEY,\n    message_id      BIGINT NOT NULL REFERENCES message (id) ON DELETE CASCADE,\n    seq             INT NOT NULL,\n    tool_name       VARCHAR(100) NOT NULL,\n    arguments       JSONB,\n    ok              BOOLEAN,\n    result_content  TEXT,\n    duration_ms     INT,\n    has_work_data   BOOLEAN NOT NULL DEFAULT FALSE,\n    UNIQUE (message_id, seq)\n)',
    "CREATE TABLE approval_request (\n    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    call_id  VARCHAR(100) NOT NULL UNIQUE REFERENCES tool_call (call_id) ON DELETE CASCADE,\n    status   VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'timeout')),\n    reason   TEXT NOT NULL\n)",
    "CREATE TABLE policy_rule (\n    policy_rule_id  VARCHAR(100) PRIMARY KEY,\n    priority        INT NOT NULL,\n    tool            VARCHAR(100) NOT NULL,\n    decision        VARCHAR(10) NOT NULL CHECK (decision IN ('allow', 'deny', 'ask')),\n    conditions      JSONB,\n    reason          TEXT,\n    enabled         BOOLEAN NOT NULL DEFAULT TRUE,\n    CHECK (decision = 'allow' OR coalesce(reason, '') <> '')\n)",
    "CREATE TABLE audit_log (\n    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    event_type          VARCHAR(30) NOT NULL DEFAULT 'tool_call'\n                        CHECK (event_type IN ('tool_call', 'doc_register', 'work_upload', 'work_purge',\n                                              'output_download', 'agent_share')),\n    call_id             VARCHAR(100) REFERENCES tool_call (call_id),\n    session_id          UUID,\n    user_id             BIGINT NOT NULL,\n    agent_id            BIGINT,\n    tool_name           VARCHAR(100),\n    arguments           JSONB,\n    principal_snapshot  JSONB,\n    decision            VARCHAR(10) CHECK (decision IN ('allow', 'deny', 'ask')),\n    reason              TEXT,\n    rule_id             VARCHAR(100) REFERENCES policy_rule (policy_rule_id),\n    approval            VARCHAR(20) CHECK (approval IN ('approved', 'rejected', 'timeout')),\n    executed            BOOLEAN NOT NULL DEFAULT FALSE,\n    tool_ok             BOOLEAN,\n    target_type         VARCHAR(30) CHECK (target_type IN ('document', 'work_file', 'output_file', 'agent')),\n    target_id           BIGINT,\n    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),\n    CHECK (event_type <> 'tool_call'\n           OR (call_id IS NOT NULL AND session_id IS NOT NULL AND tool_name IS NOT NULL AND decision IS NOT NULL))\n)",
    'CREATE INDEX idx_audit_log_user_ts ON audit_log (user_id, ts)',
    'CREATE INDEX idx_audit_log_tool_ts ON audit_log (tool_name, ts)',
    'CREATE INDEX idx_audit_log_ts      ON audit_log (ts)',
    'CREATE INDEX idx_audit_log_target  ON audit_log (target_type, target_id)',
    "CREATE TABLE document (\n    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    owner_user_id   BIGINT NOT NULL REFERENCES users (id),\n    owner_dept_id   BIGINT NOT NULL REFERENCES department (id),\n    title           VARCHAR(255) NOT NULL,\n    category        VARCHAR(30) NOT NULL CHECK (category IN ('내규', '매뉴얼', '상품', '법령', '공문')),\n    scope           VARCHAR(20) NOT NULL DEFAULT 'company' CHECK (scope IN ('dept', 'company')),\n    security_grade  INT NOT NULL DEFAULT 1,\n    status          VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired'))\n)",
    "CREATE TABLE document_version (\n    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    document_id     BIGINT NOT NULL REFERENCES document (id) ON DELETE CASCADE,\n    version         INT NOT NULL,\n    is_current      BOOLEAN NOT NULL DEFAULT TRUE,\n    file_name       VARCHAR(255) NOT NULL,\n    mime_type       VARCHAR(100) NOT NULL,\n    storage_path    VARCHAR(500) NOT NULL,\n    status          VARCHAR(20) NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'ready', 'failed')),\n    sha256          CHAR(64) NOT NULL,\n    effective_date  DATE,\n    parse_profile   VARCHAR(20) NOT NULL DEFAULT 'accurate',\n    parsed_path     VARCHAR(500),\n    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),\n    UNIQUE (document_id, version)\n)",
    'CREATE UNIQUE INDEX uq_document_version_current ON document_version (document_id) WHERE is_current',
    'CREATE UNIQUE INDEX uq_document_version_sha256  ON document_version (document_id, sha256)',
    "CREATE TABLE chunk (\n    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    document_version_id  BIGINT NOT NULL REFERENCES document_version (id) ON DELETE CASCADE,\n    chunk_index          INT NOT NULL,\n    page                 INT,\n    content              TEXT NOT NULL,\n    owner_dept_id        BIGINT NOT NULL,\n    scope                VARCHAR(20) NOT NULL CHECK (scope IN ('dept', 'company')),\n    security_grade       INT NOT NULL,\n    heading_path         TEXT[],\n    embedding            vector(1024),\n    UNIQUE (document_version_id, chunk_index)\n)",
    'CREATE INDEX idx_chunk_embedding ON chunk USING hnsw (embedding vector_cosine_ops)',
    'CREATE INDEX idx_chunk_access    ON chunk (scope, owner_dept_id, security_grade)',
    'CREATE TABLE citation (\n    message_id  BIGINT NOT NULL REFERENCES message (id) ON DELETE CASCADE,\n    chunk_id    BIGINT NOT NULL REFERENCES chunk (id) ON DELETE CASCADE,\n    score       REAL,\n    PRIMARY KEY (message_id, chunk_id)\n)',
    "CREATE TABLE agent_knowledge (\n    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    agent_id     BIGINT NOT NULL REFERENCES agent (id) ON DELETE CASCADE,\n    document_id  BIGINT REFERENCES document (id) ON DELETE CASCADE,\n    category     VARCHAR(30) CHECK (category IN ('내규', '매뉴얼', '상품', '법령', '공문')),\n    CHECK ((document_id IS NULL) <> (category IS NULL))\n)",
    "CREATE TABLE work_file (\n    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    session_id           UUID REFERENCES chat_session (id) ON DELETE SET NULL,\n    user_id              BIGINT NOT NULL REFERENCES users (id),\n    file_name            VARCHAR(255) NOT NULL,\n    mime_type            VARCHAR(100) NOT NULL,\n    size_bytes           BIGINT NOT NULL,\n    sha256               CHAR(64) NOT NULL,\n    storage_path         VARCHAR(500),\n    original_deleted_at  TIMESTAMPTZ,\n    parsed_path          VARCHAR(500),\n    pii_found            BOOLEAN NOT NULL DEFAULT FALSE,\n    pii_summary          JSONB,\n    status               VARCHAR(20) NOT NULL DEFAULT 'uploaded'\n                         CHECK (status IN ('uploaded', 'parsing', 'ready', 'failed', 'expired')),\n    uploaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),\n    last_used_at         TIMESTAMPTZ NOT NULL DEFAULT now(),\n    expires_at           TIMESTAMPTZ NOT NULL,\n    deleted_at           TIMESTAMPTZ\n)",
    'CREATE INDEX idx_work_file_session ON work_file (session_id)',
    'CREATE INDEX idx_work_file_expires ON work_file (expires_at) WHERE deleted_at IS NULL',
    'CREATE TABLE output_file (\n    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    owner_user_id      BIGINT NOT NULL REFERENCES users (id),\n    session_id         UUID REFERENCES chat_session (id) ON DELETE SET NULL,\n    run_id             UUID REFERENCES agent_run (id) ON DELETE SET NULL,\n    title              VARCHAR(200) NOT NULL,\n    file_name          VARCHAR(255) NOT NULL,\n    mime_type          VARCHAR(100) NOT NULL,\n    storage_path       VARCHAR(500) NOT NULL,\n    sha256             CHAR(64) NOT NULL,\n    has_customer_info  BOOLEAN NOT NULL DEFAULT FALSE,\n    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),\n    deleted_at         TIMESTAMPTZ\n)',
    'CREATE INDEX idx_output_file_owner ON output_file (owner_user_id, created_at)',
    'CREATE TABLE output_source (\n    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n    output_id            BIGINT NOT NULL REFERENCES output_file (id) ON DELETE CASCADE,\n    work_file_id         BIGINT REFERENCES work_file (id),\n    document_version_id  BIGINT REFERENCES document_version (id),\n    CHECK ((work_file_id IS NULL) <> (document_version_id IS NULL))\n)',
    "COMMENT ON TABLE department          IS '부서'",
    "COMMENT ON TABLE users               IS '사용자'",
    "COMMENT ON TABLE user_role           IS '사용자 역할 (admin > dept_head > member)'",
    "COMMENT ON TABLE agent_template      IS '관리자가 만든 에이전트 템플릿'",
    "COMMENT ON TABLE agent_template_tool IS '템플릿에 들어 있는 툴'",
    "COMMENT ON TABLE agent               IS '에이전트 (개인·부서·전사)'",
    "COMMENT ON TABLE agent_tool          IS '에이전트가 쓸 수 있는 툴 (실행 시 권한은 실행자 기준으로 따로 판정)'",
    "COMMENT ON TABLE agent_share_request IS '에이전트 부서·전사 공유 요청 (바로 위 관리자 승인)'",
    "COMMENT ON TABLE agent_assignment    IS '부서 에이전트를 쓸 수 있는 부서'",
    "COMMENT ON TABLE chat_session        IS '대화 세션'",
    "COMMENT ON TABLE agent_run           IS '에이전트 실행 (요청 1건)'",
    "COMMENT ON TABLE message             IS '대화 메시지'",
    "COMMENT ON TABLE tool_call           IS '툴 호출'",
    "COMMENT ON TABLE approval_request    IS 'ASK 판정 툴 호출의 승인 요청'",
    "COMMENT ON TABLE policy_rule         IS '권한 정책 규칙 (priority 순으로 처음 맞는 규칙 적용)'",
    "COMMENT ON TABLE audit_log           IS '감사 로그 (툴 호출, 문서 등록, 파일 업로드·삭제·내려받기, 에이전트 공유)'",
    "COMMENT ON TABLE document            IS '사내 문서 (관리자 등록)'",
    "COMMENT ON TABLE document_version    IS '문서 버전'",
    "COMMENT ON TABLE chunk               IS '문서 청크 (권한 속성은 문서에서 상속)'",
    "COMMENT ON TABLE citation            IS '답변 근거'",
    "COMMENT ON TABLE agent_knowledge     IS '에이전트가 검색할 문서 (문서 또는 분류)'",
    "COMMENT ON TABLE work_file           IS '작업 파일 (직원이 대화에 올린 파일, 파싱본만 만료 때까지 보관)'",
    "COMMENT ON TABLE output_file         IS 'AI 산출물'",
    "COMMENT ON TABLE output_source       IS '산출물을 만들 때 쓴 입력'",
    "COMMENT ON COLUMN department.head_user_id        IS '부서장 사용자 ID'",
    "COMMENT ON COLUMN users.position                 IS '직급'",
    "COMMENT ON COLUMN users.security_grade           IS '보안등급'",
    "COMMENT ON COLUMN agent.scope                    IS '공개 범위 (personal, dept, company)'",
    "COMMENT ON COLUMN agent.template_id              IS '만들 때 고른 템플릿 ID'",
    "COMMENT ON COLUMN agent_share_request.approver_id IS '승인할 사용자 ID (바로 위 관리자)'",
    "COMMENT ON COLUMN chat_session.agent_id          IS '사용한 에이전트 ID (NULL 이면 기본 에이전트)'",
    "COMMENT ON COLUMN chat_session.updated_at        IS '마지막 대화 시각'",
    "COMMENT ON COLUMN agent_run.stop_reason          IS '종료 사유 (completed, max_turns, denied)'",
    "COMMENT ON COLUMN message.seq                    IS '세션 내 순서'",
    "COMMENT ON COLUMN message.tool_call_id           IS 'role=tool 일 때 대응 툴 호출 ID'",
    "COMMENT ON COLUMN message.has_work_data          IS '작업 파일 내용 포함 여부 (만료 때 content 삭제 대상)'",
    "COMMENT ON COLUMN tool_call.call_id              IS '툴 호출 ID (LLM 이 준 id)'",
    "COMMENT ON COLUMN tool_call.seq                  IS '응답 내 호출 순서'",
    "COMMENT ON COLUMN tool_call.result_content       IS 'LLM 에게 돌려준 결과'",
    "COMMENT ON COLUMN tool_call.has_work_data        IS '결과에 작업 파일 내용 포함 여부'",
    "COMMENT ON COLUMN policy_rule.priority           IS '매칭 순서 (작을수록 먼저)'",
    "COMMENT ON COLUMN policy_rule.conditions         IS 'ABAC 조건 (사용자 속성, 툴 인자)'",
    "COMMENT ON COLUMN audit_log.event_type           IS '기록 종류 (tool_call 이면 call_id·session_id·tool_name·decision 필수)'",
    "COMMENT ON COLUMN audit_log.agent_id             IS '호출한 에이전트 ID (기록용, FK 없음)'",
    "COMMENT ON COLUMN audit_log.principal_snapshot   IS '판정 시점 요청자 속성'",
    "COMMENT ON COLUMN audit_log.approval             IS 'ASK 일 때 승인 결과 (approved, rejected, timeout)'",
    "COMMENT ON COLUMN audit_log.tool_ok              IS '실행했을 때 툴 성공 여부 (실행 안 했으면 NULL)'",
    "COMMENT ON COLUMN audit_log.ts                   IS '판정 시각 (UTC)'",
    "COMMENT ON COLUMN document.category              IS '분류 (내규, 매뉴얼, 상품, 법령, 공문)'",
    "COMMENT ON COLUMN document.scope                 IS '공개 범위 (dept, company)'",
    "COMMENT ON COLUMN document.owner_dept_id         IS '관리 책임 부서'",
    "COMMENT ON COLUMN document_version.is_current    IS '현재 버전 여부'",
    "COMMENT ON COLUMN document_version.parsed_path   IS 'parse_result/1 JSON 경로'",
    "COMMENT ON COLUMN chunk.heading_path             IS '제목 경로 (parse_result elements.heading_path)'",
    "COMMENT ON COLUMN chunk.embedding                IS 'bge-m3 임베딩 (1024차원)'",
    "COMMENT ON COLUMN citation.score                 IS '검색 유사도'",
    "COMMENT ON COLUMN work_file.expires_at           IS '파싱본 만료 시각 (last_used_at + 관리자 정책 일수)'",
    "COMMENT ON COLUMN output_file.has_customer_info  IS '고객정보 포함 여부'",
]


def upgrade() -> None:
    for s in STATEMENTS:
        op.execute(s)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS output_source, output_file, work_file, agent_knowledge, citation, chunk, document_version, document, audit_log, policy_rule, approval_request, tool_call, message, agent_run, chat_session, agent_assignment, agent_share_request, agent_tool, agent, agent_template_tool, agent_template, user_role, users, department CASCADE")
    op.execute("DROP FUNCTION IF EXISTS touch_chat_session()")
