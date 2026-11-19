-- 업무 자동화 에이전트 DB 스키마 (PostgreSQL 16 + pgvector)
-- 기준: docs/architecture/erd.sql (ERDCloud 용 MySQL 문법). 테이블·컬럼·관계는 같고 타입만 Postgres 에 맞췄다.
-- 추가: CHECK 제약(상태값), UNIQUE, ON DELETE 규칙, 검색용 인덱스
-- 변경: chunk.vector_id 대신 pgvector embedding 컬럼 (벡터 DB 미결 → pgvector 제안)
-- chat_session.updated_at 은 message 추가 트리거로 갱신된다.

CREATE EXTENSION IF NOT EXISTS vector;

-- ── 조직 ────────────────────────────────────────────────

CREATE TABLE department (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    head_user_id  BIGINT  -- FK 는 users 생성 후 아래에서 추가 (순환 참조)
);

CREATE TABLE users (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    login_id        VARCHAR(100) NOT NULL UNIQUE,
    name            VARCHAR(100) NOT NULL,
    department_id   BIGINT NOT NULL REFERENCES department (id),
    position        VARCHAR(50),
    security_level  INT NOT NULL DEFAULT 1
);

ALTER TABLE department
    ADD CONSTRAINT fk_department_head FOREIGN KEY (head_user_id) REFERENCES users (id);

CREATE TABLE user_role (
    role     VARCHAR(50) NOT NULL CHECK (role IN ('admin', 'dept_head', 'member')),
    user_id  BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    PRIMARY KEY (role, user_id)
);

-- ── 에이전트 ────────────────────────────────────────────

CREATE TABLE agent (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id  BIGINT NOT NULL REFERENCES users (id),
    name           VARCHAR(100) NOT NULL,
    description    TEXT,
    system_prompt  TEXT NOT NULL,
    scope          VARCHAR(20) NOT NULL DEFAULT 'personal' CHECK (scope IN ('personal', 'dept', 'company')),
    status         VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled'))
);

CREATE TABLE agent_tool (
    tool_name  VARCHAR(100) NOT NULL,
    agent_id   BIGINT NOT NULL REFERENCES agent (id) ON DELETE CASCADE,
    PRIMARY KEY (tool_name, agent_id)
);

CREATE TABLE agent_share_request (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_id         BIGINT NOT NULL REFERENCES agent (id) ON DELETE CASCADE,
    requester_id     BIGINT NOT NULL REFERENCES users (id),
    approver_id      BIGINT NOT NULL REFERENCES users (id),
    requested_scope  VARCHAR(20) NOT NULL CHECK (requested_scope IN ('dept', 'company')),
    status           VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    comment          TEXT,
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ
);

-- ── 대화 ────────────────────────────────────────────────

CREATE TABLE chat_session (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     BIGINT NOT NULL REFERENCES users (id),
    agent_id    BIGINT REFERENCES agent (id) ON DELETE SET NULL,  -- NULL 이면 기본 에이전트
    title       VARCHAR(200),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()  -- 메시지가 추가되면 트리거로 갱신
);

CREATE TABLE agent_run (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id   UUID NOT NULL REFERENCES chat_session (id) ON DELETE CASCADE,
    llm_calls    INT NOT NULL DEFAULT 0,
    stop_reason  VARCHAR(20) CHECK (stop_reason IN ('completed', 'max_turns', 'denied')),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at     TIMESTAMPTZ  -- 실행 중이면 NULL
);

CREATE TABLE message (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id    UUID NOT NULL REFERENCES chat_session (id) ON DELETE CASCADE,
    run_id        UUID REFERENCES agent_run (id) ON DELETE SET NULL,
    seq           INT NOT NULL,
    role          VARCHAR(20) NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content       TEXT,
    tool_call_id  VARCHAR(100),  -- role=tool 일 때 대응 툴 호출 ID (순환 참조라 FK 없음)
    UNIQUE (session_id, seq)
);

-- chat_session.updated_at = 마지막 대화 시각. 제목 수정 등에는 바뀌지 않도록 메시지 추가 때만 갱신한다.
CREATE FUNCTION touch_chat_session() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE chat_session SET updated_at = now() WHERE id = NEW.session_id;
    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_message_touch_session
    AFTER INSERT ON message
    FOR EACH ROW EXECUTE FUNCTION touch_chat_session();

