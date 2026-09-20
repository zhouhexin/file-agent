"""增加共享累计表格 Demo 的成员与独立导出，不改变现有文件事实。

Revision ID: 20260920_0001
Revises: 20260914_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260920_0001"
down_revision = "20260914_0001"
branch_labels = None
depends_on = None


def upgrade():
    """只新增三张表，来源外键使用 RESTRICT 防止累计事实失去依据。"""
    op.create_table("structured_datasets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("schema_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("creation_key", sa.String(64), nullable=False),
        sa.Column("creation_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("creation_key", name="uq_dataset_creation_key"),
    )
    op.create_table("structured_dataset_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_id", sa.String(36), sa.ForeignKey("structured_datasets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("structured_extraction_run_id", sa.String(36), sa.ForeignKey("structured_extraction_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("document_version_id", sa.String(36), sa.ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("append_key", sa.String(120), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("appended_revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dataset_id", "document_version_id", name="uq_dataset_source_version"),
        sa.UniqueConstraint("dataset_id", "append_key", name="uq_dataset_append_key"),
    )
    op.create_index("ix_structured_dataset_members_dataset_id", "structured_dataset_members", ["dataset_id"])
    op.create_table("structured_dataset_exports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_id", sa.String(36), sa.ForeignKey("structured_datasets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("dataset_revision", sa.Integer, nullable=False),
        sa.Column("storage_path", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dataset_id", "dataset_revision", name="uq_dataset_export_revision"),
    )
    op.create_index("ix_structured_dataset_exports_dataset_id", "structured_dataset_exports", ["dataset_id"])


def downgrade():
    """仅回退 Demo 新表，不修改 Document、结构化运行或已有审计。"""
    op.drop_table("structured_dataset_exports")
    op.drop_table("structured_dataset_members")
    op.drop_table("structured_datasets")
