"""确保 documents.original_filename 在数据库层不可变。

Revision ID: 20260901_0001
Revises: 20260831_0001
"""

from alembic import op


revision = "20260901_0001"
down_revision = "20260831_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """拒绝任何把原始文件名改写为工作副本名称的数据库更新。"""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION preserve_documents_original_filename()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.original_filename IS DISTINCT FROM OLD.original_filename THEN
                RAISE EXCEPTION 'documents.original_filename is immutable after creation'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_documents_original_filename_immutable
        BEFORE UPDATE OF original_filename ON documents
        FOR EACH ROW
        EXECUTE FUNCTION preserve_documents_original_filename();
        """
    )


def downgrade() -> None:
    """移除数据库触发器；业务代码仍应把原始文件名视为不可变字段。"""

    op.execute(
        "DROP TRIGGER IF EXISTS trg_documents_original_filename_immutable ON documents"
    )
    op.execute("DROP FUNCTION IF EXISTS preserve_documents_original_filename()")
