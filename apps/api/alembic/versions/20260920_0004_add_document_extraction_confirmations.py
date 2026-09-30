"""增加 WorkBuddy 显式内容提取的保存前确认记录。

Revision ID: 20260920_0004
Revises: 20260920_0003
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260920_0004"
down_revision = "20260920_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建只保存结果指纹和确认状态的审计表，正文仍只在确认后入库。"""

    op.create_table(
        "document_extraction_confirmations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("source_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("preview_key", sa.String(length=200), nullable=False),
        sa.Column("result_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "summary_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_version_id"], ["document_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["external_extraction_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "preview_key", name="uq_document_extraction_confirmation_preview_key"),
        sa.UniqueConstraint("task_id", "revision", name="uq_document_extraction_confirmation_revision"),
    )
    op.create_index(
        "ix_document_extraction_confirmations_result_digest",
        "document_extraction_confirmations",
        ["result_digest"],
        unique=False,
    )
    op.create_index(
        "ix_document_extraction_confirmations_source_version_id",
        "document_extraction_confirmations",
        ["source_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_extraction_confirmations_status",
        "document_extraction_confirmations",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_document_extraction_confirmations_task_id",
        "document_extraction_confirmations",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_extraction_confirmations_user_id",
        "document_extraction_confirmations",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """删除确认记录表，不改动已经确认后保存的文档事实。"""

    op.drop_index("ix_document_extraction_confirmations_user_id", table_name="document_extraction_confirmations")
    op.drop_index("ix_document_extraction_confirmations_task_id", table_name="document_extraction_confirmations")
    op.drop_index("ix_document_extraction_confirmations_status", table_name="document_extraction_confirmations")
    op.drop_index("ix_document_extraction_confirmations_source_version_id", table_name="document_extraction_confirmations")
    op.drop_index("ix_document_extraction_confirmations_result_digest", table_name="document_extraction_confirmations")
    op.drop_table("document_extraction_confirmations")
