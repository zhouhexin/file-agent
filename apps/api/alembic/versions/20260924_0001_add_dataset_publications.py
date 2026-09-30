"""持久化累计 Excel 的数据库优先发布与恢复状态。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260924_0001"
down_revision = "20260922_0002"
branch_labels = None
depends_on = None


def upgrade():
    """发布记录与版本元数据同事务提交。"""
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "structured_dataset_publications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_id", sa.String(36), sa.ForeignKey("structured_datasets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("working_copy_id", sa.String(36), sa.ForeignKey("working_copies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("structured_dataset_tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("base_document_version_id", sa.String(36), nullable=True),
        sa.Column("candidate_document_version_id", sa.String(36), sa.ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("candidate_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_storage_path", sa.Text(), nullable=False),
        sa.Column("visible_relative_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("result_json", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("cleanup_json", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("agent_run_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", name="uq_dataset_publication_task"),
    )
    for name in ("dataset_id", "working_copy_id", "task_id", "status"):
        op.create_index(f"ix_structured_dataset_publications_{name}", "structured_dataset_publications", [name])
    op.create_index("uq_dataset_publication_open", "structured_dataset_publications", ["dataset_id"],
                    unique=True,
                    postgresql_where=sa.text("status IN ('DB_COMMITTED', 'NEEDS_REPAIR')"),
                    sqlite_where=sa.text("status IN ('DB_COMMITTED', 'NEEDS_REPAIR')"))


def downgrade():
    """仅回滚结构；生产回滚前必须先对账并清空未完成发布。"""
    op.drop_table("structured_dataset_publications")
