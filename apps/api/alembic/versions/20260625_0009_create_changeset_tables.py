"""创建 ChangeSet 和 ChangeItem 表。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0009"
down_revision = "20260625_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建真实 ChangeSet 审计表，并关联 AgentRun。"""

    op.add_column("agent_runs", sa.Column("changeset_id", sa.String(length=36), nullable=True))
    op.create_index("ix_agent_runs_changeset_id", "agent_runs", ["changeset_id"])
    op.create_table(
        "change_sets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_change_sets_agent_run_id", "change_sets", ["agent_run_id"])
    op.create_index("ix_change_sets_conversation_id", "change_sets", ["conversation_id"])
    op.create_index("ix_change_sets_user_id", "change_sets", ["user_id"])
    op.create_index("ix_change_sets_workspace_id", "change_sets", ["workspace_id"])
    op.create_foreign_key(
        "agent_runs_changeset_id_fkey",
        "agent_runs",
        "change_sets",
        ["changeset_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "change_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("changeset_id", sa.String(length=36), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=True),
        sa.Column("target_document_id", sa.String(length=36), nullable=True),
        sa.Column("change_type", sa.String(length=80), nullable=False),
        sa.Column("before_value_json", sa.JSON(), nullable=False),
        sa.Column("after_value_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("execution_status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["changeset_id"], ["change_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_change_items_changeset_id", "change_items", ["changeset_id"])
    op.create_index("ix_change_items_change_type", "change_items", ["change_type"])
    op.create_index("ix_change_items_target_document_id", "change_items", ["target_document_id"])


def downgrade() -> None:
    """删除真实 ChangeSet 审计表。"""

    op.drop_index("ix_change_items_target_document_id", table_name="change_items")
    op.drop_index("ix_change_items_change_type", table_name="change_items")
    op.drop_index("ix_change_items_changeset_id", table_name="change_items")
    op.drop_table("change_items")
    op.drop_constraint("agent_runs_changeset_id_fkey", "agent_runs", type_="foreignkey")
    op.drop_index("ix_change_sets_workspace_id", table_name="change_sets")
    op.drop_index("ix_change_sets_user_id", table_name="change_sets")
    op.drop_index("ix_change_sets_conversation_id", table_name="change_sets")
    op.drop_index("ix_change_sets_agent_run_id", table_name="change_sets")
    op.drop_table("change_sets")
    op.drop_index("ix_agent_runs_changeset_id", table_name="agent_runs")
    op.drop_column("agent_runs", "changeset_id")
