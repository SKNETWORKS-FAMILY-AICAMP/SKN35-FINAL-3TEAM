"""[초안 — DB 담당 확인 전] 개인 메일 저장 표 5개 (2026-11-23, 커넥터 담당 제안)

직원이 자기 Google 계정을 연결하고, 그 직원의 메일을 관계형 DB 에만 저장한다 (벡터DB · chunk 대상 아님).
커넥터(mcp-servers/gmail)가 쓰는 표이고, 읽기·쓰기는 모두 owner_user_id = 로그인 사용자 조건으로만 한다.

- mail_account      직원 ↔ 연결한 메일 주소
- mail_message      메일 한 통 = 한 행, 기본키 (owner_user_id, message_id)
- mail_sync_state   직원별 증분 동기화 진행 상태
- mail_sync_failure 동기화 실패 사유
- mail_sync_run     동기화 실행 기록

직원(users)이 지워지면 메일도 함께 지워진다 (ON DELETE CASCADE). 단 0012 트리거가 지우기 직전에 mail_archive 로 복사해 둔다.
기존 표는 건드리지 않는다. 앱 권한(agent_app)은 0002 의 기본 권한 규칙이 새 표에도 적용한다.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-23
"""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLES = ("mail_sync_run", "mail_sync_failure", "mail_sync_state", "mail_message", "mail_account")


def upgrade() -> None:
    op.execute("""
CREATE TABLE mail_account (
    owner_user_id  BIGINT       PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    email          VARCHAR(255) NOT NULL,
    connected_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
)""")
    op.execute("""
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
)""")
    op.execute("CREATE INDEX ix_mail_owner_thread ON mail_message (owner_user_id, thread_id)")
    op.execute("CREATE INDEX ix_mail_owner_sent ON mail_message (owner_user_id, sent_at DESC)")
    op.execute("""
CREATE TABLE mail_sync_state (
    owner_user_id  BIGINT           PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    last_sync_end  DOUBLE PRECISION,
    window_start   DOUBLE PRECISION,
    window_end     DOUBLE PRECISION,
    checkpoint     JSONB,
    job_id         VARCHAR(64)
)""")
    op.execute("""
CREATE TABLE mail_sync_failure (
    id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id  BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    job_id         VARCHAR(64) NOT NULL,
    message_id     VARCHAR(64),
    reason         VARCHAR(30) NOT NULL,
    detail         TEXT        NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)""")
    op.execute("CREATE INDEX ix_mail_failure_owner_job ON mail_sync_failure (owner_user_id, job_id)")
    op.execute("""
CREATE TABLE mail_sync_run (
    id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id  BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    job_id         VARCHAR(64) NOT NULL,
    mode           VARCHAR(20) NOT NULL,
    summary        JSONB       NOT NULL,
    finished_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)""")
    op.execute("CREATE INDEX ix_mail_run_owner ON mail_sync_run (owner_user_id, id)")

    comments = {
        "mail_account": "직원이 연결한 개인 메일 계정 (직원 1명당 1개)",
        "mail_message": "직원 개인 메일 (메일 한 통 = 한 행). 본인만 조회 가능",
        "mail_sync_state": "직원별 증분 동기화 진행 상태",
        "mail_sync_failure": "동기화 중 실패한 메일과 사유",
        "mail_sync_run": "동기화 실행 기록 (감사용)",
    }
    for table, text in comments.items():
        op.execute(f"COMMENT ON TABLE {table} IS '{text}'")
    for column, text in {
        "mail_account.owner_user_id": "직원 ID (users.id)",
        "mail_account.email": "연결한 메일 주소",
        "mail_message.owner_user_id": "메일함 주인 (users.id) — 이 사람만 조회 가능",
        "mail_message.message_id": "Gmail 메시지 ID",
        "mail_message.thread_id": "Gmail 스레드 ID",
        "mail_message.bcc_addrs": "숨은참조 목록 (본인이 보낸 메일에만 있음)",
        "mail_message.body": "본문 텍스트",
        "mail_message.body_omitted": "본문 일부가 너무 커서 생략됨",
        "mail_message.content_hash": "저장 내용 해시 — 같으면 다시 쓰지 않음",
        "mail_sync_state.last_sync_end": "마지막으로 끝까지 성공한 동기화 창의 끝 (epoch 초)",
        "mail_sync_state.checkpoint": "진행 중 체크포인트 (다음 페이지 토큰) — NULL 이면 진행 중 아님",
        "mail_sync_failure.reason": "fetch_error / too_large / invalid_message",
        "mail_sync_run.mode": "incremental / full / prune",
        "mail_sync_run.summary": "건수 · 사유별 실패 수",
    }.items():
        op.execute(f"COMMENT ON COLUMN {column} IS '{text}'")


def downgrade() -> None:
    for table in TABLES:  # 자식 표부터
        op.execute(f"DROP TABLE IF EXISTS {table}")
