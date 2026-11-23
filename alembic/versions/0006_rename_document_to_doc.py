"""document → doc 이름 바꾸기 (2026-11-23)

대상: 테이블 document → doc, document_version → doc_version
      이름에 document 가 들어간 제약·인덱스·시퀀스 (document_pkey, uq_document_version_current,
      document_version_doc_id_version_key, ck_document_security_grade, document_id_seq 등) → document 를 doc 으로
      audit_log.target_type 허용값에 'doc' 추가. audit_log 는 추가만 되는 표라 예전 'document' 행은 고칠 수 없어서
      'document' 도 허용값에 남겨 둔다 (새 기록은 'doc').
값·외래키는 그대로 따라간다.

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

TARGET_TYPES_OLD = "'document', 'work_file', 'output_file', 'agent'"
TARGET_TYPES_NEW = "'doc', 'document', 'work_file', 'output_file', 'agent'"


def _rename_objects(old: str, new: str, tables: tuple[str, ...]) -> None:
    """tables 에 딸린 제약·인덱스·시퀀스 이름 속 old 를 new 로 바꾼다."""
    table_list = ", ".join(f"'{t}'" for t in tables)
    op.execute(f"""
DO $$
DECLARE r record;
BEGIN
    -- 제약 (PK·UNIQUE 는 딸린 인덱스 이름도 같이 바뀐다)
    FOR r IN SELECT c.conname, t.relname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname IN ({table_list}) AND c.conname LIKE '%{old}%' LOOP
        EXECUTE format('ALTER TABLE %I RENAME CONSTRAINT %I TO %I', r.relname, r.conname,
                       replace(r.conname, '{old}', '{new}'));
    END LOOP;
    -- 제약에 안 딸린 인덱스
    FOR r IN SELECT i.relname FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
             JOIN pg_class t ON t.oid = x.indrelid
             WHERE t.relname IN ({table_list}) AND i.relname LIKE '%{old}%' LOOP
        EXECUTE format('ALTER INDEX %I RENAME TO %I', r.relname, replace(r.relname, '{old}', '{new}'));
    END LOOP;
    -- 식별(IDENTITY) 시퀀스
    FOR r IN SELECT s.relname FROM pg_class s JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')
             JOIN pg_class t ON t.oid = d.refobjid
             WHERE s.relkind = 'S' AND t.relname IN ({table_list}) AND s.relname LIKE '%{old}%' LOOP
        EXECUTE format('ALTER SEQUENCE %I RENAME TO %I', r.relname, replace(r.relname, '{old}', '{new}'));
    END LOOP;
END
$$""")


def upgrade() -> None:
    op.execute("ALTER TABLE document RENAME TO doc")
    op.execute("ALTER TABLE document_version RENAME TO doc_version")
    _rename_objects("document", "doc", ("doc", "doc_version"))
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT audit_log_target_type_check")
    op.execute(f"ALTER TABLE audit_log ADD CONSTRAINT audit_log_target_type_check "
               f"CHECK (target_type IN ({TARGET_TYPES_NEW}))")


def downgrade() -> None:
    # 'doc' 으로 남은 감사 기록이 있으면 예전 제약을 다시 걸 수 없으므로 실패한다 (기록은 고칠 수 없음)
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT audit_log_target_type_check")
    op.execute(f"ALTER TABLE audit_log ADD CONSTRAINT audit_log_target_type_check "
               f"CHECK (target_type IN ({TARGET_TYPES_OLD}))")
    _rename_objects("doc", "document", ("doc", "doc_version"))
    # 0003 에서 doc_id 로 맞춰 둔 UNIQUE 제약은 위에서 document_id 까지 되돌아가므로 다시 맞춘다
    op.execute("ALTER TABLE doc_version RENAME CONSTRAINT document_version_document_id_version_key "
               "TO document_version_doc_id_version_key")
    op.execute("ALTER TABLE doc_version RENAME TO document_version")
    op.execute("ALTER TABLE doc RENAME TO document")
