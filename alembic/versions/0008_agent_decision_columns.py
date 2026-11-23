"""에이전트·공유 요청 저장에 필요한 칸 (2026-11-23)

앱(apps/api/src/services/agents.py)이 메모리에서 쓰던 값 중 팀 ERD(0001)에 없던 칸을 더한다.
  agent.disabled_by                   누가 비활성화했는지. 주인이 직접 끈 것만 주인이 다시 켤 수 있다 (부서장·관리자가 끈 것은 못 켬)
  agent_share_request.decided_by      누가 승인·반려했는지
  agent_share_request.decision_comment 승인·반려 의견
  chat_session.agent_removed          대화에 쓰던 에이전트가 삭제됨. agent_id 는 삭제 때 NULL 이 되는데(ON DELETE SET NULL),
                                      NULL 은 '기본 에이전트(모든 툴)' 와 구분이 안 돼 권한이 넓어질 수 있으므로 따로 표시한다

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-23
"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent ADD COLUMN disabled_by BIGINT REFERENCES users (id)")
    op.execute("COMMENT ON COLUMN agent.disabled_by IS '비활성화한 사람. 주인이 직접 끈 것만 주인이 다시 켤 수 있다'")
    op.execute("""
        ALTER TABLE agent_share_request
            ADD COLUMN decided_by BIGINT REFERENCES users (id),
            ADD COLUMN decision_comment TEXT
    """)
    op.execute("COMMENT ON COLUMN agent_share_request.decided_by IS '승인·반려한 사람'")
    op.execute("COMMENT ON COLUMN agent_share_request.decision_comment IS '승인·반려 의견'")
    op.execute("ALTER TABLE chat_session ADD COLUMN agent_removed BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("COMMENT ON COLUMN chat_session.agent_removed IS '쓰던 에이전트가 삭제됨 (이 대화는 더 이어갈 수 없음)'")


def downgrade() -> None:
    op.execute("ALTER TABLE chat_session DROP COLUMN agent_removed")
    op.execute("ALTER TABLE agent_share_request DROP COLUMN decision_comment, DROP COLUMN decided_by")
    op.execute("ALTER TABLE agent DROP COLUMN disabled_by")
