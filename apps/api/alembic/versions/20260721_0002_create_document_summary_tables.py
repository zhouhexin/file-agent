"""创建普通文档摘要和分类主题摘要持久化表。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260721_0002"
down_revision = "20260721_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建按文档版本和解析运行隔离的双摘要记录。"""

    op.create_table(
        "document_summaries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("coverage_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_provider", sa.String(length=80), nullable=False),
        sa.Column("model_name", sa.String(length=160), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "extraction_run_id",
            "input_sha256",
            "model_provider",
            "model_name",
            "prompt_version",
            "schema_version",
            name="uq_document_summaries_cache_key",
        ),
    )
    op.create_table(
        "document_classification_summaries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_provider", sa.String(length=80), nullable=False),
        sa.Column("model_name", sa.String(length=160), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "extraction_run_id",
            "input_sha256",
            "model_provider",
            "model_name",
            "prompt_version",
            "schema_version",
            name="uq_document_classification_summaries_cache_key",
        ),
    )
    for table_name in ("document_summaries", "document_classification_summaries"):
        for column in (
            "document_id",
            "document_version_id",
            "extraction_run_id",
            "input_sha256",
            "status",
        ):
            op.create_index(f"ix_{table_name}_{column}", table_name, [column])
    op.add_column(
        "document_classification_runs",
        sa.Column("classification_summary_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "document_classification_runs",
        sa.Column("classification_basis", sa.String(length=40), server_default="FULL_TEXT", nullable=False),
    )
    op.add_column(
        "document_classification_runs",
        sa.Column("summary_status", sa.String(length=40), server_default="DISABLED", nullable=False),
    )
    op.create_foreign_key(
        "fk_document_classification_runs_summary",
        "document_classification_runs",
        "document_classification_summaries",
        ["classification_summary_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for column in ("classification_summary_id", "classification_basis", "summary_status"):
        op.create_index(
            f"ix_document_classification_runs_{column}",
            "document_classification_runs",
            [column],
        )


def downgrade() -> None:
    """移除双摘要记录，原文页面和工作副本不受影响。"""

    for column in ("summary_status", "classification_basis", "classification_summary_id"):
        op.drop_index(f"ix_document_classification_runs_{column}", table_name="document_classification_runs")
    op.drop_constraint(
        "fk_document_classification_runs_summary",
        "document_classification_runs",
        type_="foreignkey",
    )
    op.drop_column("document_classification_runs", "summary_status")
    op.drop_column("document_classification_runs", "classification_basis")
    op.drop_column("document_classification_runs", "classification_summary_id")
    op.drop_table("document_classification_summaries")
    op.drop_table("document_summaries")
