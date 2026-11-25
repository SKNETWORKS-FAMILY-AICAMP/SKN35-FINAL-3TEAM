"""도구 실행 감사 기록을 DB(audit_log)에 남기기 위한 조정 (2026-11-23)

지금까지 logs/permission_audit.jsonl 에만 쓰던 도구 실행 권한 판정(허용·거부·승인·거절·시간 초과)을
audit_log(event_type='tool_call')에 쓴다. 팀 ERD(0001) 의 audit_log 를 그대로 쓰되 두 외래키만 뺀다.
  - call_id → tool_call
      권한 판정은 툴 실행 도중에 기록되는데 tool_call 은 요청이 끝날 때 저장된다 (순서가 반대).
      또 감사 기록은 대화를 지워도 남아야 한다 (tool_call 은 대화와 함께 지워진다).
  - rule_id → policy_rule
      정책 규칙은 config/permissions.yaml 에 있고 policy_rule 표는 비어 있다.
  두 칸 모두 값은 그대로 남기고 찾기용 색인을 단다.
  run_id 추가: 어느 요청(agent_run)에서 나온 판정인지. 같은 이유로 외래키는 두지 않는다.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-23
"""
from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_call_id_fkey")
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_rule_id_fkey")
    op.execute("ALTER TABLE audit_log ADD COLUMN run_id UUID")
    op.execute("CREATE INDEX idx_audit_log_call ON audit_log (call_id)")
    op.execute("CREATE INDEX idx_audit_log_session ON audit_log (session_id)")
    op.execute("COMMENT ON COLUMN audit_log.run_id IS '판정이 나온 요청 (agent_run.id). 대화를 지워도 남도록 외래키 없음'")
    op.execute("COMMENT ON COLUMN audit_log.call_id IS '툴 호출 ID (tool_call.call_id 와 같은 값). 대화를 지워도 남도록 외래키 없음'")
    op.execute("COMMENT ON COLUMN audit_log.rule_id IS '맞은 정책 규칙 ID (config/permissions.yaml 의 id)'")


def downgrade() -> None:
    op.execute("DROP INDEX idx_audit_log_session")
    op.execute("DROP INDEX idx_audit_log_call")
    op.execute("ALTER TABLE audit_log DROP COLUMN run_id")
    # 되돌릴 때 이미 쌓인 기록은 짝이 안 맞을 수 있으므로 새 행부터만 검사한다 (NOT VALID)
    op.execute("ALTER TABLE audit_log ADD CONSTRAINT audit_log_rule_id_fkey "
               "FOREIGN KEY (rule_id) REFERENCES policy_rule (policy_rule_id) NOT VALID")
    op.execute("ALTER TABLE audit_log ADD CONSTRAINT audit_log_call_id_fkey "
               "FOREIGN KEY (call_id) REFERENCES tool_call (call_id) NOT VALID")
