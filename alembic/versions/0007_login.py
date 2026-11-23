"""로그인: 비밀번호·계정 잠금 칸, 로그인 세션 표, 로그인 감사 기록 (2026-11-23)

1) users 에 칸 추가
   password_hash       비밀번호 해시 (scrypt). 원문은 어디에도 저장하지 않는다. NULL = 비밀번호 로그인 불가(SSO 전용 등)
   is_active           FALSE 면 로그인·세션 모두 거부 (퇴사·정지)
   failed_login_count  연속 실패 횟수. 성공하면 0
   locked_until        이 시각까지 로그인 거부 (연속 실패 잠금)
   last_login_at       마지막 로그인 성공 시각
2) login_session 표 — 로그인하면 한 줄. 브라우저 쿠키에는 임의 토큰만 주고, DB 에는 그 토큰의 SHA-256 만 둔다
   (DB 가 새도 쿠키를 만들 수 없게). 로그아웃·강제 종료 = revoked_at 채우기.
3) audit_log.event_type 허용값에 login · login_failed · logout 추가 (누가 언제 들어왔는지도 감사 로그에 남긴다)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-23
"""
from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

EVENTS_OLD = "'tool_call', 'doc_register', 'work_upload', 'work_purge', 'output_download', 'agent_share'"
EVENTS_NEW = EVENTS_OLD + ", 'login', 'login_failed', 'logout'"


def _event_check(values: str) -> None:
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT audit_log_event_type_check")
    op.execute(f"ALTER TABLE audit_log ADD CONSTRAINT audit_log_event_type_check CHECK (event_type IN ({values}))")


def upgrade() -> None:
    op.execute("""
        ALTER TABLE users
            ADD COLUMN password_hash      VARCHAR(255),
            ADD COLUMN is_active          BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN failed_login_count INT NOT NULL DEFAULT 0 CHECK (failed_login_count >= 0),
            ADD COLUMN locked_until       TIMESTAMPTZ,
            ADD COLUMN last_login_at      TIMESTAMPTZ
    """)
    op.execute("COMMENT ON COLUMN users.password_hash IS '비밀번호 해시 scrypt$N$r$p$소금$해시. 원문 저장 안 함. NULL 이면 비밀번호 로그인 불가'")
    op.execute("COMMENT ON COLUMN users.is_active IS 'FALSE 면 로그인·기존 세션 모두 거부 (퇴사·정지)'")
    op.execute("COMMENT ON COLUMN users.failed_login_count IS '연속 로그인 실패 횟수 (성공하면 0)'")
    op.execute("COMMENT ON COLUMN users.locked_until IS '연속 실패로 이 시각까지 로그인 거부'")

    op.execute("""
        CREATE TABLE login_session (
            token_hash    CHAR(64) PRIMARY KEY,
            user_id       BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ NOT NULL,
            last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked_at    TIMESTAMPTZ,
            client_ip     VARCHAR(64),
            user_agent    VARCHAR(300),
            CHECK (expires_at > created_at)
        )
    """)
    op.execute("CREATE INDEX ix_login_session_user ON login_session (user_id)")
    op.execute("COMMENT ON TABLE login_session IS '로그인 세션. 쿠키 토큰의 SHA-256 만 저장. 로그아웃·강제 종료는 revoked_at'")
    # 앱 전용 역할(0002)이 있으면 새 표도 읽고 쓸 수 있게 (기본 권한 설정이 있어도 명시)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON login_session TO agent_app;
            END IF;
        END $$
    """)

    _event_check(EVENTS_NEW)


def downgrade() -> None:
    # audit_log 는 지울 수 없는 표라, 로그인 기록이 이미 있으면 예전 제약으로 되돌릴 수 없다 (그때는 실패하는 게 맞다)
    _event_check(EVENTS_OLD)
    op.execute("DROP TABLE login_session")
    op.execute("""
        ALTER TABLE users
            DROP COLUMN last_login_at,
            DROP COLUMN locked_until,
            DROP COLUMN failed_login_count,
            DROP COLUMN is_active,
            DROP COLUMN password_hash
    """)
