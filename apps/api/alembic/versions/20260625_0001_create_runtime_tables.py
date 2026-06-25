"""创建运行时持久化基础表。

Revision ID: 20260625_0001
Revises:
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 users、workspaces、conversations、messages、agent_runs 和 tool_invocations 表。"""

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("username", sa.String(length=100), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("display_name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("attachments_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("message_id", sa.String(length=36), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("intent", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="RECEIVED"),
        sa.Column("selected_skills_json", sa.JSON(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("graph_state_json", sa.JSON(), nullable=False),
        sa.Column("final_response", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"])
    op.create_index("ix_agent_runs_message_id", "agent_runs", ["message_id"])

    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("agent_run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("changeset_id", sa.String(length=36), nullable=True),
        sa.Column("operation_plan_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tool_invocations_agent_run_id", "tool_invocations", ["agent_run_id"])
    op.create_index("ix_tool_invocations_tool_name", "tool_invocations", ["tool_name"])


def downgrade() -> None:
    """按依赖顺序删除运行时持久化基础表。"""

    op.drop_index("ix_tool_invocations_tool_name", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_agent_run_id", table_name="tool_invocations")
    op.drop_table("tool_invocations")
    op.drop_index("ix_agent_runs_message_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_conversation_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("workspaces")
    op.drop_table("users")
