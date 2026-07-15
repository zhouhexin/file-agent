"""增加图谱分类第二版运行审计和反馈样本字段。"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_0001"
down_revision = "20260714_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建投影运行表，并扩展分类建议与追加式反馈。"""

    op.create_table(
        "graph_projection_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("projection_type", sa.String(length=40), nullable=False),
        sa.Column("scope_type", sa.String(length=40), nullable=False, server_default="ALL"),
        sa.Column("scope_id", sa.String(length=255), nullable=True),
        sa.Column("projection_version", sa.String(length=80), nullable=False, server_default="graph-v2"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="RUNNING"),
        sa.Column("nodes_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("relationships_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_succeeded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_graph_projection_runs_projection_type", "graph_projection_runs", ["projection_type"])
    op.create_index("ix_graph_projection_runs_status", "graph_projection_runs", ["status"])

    op.add_column(
        "document_category_suggestions",
        sa.Column("document_version_id", sa.String(length=36), nullable=False, server_default=""),
    )
    op.add_column(
        "document_category_suggestions",
        sa.Column("category_id", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "document_category_suggestions",
        sa.Column("candidate_scores_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "document_category_suggestions",
        sa.Column("semantic_evidence_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index(
        "ix_document_category_suggestions_document_version_id",
        "document_category_suggestions",
        ["document_version_id"],
    )
    op.create_index(
        "ix_document_category_suggestions_category_id",
        "document_category_suggestions",
        ["category_id"],
    )

    op.add_column(
        "document_category_feedback",
        sa.Column("corrected_category_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "document_category_feedback",
        sa.Column("corrected_category_path_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "document_category_feedback",
        sa.Column("supersedes_feedback_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "document_category_feedback",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_foreign_key(
        "fk_document_category_feedback_supersedes",
        "document_category_feedback",
        "document_category_feedback",
        ["supersedes_feedback_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_document_category_feedback_supersedes_feedback_id",
        "document_category_feedback",
        ["supersedes_feedback_id"],
    )
    op.create_index(
        "ix_document_category_feedback_is_active",
        "document_category_feedback",
        ["is_active"],
    )


def downgrade() -> None:
    """移除图谱分类第二版新增结构。"""

    op.drop_index("ix_document_category_feedback_is_active", table_name="document_category_feedback")
    op.drop_index(
        "ix_document_category_feedback_supersedes_feedback_id",
        table_name="document_category_feedback",
    )
    op.drop_constraint(
        "fk_document_category_feedback_supersedes",
        "document_category_feedback",
        type_="foreignkey",
    )
    op.drop_column("document_category_feedback", "is_active")
    op.drop_column("document_category_feedback", "supersedes_feedback_id")
    op.drop_column("document_category_feedback", "corrected_category_path_json")
    op.drop_column("document_category_feedback", "corrected_category_id")

    op.drop_index("ix_document_category_suggestions_category_id", table_name="document_category_suggestions")
    op.drop_index(
        "ix_document_category_suggestions_document_version_id",
        table_name="document_category_suggestions",
    )
    op.drop_column("document_category_suggestions", "semantic_evidence_json")
    op.drop_column("document_category_suggestions", "candidate_scores_json")
    op.drop_column("document_category_suggestions", "category_id")
    op.drop_column("document_category_suggestions", "document_version_id")

    op.drop_index("ix_graph_projection_runs_status", table_name="graph_projection_runs")
    op.drop_index("ix_graph_projection_runs_projection_type", table_name="graph_projection_runs")
    op.drop_table("graph_projection_runs")
