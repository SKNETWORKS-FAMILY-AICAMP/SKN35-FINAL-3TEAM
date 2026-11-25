-- 개인 메일 저장 표 5개 (PostgreSQL 16). 같은 정의가 alembic/versions/0011_mail_tables.py 와
-- apps/api/src/db/models.py 에 있다 (표를 만들고 바꾸는 건 마이그레이션이 한다 — 이 파일은 읽기용 사본).
-- 개발·테스트 중 DB 가 없을 때는 같은 구조를 SQLite (gmail_mcp/store.py) 로 쓴다.
--
-- 원칙
--   · 직원 개인 메일은 벡터DB · chunk 에 넣지 않고 이 표에만 저장한다.
--   · 모든 조회는 owner_user_id = 로그인 사용자 조건을 붙인다 (커넥터 코드가 강제).
--   · 기존 document · document_version · chunk 는 건드리지 않는다.
--   · 직원(users)이 지워지면 그 직원의 메일은 mail_archive 로 먼저 복사된 뒤 지워진다 (아래 트리거, 관리자만 조회).

CREATE TABLE mail_account (
    owner_user_id  BIGINT       PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    email          VARCHAR(255) NOT NULL,
    connected_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
COMMENT ON TABLE  mail_account               IS '직원이 연결한 개인 메일 계정 (직원 1명당 1개)';
COMMENT ON COLUMN mail_account.owner_user_id IS '직원 ID (users.id)';
COMMENT ON COLUMN mail_account.email         IS '연결한 메일 주소';
COMMENT ON COLUMN mail_account.connected_at  IS '연결 시각';

CREATE TABLE mail_message (
    owner_user_id  BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    message_id     VARCHAR(64) NOT NULL,
    thread_id      VARCHAR(64) NOT NULL,
    sender         TEXT        NOT NULL DEFAULT '',
    to_addrs       JSONB       NOT NULL DEFAULT '[]',
    cc_addrs       JSONB       NOT NULL DEFAULT '[]',
    bcc_addrs      JSONB       NOT NULL DEFAULT '[]',
    subject        TEXT        NOT NULL DEFAULT '',
    sent_at        TIMESTAMPTZ,
    labels         JSONB       NOT NULL DEFAULT '[]',
    body           TEXT        NOT NULL DEFAULT '',
    body_omitted   BOOLEAN     NOT NULL DEFAULT false,
    size_bytes     INT         NOT NULL DEFAULT 0,
    content_hash   VARCHAR(80) NOT NULL,
    synced_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_user_id, message_id)
);
CREATE INDEX ix_mail_owner_thread ON mail_message (owner_user_id, thread_id);
CREATE INDEX ix_mail_owner_sent   ON mail_message (owner_user_id, sent_at DESC);
COMMENT ON TABLE  mail_message               IS '직원 개인 메일 (메일 한 통 = 한 행). 본인만 조회 가능';
COMMENT ON COLUMN mail_message.owner_user_id IS '메일함 주인 (users.id) — 이 사람만 조회 가능';
COMMENT ON COLUMN mail_message.message_id    IS 'Gmail 메시지 ID';
COMMENT ON COLUMN mail_message.thread_id     IS 'Gmail 스레드 ID';
COMMENT ON COLUMN mail_message.sender        IS '보낸 사람';
COMMENT ON COLUMN mail_message.to_addrs      IS '받는 사람 목록';
COMMENT ON COLUMN mail_message.cc_addrs      IS '참조 목록';
COMMENT ON COLUMN mail_message.bcc_addrs     IS '숨은참조 목록 (본인이 보낸 메일에만 있음)';
COMMENT ON COLUMN mail_message.subject       IS '제목';
COMMENT ON COLUMN mail_message.sent_at       IS '보낸 시각';
COMMENT ON COLUMN mail_message.labels        IS 'Gmail 라벨';
COMMENT ON COLUMN mail_message.body          IS '본문 텍스트';
COMMENT ON COLUMN mail_message.body_omitted  IS '본문 일부가 너무 커서 생략됨';
COMMENT ON COLUMN mail_message.size_bytes    IS '원본 크기';
COMMENT ON COLUMN mail_message.content_hash  IS '저장 내용 해시 — 같으면 다시 쓰지 않음';
COMMENT ON COLUMN mail_message.synced_at     IS '마지막 저장 시각';

