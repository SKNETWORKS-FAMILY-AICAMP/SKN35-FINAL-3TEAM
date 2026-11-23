"""[초안 — DB 담당 확인 전] 퇴사(삭제)한 직원의 메일을 관리자용 보관 표에 옮겨 두기 (2026-11-23, 커넥터 담당 제안)

0005 에서는 직원(users)이 지워지면 그 직원의 메일도 함께 지워졌다 (ON DELETE CASCADE).
이제는 지우기 직전에 메일을 mail_archive 로 복사하고, 그다음에 원래 표에서 지운다.

- mail_archive  퇴사한 직원의 메일. 직원이 이미 지워졌으므로 users 외래키는 걸 수 없고,
                대신 당시 직원 정보(former_*)를 같이 적어 둔다.
- 트리거 trg_users_archive_mail  users 행을 지우기 직전에 (BEFORE DELETE) 그 직원의 mail_message 를 복사한다.
                앱이 어떤 경로로 직원을 지워도 DB 가 알아서 보관하므로 코드를 빼먹을 수 없다.

보관 표는 MCP 툴로 열어 주지 않는다 (에이전트가 퇴사자 메일을 읽을 수 없음). 관리자가 CLI(gmail-archive)나 DB 로 본다.
mail_account · mail_sync_* 는 보관하지 않고 그대로 지운다 (연결 계정 주소만 보관 행의 account_email 에 남김).
users.id 는 GENERATED ALWAYS AS IDENTITY 라서 다른 직원이 같은 ID 를 다시 받지 않는다 → former_user_id 로 구분된다.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-23
"""
from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
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
)""")
    op.execute("CREATE INDEX ix_mail_archive_user_thread ON mail_archive (former_user_id, thread_id)")
    op.execute("CREATE INDEX ix_mail_archive_user_sent ON mail_archive (former_user_id, sent_at DESC)")
    op.execute("CREATE INDEX ix_mail_archive_login ON mail_archive (former_login_id)")
    op.execute("COMMENT ON TABLE mail_archive IS '퇴사(삭제)한 직원의 메일 보관 — 관리자만 조회. users 외래키 없음'")
    for column, text in {
        "former_user_id": "삭제된 직원의 users.id (다른 직원이 다시 받지 않음)",
        "former_login_id": "삭제 당시 로그인 ID",
        "former_name": "삭제 당시 이름",
        "former_dept_id": "삭제 당시 소속 부서 ID (부서가 지워져도 남도록 외래키 없음)",
        "account_email": "연결해 두었던 메일 주소",
        "archived_at": "보관 시각 (직원이 삭제된 시각)",
    }.items():
        op.execute(f"COMMENT ON COLUMN mail_archive.{column} IS '{text}'")

    op.execute("""
CREATE FUNCTION archive_user_mail() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO mail_archive (former_user_id, message_id, former_login_id, former_name, former_dept_id, account_email,
                              thread_id, sender, to_addrs, cc_addrs, bcc_addrs, subject, sent_at, labels, body,
                              body_omitted, size_bytes, content_hash, synced_at)
    SELECT OLD.id, m.message_id, OLD.login_id, OLD.name, OLD.dept_id, a.email,
           m.thread_id, m.sender, m.to_addrs, m.cc_addrs, m.bcc_addrs, m.subject, m.sent_at, m.labels, m.body,
           m.body_omitted, m.size_bytes, m.content_hash, m.synced_at
    FROM mail_message m
    LEFT JOIN mail_account a ON a.owner_user_id = m.owner_user_id
    WHERE m.owner_user_id = OLD.id
    ON CONFLICT (former_user_id, message_id) DO NOTHING;
    RETURN OLD;
END;
$$""")
    op.execute("CREATE TRIGGER trg_users_archive_mail BEFORE DELETE ON users "
               "FOR EACH ROW EXECUTE FUNCTION archive_user_mail()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_users_archive_mail ON users")
    op.execute("DROP FUNCTION IF EXISTS archive_user_mail()")
    op.execute("DROP TABLE IF EXISTS mail_archive")
