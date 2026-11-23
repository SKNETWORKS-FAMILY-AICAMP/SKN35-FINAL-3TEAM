-- 업무 자동화 에이전트 DB 스키마 (PostgreSQL 16 + pgvector)
-- 기준: docs/architecture/erd.sql (ERDCloud 용 MySQL 문법). 테이블·컬럼·관계는 같고 타입만 Postgres 에 맞췄다.
-- 추가: CHECK 제약(상태값), UNIQUE, ON DELETE 규칙, 검색용 인덱스
-- 변경: chunk.vector_id 대신 pgvector embedding 컬럼 (벡터 DB 미결 → pgvector 제안)
-- chat_session.updated_at 은 message 추가 트리거로 갱신된다.
--
-- 2026-11-20 v5 제안(erd_v5_schema.postgres.sql) 일부 반영. 이름은 코드와 같은 dept_id·security_grade 를 유지했다.
--   반영: agent 생성·수정 시각, agent_template(+tool), agent_assignment, agent_knowledge,
--         message·tool_call.has_work_data, audit_log 확장(event_type·target),
--         doc.category·status, doc_version 메타(sha256·시행일·파싱본), chunk.heading_path,
--         work_file(session_doc 대체), output_file, output_source
--   바꿔서 반영: doc.scope 는 남기되 personal 을 뺐다 (개인 파일은 work_file 로). 부서 문서는 계속 지원.
--   보류: share_request_file, agent_attachment, output_publish_request, user_memory (흐름이 정해지면 추가)

CREATE EXTENSION IF NOT EXISTS vector;

-- ── 조직 ────────────────────────────────────────────────

CREATE TABLE dept (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    head_user_id  BIGINT  -- FK 는 users 생성 후 아래에서 추가 (순환 참조)
);

CREATE TABLE users (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    login_id        VARCHAR(100) NOT NULL UNIQUE,
    name            VARCHAR(100) NOT NULL,
    dept_id         BIGINT NOT NULL REFERENCES dept (id),
    position        VARCHAR(50),
    security_grade  INT NOT NULL DEFAULT 1
);

ALTER TABLE dept
    ADD CONSTRAINT fk_dept_head FOREIGN KEY (head_user_id) REFERENCES users (id);

CREATE TABLE user_role (
    role     VARCHAR(50) NOT NULL CHECK (role IN ('admin', 'dept_head', 'member')),
    user_id  BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    PRIMARY KEY (role, user_id)
);

-- ── 에이전트 ────────────────────────────────────────────

-- 관리자가 검증한 시작점 (UC-01~05). 직원은 템플릿을 골라 내 에이전트로 저장한다.
-- 코드: apps/api/src/services/templates.py (지금은 config/agent_templates.yaml)
CREATE TABLE agent_template (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           VARCHAR(100) NOT NULL,
    description    TEXT,
    system_prompt  TEXT NOT NULL,
    example        TEXT,  -- 이 템플릿으로 만든 에이전트에게 보낼 요청 예시
    created_by     BIGINT NOT NULL REFERENCES users (id),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()  -- 복제본의 created_at 보다 늦으면 주인에게 알린다
);

CREATE TABLE agent_template_tool (
    tool_name    VARCHAR(100) NOT NULL,
    template_id  BIGINT NOT NULL REFERENCES agent_template (id) ON DELETE CASCADE,
    PRIMARY KEY (tool_name, template_id)
);

