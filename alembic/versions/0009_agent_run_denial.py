"""요청이 권한 때문에 멈췄을 때 무엇이 왜 막혔는지 (2026-11-23)

  agent_run.denial   stop_reason = 'denied' 일 때 {call_id, tool_name, display_name, kind, reason}.
                     지난 대화를 다시 열 때 화면의 '거부 안내' 상자를 그대로 다시 그리려고 남긴다.
                     (거부 이유 문장은 답 메시지에도 들어 있지만, 어떤 툴이 어떤 이유(deny·rejected·timeout)로
                     막혔는지는 여기에만 있다)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-23
"""
from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE agent_run ADD COLUMN denial JSONB
            CHECK (denial IS NULL OR (jsonb_typeof(denial) = 'object' AND stop_reason = 'denied'))
    """)
    op.execute("COMMENT ON COLUMN agent_run.denial IS "
               "'권한 거부로 멈춘 요청의 거부 내용 {call_id, tool_name, display_name, kind, reason}'")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_run DROP COLUMN denial")