CREATE TABLE tool_call (
    call_id         VARCHAR(100) PRIMARY KEY,
    message_id      BIGINT NOT NULL REFERENCES message (id) ON DELETE CASCADE,
    seq             INT NOT NULL,
    tool_name       VARCHAR(100) NOT NULL,
    arguments       JSONB,
    ok              BOOLEAN,
    result_content  TEXT,
    duration_ms     INT,
    UNIQUE (message_id, seq)
);

CREATE TABLE approval_request (
    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_id  VARCHAR(100) NOT NULL UNIQUE REFERENCES tool_call (call_id) ON DELETE CASCADE,
    status   VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'timeout')),
    reason   TEXT NOT NULL
);

-- ── 권한·감사 ───────────────────────────────────────────

CREATE TABLE policy_rule (
    policy_rule_id  VARCHAR(100) PRIMARY KEY,
    priority        INT NOT NULL,
    tool            VARCHAR(100) NOT NULL,  -- 툴 이름 또는 '*'
    decision        VARCHAR(10) NOT NULL CHECK (decision IN ('allow', 'deny', 'ask')),
    conditions      JSONB,
    reason          TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    CHECK (decision = 'allow' OR coalesce(reason, '') <> '')  -- DENY/ASK 는 사유 필수
);

-- 감사 로그는 지워지면 안 되므로 참조 대상 삭제를 막는다 (기본 NO ACTION).
CREATE TABLE audit_log (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_id             VARCHAR(100) NOT NULL REFERENCES tool_call (call_id),
    session_id          UUID NOT NULL,
    user_id             BIGINT NOT NULL,
    agent_id            BIGINT,  -- 기록용, FK 없음
    tool_name           VARCHAR(100) NOT NULL,
    arguments           JSONB,
    principal_snapshot  JSONB,
    decision            VARCHAR(10) NOT NULL CHECK (decision IN ('allow', 'deny', 'ask')),
    reason              TEXT,
    rule_id             VARCHAR(100) REFERENCES policy_rule (policy_rule_id),
    approval            VARCHAR(20) CHECK (approval IN ('approved', 'rejected', 'timeout')),
    executed            BOOLEAN NOT NULL DEFAULT FALSE,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_user_ts ON audit_log (user_id, ts);
CREATE INDEX idx_audit_log_tool_ts ON audit_log (tool_name, ts);
CREATE INDEX idx_audit_log_ts      ON audit_log (ts);

-- ── 문서 ────────────────────────────────────────────────

CREATE TABLE document (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id        BIGINT NOT NULL REFERENCES users (id),
    owner_department_id  BIGINT NOT NULL REFERENCES department (id),
    title                VARCHAR(255) NOT NULL,
    scope                VARCHAR(20) NOT NULL CHECK (scope IN ('personal', 'dept', 'company')),
    security_level       INT NOT NULL DEFAULT 1
);

CREATE TABLE document_version (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id   BIGINT NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    version       INT NOT NULL,
    is_current    BOOLEAN NOT NULL DEFAULT TRUE,
    file_name     VARCHAR(255) NOT NULL,
    mime_type     VARCHAR(100) NOT NULL,
    storage_path  VARCHAR(500) NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'ready', 'failed')),
    UNIQUE (document_id, version)
);

-- 문서마다 현재 버전은 하나만
CREATE UNIQUE INDEX uq_document_version_current ON document_version (document_id) WHERE is_current;

CREATE TABLE chunk (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_version_id  BIGINT NOT NULL REFERENCES document_version (id) ON DELETE CASCADE,
    chunk_index          INT NOT NULL,
    page                 INT,
    content              TEXT NOT NULL,
    owner_department_id  BIGINT NOT NULL,  -- 문서에서 상속
    scope                VARCHAR(20) NOT NULL CHECK (scope IN ('personal', 'dept', 'company')),  -- 문서에서 상속
    security_level       INT NOT NULL,     -- 문서에서 상속
    embedding            vector(1024),     -- bge-m3 (EMBEDDING_DIM=1024)
    UNIQUE (document_version_id, chunk_index)
);

CREATE INDEX idx_chunk_embedding ON chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_chunk_access    ON chunk (scope, owner_department_id, security_level);

CREATE TABLE session_document (
    session_id   UUID NOT NULL REFERENCES chat_session (id) ON DELETE CASCADE,
    document_id  BIGINT NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    PRIMARY KEY (session_id, document_id)
);