CREATE TABLE agent (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id  BIGINT NOT NULL REFERENCES users (id),
    name           VARCHAR(100) NOT NULL,
    description    TEXT,
    system_prompt  TEXT NOT NULL,
    scope          VARCHAR(20) NOT NULL DEFAULT 'personal' CHECK (scope IN ('personal', 'dept', 'company')),
    status         VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    template_id    BIGINT REFERENCES agent_template (id) ON DELETE SET NULL,  -- 만들 때 고른 템플릿
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
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

-- scope=dept 에이전트를 쓸 수 있는 부서. 부서 공유가 승인되면 요청자 부서가 들어가고,
-- 관리자가 다른 부서에 더 배정할 수 있다.
CREATE TABLE agent_assignment (
    agent_id     BIGINT NOT NULL REFERENCES agent (id) ON DELETE CASCADE,
    dept_id      BIGINT NOT NULL REFERENCES dept (id) ON DELETE CASCADE,
    assigned_by  BIGINT NOT NULL REFERENCES users (id),
    assigned_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, dept_id)
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
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id     UUID NOT NULL REFERENCES chat_session (id) ON DELETE CASCADE,
    run_id         UUID REFERENCES agent_run (id) ON DELETE SET NULL,
    seq            INT NOT NULL,
    role           VARCHAR(20) NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content        TEXT,
    tool_call_id   VARCHAR(100),  -- role=tool 일 때 대응 툴 호출 ID (순환 참조라 FK 없음)
    has_work_data  BOOLEAN NOT NULL DEFAULT FALSE,  -- 작업 파일 내용이 들어감 → 파일 만료 때 content 를 지운다
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
    has_work_data   BOOLEAN NOT NULL DEFAULT FALSE,  -- 결과에 작업 파일 내용 → 파일 만료 때 result_content 를 지운다
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
-- 툴 호출 말고도 문서 등록, 작업 파일 업로드·삭제, 산출물 내려받기, 에이전트 공유를 기록한다.
-- 그래서 call_id·session_id·tool_name·decision 은 툴 호출일 때만 필수다.
CREATE TABLE audit_log (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type          VARCHAR(30) NOT NULL DEFAULT 'tool_call'
                        CHECK (event_type IN ('tool_call', 'doc_register', 'work_upload', 'work_purge',
                                              'output_download', 'agent_share')),
    call_id             VARCHAR(100) REFERENCES tool_call (call_id),
    session_id          UUID,
    user_id             BIGINT NOT NULL,
    agent_id            BIGINT,  -- 기록용, FK 없음
    tool_name           VARCHAR(100),
    arguments           JSONB,
    principal_snapshot  JSONB,
    decision            VARCHAR(10) CHECK (decision IN ('allow', 'deny', 'ask')),
    reason              TEXT,
    rule_id             VARCHAR(100) REFERENCES policy_rule (policy_rule_id),
    approval            VARCHAR(20) CHECK (approval IN ('approved', 'rejected', 'timeout')),
    executed            BOOLEAN NOT NULL DEFAULT FALSE,
    tool_ok             BOOLEAN,  -- 실행했을 때 툴 성공 여부, 실행 안 했으면 NULL
    target_type         VARCHAR(30) CHECK (target_type IN ('doc', 'work_file', 'output_file', 'agent')),
    target_id           BIGINT,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (event_type <> 'tool_call'
           OR (call_id IS NOT NULL AND session_id IS NOT NULL AND tool_name IS NOT NULL AND decision IS NOT NULL))
);

CREATE INDEX idx_audit_log_user_ts ON audit_log (user_id, ts);
CREATE INDEX idx_audit_log_tool_ts ON audit_log (tool_name, ts);
CREATE INDEX idx_audit_log_ts      ON audit_log (ts);
CREATE INDEX idx_audit_log_target  ON audit_log (target_type, target_id);

-- ── 문서 (사내 지식) ────────────────────────────────────
-- 관리자(또는 부서 관리자)가 등록한 문서만 들어간다. 직원이 대화에 올린 파일은 work_file 로 간다.

CREATE TABLE doc (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id   BIGINT NOT NULL REFERENCES users (id),       -- 등록한 사람
    owner_dept_id   BIGINT NOT NULL REFERENCES dept (id),  -- 관리 책임 부서 (scope=dept 면 읽을 수 있는 부서)
    title           VARCHAR(255) NOT NULL,
    category        VARCHAR(30) NOT NULL CHECK (category IN ('내규', '매뉴얼', '상품', '법령', '공문')),
    scope           VARCHAR(20) NOT NULL DEFAULT 'company' CHECK (scope IN ('dept', 'company')),
    security_grade  INT NOT NULL DEFAULT 1,
    status          VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired'))  -- 폐지된 규정은 retired
);

CREATE TABLE doc_version (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id     BIGINT NOT NULL REFERENCES doc (id) ON DELETE CASCADE,
    version         INT NOT NULL,
    is_current      BOOLEAN NOT NULL DEFAULT TRUE,
    file_name       VARCHAR(255) NOT NULL,
    mime_type       VARCHAR(100) NOT NULL,
    storage_path    VARCHAR(500) NOT NULL,  -- knowledge/{doc_id}/{version}/원본
    status          VARCHAR(20) NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'ready', 'failed')),
    sha256          CHAR(64) NOT NULL,                        -- 같은 파일 중복 등록 막기
    effective_date  DATE,                                     -- 시행일
    parse_profile   VARCHAR(20) NOT NULL DEFAULT 'accurate',  -- 파싱 설정
    parsed_path     VARCHAR(500),                             -- parse_result/1 JSON
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (doc_id, version)
);

