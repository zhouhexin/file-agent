"""增加文件 ingest 状态和基础洞察表。

Revision ID: 20260625_0006
Revises: 20260625_0005
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0006"
down_revision = "20260625_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为 deterministic ingest 增加状态和可复用处理结果。"""

    op.add_column("documents", sa.Column("ingest_status", sa.String(length=40), nullable=False, server_default="UPLOADED"))
    op.create_table(
        "document_insights",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("keywords_json", sa.JSON(), nullable=False),
        sa.Column("labels_json", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id"),
    )
    op.create_index("ix_document_insights_document_id", "document_insights", ["document_id"])


def downgrade() -> None:
    """回滚文件 ingest 状态和基础洞察表。"""

    op.drop_index("ix_document_insights_document_id", table_name="document_insights")
    op.drop_table("document_insights")
    op.drop_column("documents", "ingest_status")
