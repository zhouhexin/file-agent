"""创建批次附带请求的增量执行记录。

Revision ID: 20260908_0005
Revises: 20260908_0004
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260908_0005"
down_revision = "20260908_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存固定文件集合、异步任务与 AgentRun 的可恢复映射。"""

    op.create_table(
        "ingest_request_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("item_ids_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("document_ids_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("item_set_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column("filesystem_job_id", sa.String(length=36), nullable=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["batch_id"], ["ingest_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["filesystem_job_id"], ["filesystem_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "item_set_digest", name="uq_ingest_request_execution_item_set"),
    )
    for column in ("batch_id", "status", "filesystem_job_id", "conversation_id", "agent_run_id"):
        op.create_index(
            f"ix_ingest_request_executions_{column}",
            "ingest_request_executions",
            [column],
        )
    op.create_index(
        "ix_ingest_request_executions_batch_status",
        "ingest_request_executions",
        ["batch_id", "status"],
    )


def downgrade() -> None:
    """移除附带请求执行映射，不删除既有会话或 AgentRun。"""

    op.drop_table("ingest_request_executions")