CREATE TABLE citation (
    message_id  BIGINT NOT NULL REFERENCES message (id) ON DELETE CASCADE,
    chunk_id    BIGINT NOT NULL REFERENCES chunk (id) ON DELETE CASCADE,
    score       REAL,
    PRIMARY KEY (message_id, chunk_id)
);

-- ── 설명 (COMMENT ON) ───────────────────────────────────

COMMENT ON TABLE department          IS '부서';
COMMENT ON TABLE users               IS '사용자';
COMMENT ON TABLE user_role           IS '사용자 역할 (admin > dept_head > member)';
COMMENT ON TABLE agent               IS '사용자가 만든 커스텀 에이전트';
COMMENT ON TABLE agent_tool          IS '에이전트가 쓸 수 있는 툴 (실행 시 권한은 실행자 기준으로 따로 판정)';
COMMENT ON TABLE agent_share_request IS '에이전트 부서·전사 공유 요청 (바로 위 관리자 승인)';
COMMENT ON TABLE chat_session        IS '대화 세션';
COMMENT ON TABLE agent_run           IS '에이전트 실행 (요청 1건)';
COMMENT ON TABLE message             IS '대화 메시지';
COMMENT ON TABLE tool_call           IS '툴 호출';
COMMENT ON TABLE approval_request    IS 'ASK 판정 툴 호출의 승인 요청';
COMMENT ON TABLE policy_rule         IS '권한 정책 규칙 (priority 순으로 처음 맞는 규칙 적용)';
COMMENT ON TABLE audit_log           IS '권한 감사 로그';
COMMENT ON TABLE document            IS '문서';
COMMENT ON TABLE document_version    IS '문서 버전';
COMMENT ON TABLE chunk               IS '문서 청크 (권한 속성은 문서에서 상속)';
COMMENT ON TABLE session_document    IS '대화에 첨부한 문서';
COMMENT ON TABLE citation            IS '답변 근거';

COMMENT ON COLUMN department.head_user_id        IS '부서장 사용자 ID';
COMMENT ON COLUMN users.position                 IS '직급';
COMMENT ON COLUMN users.security_level           IS '보안등급';
COMMENT ON COLUMN agent.scope                    IS '공개 범위 (personal, dept, company)';
COMMENT ON COLUMN agent_share_request.approver_id IS '승인할 사용자 ID (바로 위 관리자)';
COMMENT ON COLUMN chat_session.agent_id          IS '사용한 에이전트 ID (NULL 이면 기본 에이전트)';
COMMENT ON COLUMN chat_session.updated_at        IS '마지막 대화 시각';
COMMENT ON COLUMN agent_run.stop_reason          IS '종료 사유 (completed, max_turns, denied)';
COMMENT ON COLUMN message.seq                    IS '세션 내 순서';
COMMENT ON COLUMN message.tool_call_id           IS 'role=tool 일 때 대응 툴 호출 ID';
COMMENT ON COLUMN tool_call.call_id              IS '툴 호출 ID (LLM 이 준 id)';
COMMENT ON COLUMN tool_call.seq                  IS '응답 내 호출 순서';
COMMENT ON COLUMN tool_call.result_content       IS 'LLM 에게 돌려준 결과';
COMMENT ON COLUMN policy_rule.priority           IS '매칭 순서 (작을수록 먼저)';
COMMENT ON COLUMN policy_rule.conditions         IS 'ABAC 조건 (사용자 속성, 툴 인자)';
COMMENT ON COLUMN audit_log.agent_id             IS '호출한 에이전트 ID (기록용, FK 없음)';
COMMENT ON COLUMN audit_log.principal_snapshot   IS '판정 시점 요청자 속성';
COMMENT ON COLUMN audit_log.approval             IS 'ASK 일 때 승인 결과 (approved, rejected, timeout)';
COMMENT ON COLUMN audit_log.ts                   IS '판정 시각 (UTC)';
COMMENT ON COLUMN document.scope                 IS '공개 범위 (personal, dept, company)';
COMMENT ON COLUMN document_version.is_current    IS '현재 버전 여부';
COMMENT ON COLUMN chunk.embedding                IS 'bge-m3 임베딩 (1024차원)';
COMMENT ON COLUMN citation.score                 IS '검색 유사도';
