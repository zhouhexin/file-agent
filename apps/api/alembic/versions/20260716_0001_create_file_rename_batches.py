"""创建完整范围文件重命名批次。"""

from alembic import op
import sqlalchemy as sa


revision = "20260716_0001"
down_revision = "20260715_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存批次范围、逐文件建议和用户决策。"""

    op.create_table(
        "file_rename_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("operation_plan_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="ANALYZING"),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ready_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("needs_review_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("excluded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operation_plan_id"], ["operation_plans.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ["workspace_id", "conversation_id", "agent_run_id", "user_id", "operation_plan_id", "status"]:
        op.create_index(f"ix_file_rename_batches_{column}", "file_rename_batches", [column])

    op.create_table(
        "file_rename_batch_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("rename_batch_id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("root_key", sa.String(length=100), nullable=False),
        sa.Column("original_relative_path", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("proposed_relative_path", sa.Text(), nullable=True),
        sa.Column("proposed_filename", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="NEEDS_REVIEW"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["managed_file_id"], ["managed_files.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rename_batch_id"], ["file_rename_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rename_batch_id", "managed_file_id", name="uq_file_rename_batch_file"),
    )
    for column in ["rename_batch_id", "managed_file_id", "document_id", "root_key", "status"]:
        op.create_index(f"ix_file_rename_batch_items_{column}", "file_rename_batch_items", [column])

    op.add_column("file_rename_review_items", sa.Column("rename_batch_id", sa.String(length=36), nullable=True))
    op.add_column("file_rename_review_items", sa.Column("rename_batch_item_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_file_rename_review_items_batch",
        "file_rename_review_items",
        "file_rename_batches",
        ["rename_batch_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_file_rename_review_items_batch_item",
        "file_rename_review_items",
        "file_rename_batch_items",
        ["rename_batch_item_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_file_rename_review_items_rename_batch_id", "file_rename_review_items", ["rename_batch_id"])
    op.create_index(
        "ix_file_rename_review_items_rename_batch_item_id",
        "file_rename_review_items",
        ["rename_batch_item_id"],
    )


def downgrade() -> None:
    """移除完整范围重命名批次结构。"""

    op.drop_index("ix_file_rename_review_items_rename_batch_item_id", table_name="file_rename_review_items")
    op.drop_index("ix_file_rename_review_items_rename_batch_id", table_name="file_rename_review_items")
    op.drop_constraint("fk_file_rename_review_items_batch_item", "file_rename_review_items", type_="foreignkey")
    op.drop_constraint("fk_file_rename_review_items_batch", "file_rename_review_items", type_="foreignkey")
    op.drop_column("file_rename_review_items", "rename_batch_item_id")
    op.drop_column("file_rename_review_items", "rename_batch_id")
    op.drop_table("file_rename_batch_items")
    op.drop_table("file_rename_batches")
