"""增加用户邮箱和文件上传持久化表。

Revision ID: 20260625_0003
Revises: 20260625_0002
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0003"
down_revision = "20260625_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加 users.email，并创建 documents、file_objects。"""

    op.add_column("users", sa.Column("email", sa.String(length=255), nullable=True))
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_user_id", "documents", ["user_id"])
    op.create_index("ix_documents_workspace_id", "documents", ["workspace_id"])
    op.create_index("ix_documents_sha256", "documents", ["sha256"])

    op.create_table(
        "file_objects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("storage_backend", sa.String(length=40), nullable=False),
        sa.Column("storage_path", sa.String(length=500), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_file_objects_document_id", "file_objects", ["document_id"])


def downgrade() -> None:
    """回滚文件上传表和 users.email。"""

    op.drop_index("ix_file_objects_document_id", table_name="file_objects")
    op.drop_table("file_objects")
    op.drop_index("ix_documents_sha256", table_name="documents")
    op.drop_index("ix_documents_workspace_id", table_name="documents")
    op.drop_index("ix_documents_user_id", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "email")