-- 문서마다 현재 버전은 하나만
CREATE UNIQUE INDEX uq_doc_version_current ON doc_version (doc_id) WHERE is_current;
CREATE UNIQUE INDEX uq_doc_version_sha256  ON doc_version (doc_id, sha256);

CREATE TABLE chunk (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_version_id  BIGINT NOT NULL REFERENCES doc_version (id) ON DELETE CASCADE,
    chunk_index          INT NOT NULL,
    page                 INT,
    content              TEXT NOT NULL,
    owner_dept_id        BIGINT NOT NULL,  -- 문서에서 상속
    scope                VARCHAR(20) NOT NULL CHECK (scope IN ('dept', 'company')),  -- 문서에서 상속
    security_grade       INT NOT NULL,     -- 문서에서 상속
    heading_path         TEXT[],           -- parse_result elements.heading_path (예: {제3장, 제42조})
    embedding            vector(1024),     -- bge-m3 (EMBEDDING_DIM=1024)
    UNIQUE (doc_version_id, chunk_index)
);

CREATE INDEX idx_chunk_embedding ON chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_chunk_access    ON chunk (scope, owner_dept_id, security_grade);

CREATE TABLE citation (
    message_id  BIGINT NOT NULL REFERENCES message (id) ON DELETE CASCADE,
    chunk_id    BIGINT NOT NULL REFERENCES chunk (id) ON DELETE CASCADE,
    score       REAL,
    PRIMARY KEY (message_id, chunk_id)
);

-- 에이전트가 검색할 문서. 문서 하나 또는 분류 전체 중 하나만 지정한다. 행이 없으면 읽을 수 있는 문서 전체.
CREATE TABLE agent_knowledge (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_id     BIGINT NOT NULL REFERENCES agent (id) ON DELETE CASCADE,
    doc_id  BIGINT REFERENCES doc (id) ON DELETE CASCADE,
    category     VARCHAR(30) CHECK (category IN ('내규', '매뉴얼', '상품', '법령', '공문')),
    CHECK ((doc_id IS NULL) <> (category IS NULL))
);

-- ── 작업 파일 ───────────────────────────────────────────
-- 직원이 이번 대화에서 쓰려고 직접 올린 파일 (예전 session_doc 자리).
-- 원본은 파싱을 확인하면 지우고, 파싱본만 만료 시각까지 둔다.
-- 대화가 지워져도 이 행은 감사용으로 남는다 (session_id → NULL). 파일 실물은 정리 작업이 지운다.

CREATE TABLE work_file (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id           UUID REFERENCES chat_session (id) ON DELETE SET NULL,  -- 툴은 이 대화 안에서만 읽음
    user_id              BIGINT NOT NULL REFERENCES users (id),                 -- 올린 사람 (본인만 접근)
    file_name            VARCHAR(255) NOT NULL,
    mime_type            VARCHAR(100) NOT NULL,
    size_bytes           BIGINT NOT NULL,
    sha256               CHAR(64) NOT NULL,                   -- 삭제 후에도 남김 (감사)
    storage_path         VARCHAR(500),                        -- 원본 work/{session_id}/… 파싱 확인 후 NULL
    original_deleted_at  TIMESTAMPTZ,
    parsed_path          VARCHAR(500),                        -- parse_result/1 JSON. 만료 시 NULL
    pii_found            BOOLEAN NOT NULL DEFAULT FALSE,      -- 파싱 직후 PII 검사 결과
    pii_summary          JSONB,                               -- 종류별 건수만. 예: {"이름": 12, "계좌번호": 30}
    status               VARCHAR(20) NOT NULL DEFAULT 'uploaded'
                         CHECK (status IN ('uploaded', 'parsing', 'ready', 'failed', 'expired')),
    uploaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 툴이 쓸 때마다 갱신
    expires_at           TIMESTAMPTZ NOT NULL,                -- last_used_at + N일 (N 은 관리자 정책, 미정)
    deleted_at           TIMESTAMPTZ                          -- 파싱본까지 지운 시각
);

