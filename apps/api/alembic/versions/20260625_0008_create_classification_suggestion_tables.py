"""创建分类运行、分类建议和反馈表。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0008"
down_revision = "20260625_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建分类建议持久化相关表。"""

    op.create_table(
        "document_classification_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=False),
        sa.Column("taxonomy_key", sa.String(length=120), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=80), nullable=False),
        sa.Column("classifier_version", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_classification_runs_agent_run_id", "document_classification_runs", ["agent_run_id"])
    op.create_index("ix_document_classification_runs_document_id", "document_classification_runs", ["document_id"])

    op.create_table(
        "document_category_suggestions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("classification_run_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("category_name", sa.String(length=255), nullable=False),
        sa.Column("category_path_json", sa.JSON(), nullable=False),
        sa.Column("taxonomy_key", sa.String(length=120), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=80), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["classification_run_id"], ["document_classification_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_document_category_suggestions_classification_run_id",
        "document_category_suggestions",
        ["classification_run_id"],
    )
    op.create_index("ix_document_category_suggestions_document_id", "document_category_suggestions", ["document_id"])

    op.create_table(
        "document_category_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("suggestion_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["suggestion_id"], ["document_category_suggestions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_category_feedback_document_id", "document_category_feedback", ["document_id"])
    op.create_index("ix_document_category_feedback_suggestion_id", "document_category_feedback", ["suggestion_id"])
    op.create_index("ix_document_category_feedback_user_id", "document_category_feedback", ["user_id"])


def downgrade() -> None:
    """删除分类建议持久化相关表。"""

    op.drop_index("ix_document_category_feedback_user_id", table_name="document_category_feedback")
    op.drop_index("ix_document_category_feedback_suggestion_id", table_name="document_category_feedback")
    op.drop_index("ix_document_category_feedback_document_id", table_name="document_category_feedback")
    op.drop_table("document_category_feedback")
    op.drop_index("ix_document_category_suggestions_document_id", table_name="document_category_suggestions")
    op.drop_index(
        "ix_document_category_suggestions_classification_run_id",
        table_name="document_category_suggestions",
    )
    op.drop_table("document_category_suggestions")
    op.drop_index("ix_document_classification_runs_document_id", table_name="document_classification_runs")
    op.drop_index("ix_document_classification_runs_agent_run_id", table_name="document_classification_runs")
    op.drop_table("document_classification_runs")
