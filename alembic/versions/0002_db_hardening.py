"""팀 ERD 위에 DB 담당이 더한 것 (2026-11-20 결정)

1) users.auth_provider · external_id — 사내 통합 로그인(SSO) 계정 연결용 빈 칸
2) security_grade 1~5 제한 — users · document · chunk
3) 감사 로그 보호 — audit_log 는 추가만. 수정·삭제·TRUNCATE 를 트리거가 거부
4) 앱 전용 역할 agent_app — 일반 표는 읽기·쓰기, audit_log 는 읽기·추가만
   (로그인 계정·비밀번호는 여기서 만들지 않는다: alembic/README.md 참고)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

GRADE_TABLES = ("users", "document", "chunk")


def upgrade() -> None:
    # 1) SSO 연결 칸 — 같은 로그인 방식 안에서 외부 계정 ID 는 한 사람에게만
    op.execute("ALTER TABLE users ADD COLUMN auth_provider VARCHAR(30)")
    op.execute("ALTER TABLE users ADD COLUMN external_id VARCHAR(255)")
    op.execute("CREATE UNIQUE INDEX uq_users_external ON users (auth_provider, external_id) "
               "WHERE external_id IS NOT NULL")
    op.execute("COMMENT ON COLUMN users.auth_provider IS '로그인 방식 (예: ldap, saml, oidc). 정해지기 전엔 NULL'")
    op.execute("COMMENT ON COLUMN users.external_id IS 'SSO 쪽 계정 ID'")

    # 2) 보안등급 범위 (개발용 사용자 기준 1~5, 클수록 높음)
    for t in GRADE_TABLES:
        op.execute(f"ALTER TABLE {t} ADD CONSTRAINT ck_{t}_security_grade CHECK (security_grade BETWEEN 1 AND 5)")

    # 3) 감사 로그 보호: 행 수정·삭제와 TRUNCATE 를 모두 거부 (슈퍼유저가 트리거를 끄는 것까지는 못 막음)
    op.execute("""
CREATE FUNCTION audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log 는 추가만 할 수 있습니다 (% 거부)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$""")
    op.execute("CREATE TRIGGER trg_audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
               "FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()")
    op.execute("CREATE TRIGGER trg_audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
               "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()")

    # 4) 앱 전용 역할 (로그인 불가 묶음 역할 — 실제 로그인 계정은 이 역할에 넣어서 만든다)
    op.execute("""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app') THEN
        CREATE ROLE agent_app NOLOGIN;
    END IF;
END
$$""")
    op.execute("GRANT USAGE ON SCHEMA public TO agent_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO agent_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agent_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM agent_app")
    op.execute("REVOKE ALL ON alembic_version FROM agent_app")   # 마이그레이션 기록은 앱이 건드리지 않음
    # 앞으로 마이그레이션이 만드는 표에도 같은 권한 (audit 류 표는 그 마이그레이션에서 따로 회수)
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agent_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO agent_app")


def downgrade() -> None:
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM agent_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM agent_app")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM agent_app")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM agent_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM agent_app")
    # 역할 자체는 남긴다 (이 역할에 속한 로그인 계정이 있을 수 있음)
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
    for t in GRADE_TABLES:
        op.execute(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS ck_{t}_security_grade")
    op.execute("DROP INDEX IF EXISTS uq_users_external")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS external_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS auth_provider")