CREATE TABLE mail_sync_state (
    owner_user_id  BIGINT           PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    last_sync_end  DOUBLE PRECISION,
    window_start   DOUBLE PRECISION,
    window_end     DOUBLE PRECISION,
    checkpoint     JSONB,
    job_id         VARCHAR(64)
);
COMMENT ON TABLE  mail_sync_state               IS '직원별 증분 동기화 진행 상태';
COMMENT ON COLUMN mail_sync_state.last_sync_end IS '마지막으로 끝까지 성공한 동기화 창의 끝 (epoch 초)';
COMMENT ON COLUMN mail_sync_state.window_start  IS '진행 중인 창 시작 (epoch 초)';
COMMENT ON COLUMN mail_sync_state.window_end    IS '진행 중인 창 끝 (epoch 초)';
COMMENT ON COLUMN mail_sync_state.checkpoint    IS '진행 중 체크포인트 (다음 페이지 토큰) — NULL 이면 진행 중 아님';
COMMENT ON COLUMN mail_sync_state.job_id        IS '진행 중 작업 ID';

CREATE TABLE mail_sync_failure (
    id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id  BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    job_id         VARCHAR(64) NOT NULL,
    message_id     VARCHAR(64),
    reason         VARCHAR(30) NOT NULL,
    detail         TEXT        NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_mail_failure_owner_job ON mail_sync_failure (owner_user_id, job_id);
COMMENT ON TABLE  mail_sync_failure        IS '동기화 중 실패한 메일과 사유';
COMMENT ON COLUMN mail_sync_failure.reason IS 'fetch_error / too_large / invalid_message';

CREATE TABLE mail_sync_run (
    id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id  BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    job_id         VARCHAR(64) NOT NULL,
    mode           VARCHAR(20) NOT NULL,
    summary        JSONB       NOT NULL,
    finished_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_mail_run_owner ON mail_sync_run (owner_user_id, id);
COMMENT ON TABLE  mail_sync_run         IS '동기화 실행 기록 (감사용)';
COMMENT ON COLUMN mail_sync_run.mode    IS 'incremental / full / prune';
COMMENT ON COLUMN mail_sync_run.summary IS '건수 · 사유별 실패 수';

-- 퇴사(삭제)한 직원의 메일 보관 — alembic 0012. 직원이 이미 없으므로 users 외래키는 없다.
CREATE TABLE mail_archive (
    former_user_id  BIGINT       NOT NULL,
    message_id      VARCHAR(64)  NOT NULL,
    former_login_id VARCHAR(100) NOT NULL,
    former_name     VARCHAR(100) NOT NULL,
    former_dept_id  BIGINT,
    account_email   VARCHAR(255),
    thread_id       VARCHAR(64)  NOT NULL,
    sender          TEXT         NOT NULL DEFAULT '',
    to_addrs        JSONB        NOT NULL DEFAULT '[]',
    cc_addrs        JSONB        NOT NULL DEFAULT '[]',
    bcc_addrs       JSONB        NOT NULL DEFAULT '[]',
    subject         TEXT         NOT NULL DEFAULT '',
    sent_at         TIMESTAMPTZ,
    labels          JSONB        NOT NULL DEFAULT '[]',
    body            TEXT         NOT NULL DEFAULT '',
    body_omitted    BOOLEAN      NOT NULL DEFAULT false,
    size_bytes      INT          NOT NULL DEFAULT 0,
    content_hash    VARCHAR(80)  NOT NULL,
    synced_at       TIMESTAMPTZ  NOT NULL,
    archived_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (former_user_id, message_id)
);
CREATE INDEX ix_mail_archive_user_thread ON mail_archive (former_user_id, thread_id);
CREATE INDEX ix_mail_archive_user_sent   ON mail_archive (former_user_id, sent_at DESC);
CREATE INDEX ix_mail_archive_login       ON mail_archive (former_login_id);

-- users 행을 지우기 직전(BEFORE DELETE)에 그 직원의 메일을 mail_archive 로 복사한다. (이후 ON DELETE CASCADE 가 원본을 지움)
-- 함수 본문은 alembic/versions/0012_mail_archive.py 의 archive_user_mail() 참고.
