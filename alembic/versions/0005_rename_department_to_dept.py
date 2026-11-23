"""department 테이블 → dept 이름 바꾸기 (2026-11-23)

대상: 테이블 department → dept, 제약 fk_department_head → fk_dept_head,
      자동 이름 department_pkey → dept_pkey, 식별 시퀀스 department_id_seq → dept_id_seq
값·외래키(users.dept_id, agent_assignment.dept_id, document.owner_dept_id)는 그대로 따라간다.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-23
"""
from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE department RENAME TO dept")
    op.execute("ALTER TABLE dept RENAME CONSTRAINT fk_department_head TO fk_dept_head")
    op.execute("ALTER TABLE dept RENAME CONSTRAINT department_pkey TO dept_pkey")
    op.execute("ALTER SEQUENCE department_id_seq RENAME TO dept_id_seq")


def downgrade() -> None:
    op.execute("ALTER SEQUENCE dept_id_seq RENAME TO department_id_seq")
    op.execute("ALTER TABLE dept RENAME CONSTRAINT dept_pkey TO department_pkey")
    op.execute("ALTER TABLE dept RENAME CONSTRAINT fk_dept_head TO fk_department_head")
    op.execute("ALTER TABLE dept RENAME TO department")