CREATE INDEX idx_work_file_session ON work_file (session_id);
CREATE INDEX idx_work_file_expires ON work_file (expires_at) WHERE deleted_at IS NULL;

-- ── 산출물 ──────────────────────────────────────────────

CREATE TABLE output_file (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id      BIGINT NOT NULL REFERENCES users (id),
    session_id         UUID REFERENCES chat_session (id) ON DELETE SET NULL,  -- 만든 대화
    run_id             UUID REFERENCES agent_run (id) ON DELETE SET NULL,     -- 만든 실행
    title              VARCHAR(200) NOT NULL,
    file_name          VARCHAR(255) NOT NULL,
    mime_type          VARCHAR(100) NOT NULL,
    storage_path       VARCHAR(500) NOT NULL,  -- outputs/{user_id}/…
    sha256             CHAR(64) NOT NULL,
    has_customer_info  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at         TIMESTAMPTZ
);

CREATE INDEX idx_output_file_owner ON output_file (owner_user_id, created_at);

-- 산출물을 만들 때 쓴 입력 (둘 중 하나만)
CREATE TABLE output_source (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    output_id            BIGINT NOT NULL REFERENCES output_file (id) ON DELETE CASCADE,
    work_file_id         BIGINT REFERENCES work_file (id),
    doc_version_id  BIGINT REFERENCES doc_version (id),
    CHECK ((work_file_id IS NULL) <> (doc_version_id IS NULL))
);

-- ── 설명 (COMMENT ON) ───────────────────────────────────

COMMENT ON TABLE dept                IS '부서';
COMMENT ON TABLE users               IS '사용자';
COMMENT ON TABLE user_role           IS '사용자 역할 (admin > dept_head > member)';
COMMENT ON TABLE agent_template      IS '관리자가 만든 에이전트 템플릿';
COMMENT ON TABLE agent_template_tool IS '템플릿에 들어 있는 툴';
COMMENT ON TABLE agent               IS '에이전트 (개인·부서·전사)';
COMMENT ON TABLE agent_tool          IS '에이전트가 쓸 수 있는 툴 (실행 시 권한은 실행자 기준으로 따로 판정)';
COMMENT ON TABLE agent_share_request IS '에이전트 부서·전사 공유 요청 (바로 위 관리자 승인)';
COMMENT ON TABLE agent_assignment    IS '부서 에이전트를 쓸 수 있는 부서';
COMMENT ON TABLE chat_session        IS '대화 세션';
COMMENT ON TABLE agent_run           IS '에이전트 실행 (요청 1건)';
COMMENT ON TABLE message             IS '대화 메시지';
COMMENT ON TABLE tool_call           IS '툴 호출';
COMMENT ON TABLE approval_request    IS 'ASK 판정 툴 호출의 승인 요청';
COMMENT ON TABLE policy_rule         IS '권한 정책 규칙 (priority 순으로 처음 맞는 규칙 적용)';
COMMENT ON TABLE audit_log           IS '감사 로그 (툴 호출, 문서 등록, 파일 업로드·삭제·내려받기, 에이전트 공유)';
COMMENT ON TABLE doc            IS '사내 문서 (관리자 등록)';
COMMENT ON TABLE doc_version    IS '문서 버전';
COMMENT ON TABLE chunk               IS '문서 청크 (권한 속성은 문서에서 상속)';
COMMENT ON TABLE citation            IS '답변 근거';
COMMENT ON TABLE agent_knowledge     IS '에이전트가 검색할 문서 (문서 또는 분류)';
COMMENT ON TABLE work_file           IS '작업 파일 (직원이 대화에 올린 파일, 파싱본만 만료 때까지 보관)';
COMMENT ON TABLE output_file         IS 'AI 산출물';
COMMENT ON TABLE output_source       IS '산출물을 만들 때 쓴 입력';

