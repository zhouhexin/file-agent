"""新增 Adaptive Planner Catalog、能力建议和 Shadow 对比审计。

Revision ID: 20260730_0001
Revises: 20260728_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260730_0001"
down_revision = "20260728_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 Planner 新契约所需的审计字段和候选表。"""

    op.add_column(
        "agent_runs",
        sa.Column("planner_mode", sa.String(length=40), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "planner_schema_version",
            sa.String(length=80),
            nullable=False,
            server_default="planner-decision-v1",
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column("catalog_version", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "catalog_fingerprint",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    op.create_index("ix_agent_runs_planner_mode", "agent_runs", ["planner_mode"])
    op.create_index(
        "ix_agent_runs_catalog_fingerprint",
        "agent_runs",
        ["catalog_fingerprint"],
    )

    op.create_table(
        "capability_suggestions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("suggestion_kind", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("missing_capability", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "expected_inputs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "expected_outputs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "related_skill_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("deduplication_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("latest_agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("requested_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("catalog_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="NEW"),
        sa.Column("review_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["first_agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["latest_agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deduplication_fingerprint",
            name="uq_capability_suggestions_fingerprint",
        ),
    )
    op.create_index(
        "ix_capability_suggestions_kind",
        "capability_suggestions",
        ["suggestion_kind"],
    )
    op.create_index(
        "ix_capability_suggestions_status",
        "capability_suggestions",
        ["status"],
    )
    op.create_index(
        "ix_capability_suggestions_requested_by_user_id",
        "capability_suggestions",
        ["requested_by_user_id"],
    )
    op.create_index(
        "ix_capability_suggestions_first_agent_run_id",
        "capability_suggestions",
        ["first_agent_run_id"],
    )
    op.create_index(
        "ix_capability_suggestions_latest_agent_run_id",
        "capability_suggestions",
        ["latest_agent_run_id"],
    )
    op.create_index(
        "ix_capability_suggestions_reviewed_by",
        "capability_suggestions",
        ["reviewed_by"],
    )

    op.create_table(
        "planner_shadow_comparisons",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=False),
        sa.Column("legacy_decision_type", sa.String(length=40), nullable=False),
        sa.Column("adaptive_decision_type", sa.String(length=40), nullable=False),
        sa.Column("legacy_intent", sa.String(length=120), nullable=False),
        sa.Column("adaptive_intent", sa.String(length=120), nullable=False),
        sa.Column(
            "legacy_skill_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "adaptive_skill_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "legacy_tool_names_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "adaptive_tool_names_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("scope_match", sa.Boolean(), nullable=False),
        sa.Column("risk_match", sa.Boolean(), nullable=False),
        sa.Column("confirmation_match", sa.Boolean(), nullable=False),
        sa.Column("adaptive_validation_status", sa.String(length=40), nullable=False),
        sa.Column("adaptive_error_code", sa.String(length=120), nullable=True),
        sa.Column("catalog_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_planner_shadow_comparisons_agent_run_id",
        "planner_shadow_comparisons",
        ["agent_run_id"],
    )


def downgrade() -> None:
    """移除 Adaptive Planner 新增表和 AgentRun 审计列。"""

    op.drop_index(
        "ix_planner_shadow_comparisons_agent_run_id",
        table_name="planner_shadow_comparisons",
    )
    op.drop_table("planner_shadow_comparisons")
    op.drop_index(
        "ix_capability_suggestions_reviewed_by",
        table_name="capability_suggestions",
    )
    op.drop_index(
        "ix_capability_suggestions_latest_agent_run_id",
        table_name="capability_suggestions",
    )
    op.drop_index(
        "ix_capability_suggestions_first_agent_run_id",
        table_name="capability_suggestions",
    )
    op.drop_index(
        "ix_capability_suggestions_requested_by_user_id",
        table_name="capability_suggestions",
    )
    op.drop_index("ix_capability_suggestions_status", table_name="capability_suggestions")
    op.drop_index("ix_capability_suggestions_kind", table_name="capability_suggestions")
    op.drop_table("capability_suggestions")
    op.drop_index("ix_agent_runs_catalog_fingerprint", table_name="agent_runs")
    op.drop_index("ix_agent_runs_planner_mode", table_name="agent_runs")
    op.drop_column("agent_runs", "catalog_fingerprint")
    op.drop_column("agent_runs", "catalog_version")
    op.drop_column("agent_runs", "planner_schema_version")
    op.drop_column("agent_runs", "planner_mode")
