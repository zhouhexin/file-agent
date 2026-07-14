"""创建文件重命名待复核项表。"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0001"
down_revision = "20260713_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存待用户提供新名称的受管文件上下文。"""

    op.create_table(
        "file_rename_review_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("root_key", sa.String(length=100), nullable=False),
        sa.Column("original_relative_path", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="NEEDS_REVIEW"),
        sa.Column("review_context_json", sa.JSON(), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["managed_file_id"], ["managed_files.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", "managed_file_id", name="uq_file_rename_review_run_file"),
    )
    for column in ["conversation_id", "agent_run_id", "user_id", "managed_file_id", "document_id", "root_key", "original_filename", "status"]:
        op.create_index(
            f"ix_file_rename_review_items_{column}",
            "file_rename_review_items",
            [column],
            unique=False,
        )


def downgrade() -> None:
    """删除文件重命名待复核项表。"""

    op.drop_table("file_rename_review_items")
