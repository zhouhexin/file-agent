"""增加文件解析运行和页面表。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0007"
down_revision = "20260625_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 document_extraction_runs 和 document_pages。"""

    op.create_table(
        "document_extraction_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("extractor", sa.String(length=80), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_extraction_runs_document_id",
        "document_extraction_runs",
        ["document_id"],
    )

    op.create_table(
        "document_pages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("sheet_name", sa.String(length=255), nullable=True),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_pages_document_id", "document_pages", ["document_id"])
    op.create_index("ix_document_pages_extraction_run_id", "document_pages", ["extraction_run_id"])


def downgrade() -> None:
    """删除文件解析相关表。"""

    op.drop_index("ix_document_pages_extraction_run_id", table_name="document_pages")
    op.drop_index("ix_document_pages_document_id", table_name="document_pages")
    op.drop_table("document_pages")
    op.drop_index("ix_document_extraction_runs_document_id", table_name="document_extraction_runs")
    op.drop_table("document_extraction_runs")
