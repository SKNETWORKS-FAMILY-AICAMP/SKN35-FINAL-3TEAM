"""document_id → doc_id, document_version_id → doc_version_id 이름 바꾸기 (2026-11-20)

대상: document_version.document_id, agent_knowledge.document_id → doc_id
      chunk.document_version_id, output_source.document_version_id → doc_version_id
칸 이름만 바뀌고 값·외래키·인덱스는 그대로 따라간다. 이름에 document_id 가 들어간 제약 하나도 같이 바꾼다.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-20
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE document_version RENAME COLUMN document_id TO doc_id")
    op.execute("ALTER TABLE agent_knowledge RENAME COLUMN document_id TO doc_id")
    op.execute("ALTER TABLE chunk RENAME COLUMN document_version_id TO doc_version_id")
    op.execute("ALTER TABLE output_source RENAME COLUMN document_version_id TO doc_version_id")
    # UNIQUE (document_version_id, chunk_index) 제약 이름도 맞춘다
    op.execute("ALTER TABLE chunk RENAME CONSTRAINT chunk_document_version_id_chunk_index_key "
               "TO chunk_doc_version_id_chunk_index_key")
    # UNIQUE (document_id, version) 제약의 자동 이름도 새 칸 이름에 맞춘다
    op.execute("ALTER TABLE document_version RENAME CONSTRAINT document_version_document_id_version_key "
               "TO document_version_doc_id_version_key")


def downgrade() -> None:
    op.execute("ALTER TABLE document_version RENAME CONSTRAINT document_version_doc_id_version_key "
               "TO document_version_document_id_version_key")
    op.execute("ALTER TABLE chunk RENAME CONSTRAINT chunk_doc_version_id_chunk_index_key "
               "TO chunk_document_version_id_chunk_index_key")
    op.execute("ALTER TABLE output_source RENAME COLUMN doc_version_id TO document_version_id")
    op.execute("ALTER TABLE chunk RENAME COLUMN doc_version_id TO document_version_id")
    op.execute("ALTER TABLE agent_knowledge RENAME COLUMN doc_id TO document_id")
    op.execute("ALTER TABLE document_version RENAME COLUMN doc_id TO document_id")
