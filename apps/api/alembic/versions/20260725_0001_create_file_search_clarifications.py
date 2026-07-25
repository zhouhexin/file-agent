"""创建文件检索歧义选择持久化表。

Revision ID: 20260725_0001
Revises: 20260724_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20260725_0001"
down_revision = "20260724_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建可跨刷新恢复的检索选择记录，不能用前端临时状态替代。"""

    op.create_table(
        "file_search_clarifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("original_query", sa.Text(), nullable=False),
        sa.Column("core_phrase", sa.String(length=120), nullable=False),
        sa.Column("relation_mode", sa.String(length=30), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("selected_option_id", sa.String(length=80), nullable=True),
        sa.Column("result_message_id", sa.String(length=36), nullable=True),
        sa.Column("result_agent_run_id", sa.String(length=36), nullable=True),
        sa.Column(
            "resolution_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["result_message_id"], ["messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["result_agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_file_search_clarifications_conversation_id",
        "file_search_clarifications",
        ["conversation_id"],
    )
    op.create_index(
        "ix_file_search_clarifications_user_id",
        "file_search_clarifications",
        ["user_id"],
    )
    op.create_index(
        "ix_file_search_clarifications_agent_run_id",
        "file_search_clarifications",
        ["agent_run_id"],
    )
    op.create_index(
        "ix_file_search_clarifications_status",
        "file_search_clarifications",
        ["status"],
    )
    op.create_index(
        "ix_file_search_clarifications_result_message_id",
        "file_search_clarifications",
        ["result_message_id"],
    )
    op.create_index(
        "ix_file_search_clarifications_result_agent_run_id",
        "file_search_clarifications",
        ["result_agent_run_id"],
    )


def downgrade() -> None:
    """移除检索歧义选择表。"""

    op.drop_index(
        "ix_file_search_clarifications_result_agent_run_id",
        table_name="file_search_clarifications",
    )
    op.drop_index(
        "ix_file_search_clarifications_result_message_id",
        table_name="file_search_clarifications",
    )
    op.drop_index(
        "ix_file_search_clarifications_status",
        table_name="file_search_clarifications",
    )
    op.drop_index(
        "ix_file_search_clarifications_agent_run_id",
        table_name="file_search_clarifications",
    )
    op.drop_index(
        "ix_file_search_clarifications_user_id",
        table_name="file_search_clarifications",
    )
    op.drop_index(
        "ix_file_search_clarifications_conversation_id",
        table_name="file_search_clarifications",
    )
    op.drop_table("file_search_clarifications")
