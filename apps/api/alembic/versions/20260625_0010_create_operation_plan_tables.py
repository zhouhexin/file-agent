"""创建 OperationPlan 和确认表。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0010"
down_revision = "20260625_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建高风险操作确认闭环所需表。"""

    op.create_table(
        "operation_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("operation_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("risk_level", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_operation_plans_agent_run_id", "operation_plans", ["agent_run_id"])
    op.create_index("ix_operation_plans_conversation_id", "operation_plans", ["conversation_id"])
    op.create_index("ix_operation_plans_user_id", "operation_plans", ["user_id"])
    op.create_index("ix_operation_plans_workspace_id", "operation_plans", ["workspace_id"])

    op.create_table(
        "operation_confirmations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operation_plan_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("confirmation_text", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["operation_plan_id"], ["operation_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_operation_confirmations_operation_plan_id", "operation_confirmations", ["operation_plan_id"])
    op.create_index("ix_operation_confirmations_user_id", "operation_confirmations", ["user_id"])


def downgrade() -> None:
    """删除高风险操作确认闭环表。"""

    op.drop_index("ix_operation_confirmations_user_id", table_name="operation_confirmations")
    op.drop_index("ix_operation_confirmations_operation_plan_id", table_name="operation_confirmations")
    op.drop_table("operation_confirmations")
    op.drop_index("ix_operation_plans_workspace_id", table_name="operation_plans")
    op.drop_index("ix_operation_plans_user_id", table_name="operation_plans")
    op.drop_index("ix_operation_plans_conversation_id", table_name="operation_plans")
    op.drop_index("ix_operation_plans_agent_run_id", table_name="operation_plans")
    op.drop_table("operation_plans")