COMMENT ON COLUMN dept.head_user_id              IS '부서장 사용자 ID';
COMMENT ON COLUMN users.position                 IS '직급';
COMMENT ON COLUMN users.security_grade           IS '보안등급';
COMMENT ON COLUMN agent.scope                    IS '공개 범위 (personal, dept, company)';
COMMENT ON COLUMN agent.template_id              IS '만들 때 고른 템플릿 ID';
COMMENT ON COLUMN agent_share_request.approver_id IS '승인할 사용자 ID (바로 위 관리자)';
COMMENT ON COLUMN chat_session.agent_id          IS '사용한 에이전트 ID (NULL 이면 기본 에이전트)';
COMMENT ON COLUMN chat_session.updated_at        IS '마지막 대화 시각';
COMMENT ON COLUMN agent_run.stop_reason          IS '종료 사유 (completed, max_turns, denied)';
COMMENT ON COLUMN message.seq                    IS '세션 내 순서';
COMMENT ON COLUMN message.tool_call_id           IS 'role=tool 일 때 대응 툴 호출 ID';
COMMENT ON COLUMN message.has_work_data          IS '작업 파일 내용 포함 여부 (만료 때 content 삭제 대상)';
COMMENT ON COLUMN tool_call.call_id              IS '툴 호출 ID (LLM 이 준 id)';
COMMENT ON COLUMN tool_call.seq                  IS '응답 내 호출 순서';
COMMENT ON COLUMN tool_call.result_content       IS 'LLM 에게 돌려준 결과';
COMMENT ON COLUMN tool_call.has_work_data        IS '결과에 작업 파일 내용 포함 여부';
COMMENT ON COLUMN policy_rule.priority           IS '매칭 순서 (작을수록 먼저)';
COMMENT ON COLUMN policy_rule.conditions         IS 'ABAC 조건 (사용자 속성, 툴 인자)';
COMMENT ON COLUMN audit_log.event_type           IS '기록 종류 (tool_call 이면 call_id·session_id·tool_name·decision 필수)';
COMMENT ON COLUMN audit_log.agent_id             IS '호출한 에이전트 ID (기록용, FK 없음)';
COMMENT ON COLUMN audit_log.principal_snapshot   IS '판정 시점 요청자 속성';
COMMENT ON COLUMN audit_log.approval             IS 'ASK 일 때 승인 결과 (approved, rejected, timeout)';
COMMENT ON COLUMN audit_log.tool_ok              IS '실행했을 때 툴 성공 여부 (실행 안 했으면 NULL)';
COMMENT ON COLUMN audit_log.ts                   IS '판정 시각 (UTC)';
COMMENT ON COLUMN doc.category              IS '분류 (내규, 매뉴얼, 상품, 법령, 공문)';
COMMENT ON COLUMN doc.scope                 IS '공개 범위 (dept, company)';
COMMENT ON COLUMN doc.owner_dept_id         IS '관리 책임 부서';
COMMENT ON COLUMN doc_version.is_current    IS '현재 버전 여부';
COMMENT ON COLUMN doc_version.parsed_path   IS 'parse_result/1 JSON 경로';
COMMENT ON COLUMN chunk.heading_path             IS '제목 경로 (parse_result elements.heading_path)';
COMMENT ON COLUMN chunk.embedding                IS 'bge-m3 임베딩 (1024차원)';
COMMENT ON COLUMN citation.score                 IS '검색 유사도';
COMMENT ON COLUMN work_file.expires_at           IS '파싱본 만료 시각 (last_used_at + 관리자 정책 일수)';
COMMENT ON COLUMN output_file.has_customer_info  IS '고객정보 포함 여부';
