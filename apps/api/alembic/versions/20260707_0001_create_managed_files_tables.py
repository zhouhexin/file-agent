"""创建服务器受管目录和文件系统异步任务表。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260707_0001"
down_revision = "20260625_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 P0 只读扫描所需表。"""

    op.create_table(
        "managed_roots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("root_key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("container_path", sa.String(length=500), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("allowed_operations_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("root_key"),
    )
    op.create_index("ix_managed_roots_created_by", "managed_roots", ["created_by"])
    op.create_index("ix_managed_roots_root_key", "managed_roots", ["root_key"])

    op.create_table(
        "managed_files",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("root_id", sa.String(length=36), nullable=False),
        sa.Column("relative_path", sa.String(length=1000), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("extension", sa.String(length=40), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("last_seen_scan_run_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["root_id"], ["managed_roots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("root_id", "relative_path", name="uq_managed_files_root_relative_path"),
    )
    op.create_index("ix_managed_files_root_id", "managed_files", ["root_id"])
    op.create_index("ix_managed_files_status", "managed_files", ["status"])
    op.create_index("ix_managed_files_last_seen_scan_run_id", "managed_files", ["last_seen_scan_run_id"])

    op.create_table(
        "filesystem_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("root_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["root_id"], ["managed_roots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_filesystem_jobs_job_type", "filesystem_jobs", ["job_type"])
    op.create_index("ix_filesystem_jobs_root_id", "filesystem_jobs", ["root_id"])
    op.create_index("ix_filesystem_jobs_status", "filesystem_jobs", ["status"])
    op.create_index("ix_filesystem_jobs_created_by", "filesystem_jobs", ["created_by"])

    op.create_table(
        "filesystem_job_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["filesystem_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_filesystem_job_events_job_id", "filesystem_job_events", ["job_id"])

    op.create_table(
        "filesystem_scan_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("root_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("files_discovered", sa.Integer(), nullable=False),
        sa.Column("files_updated", sa.Integer(), nullable=False),
        sa.Column("files_missing", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["filesystem_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["root_id"], ["managed_roots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_filesystem_scan_runs_root_id", "filesystem_scan_runs", ["root_id"])
    op.create_index("ix_filesystem_scan_runs_job_id", "filesystem_scan_runs", ["job_id"])


def downgrade() -> None:
    """删除 P0 只读扫描所需表。"""

    op.drop_index("ix_filesystem_scan_runs_job_id", table_name="filesystem_scan_runs")
    op.drop_index("ix_filesystem_scan_runs_root_id", table_name="filesystem_scan_runs")
    op.drop_table("filesystem_scan_runs")
    op.drop_index("ix_filesystem_job_events_job_id", table_name="filesystem_job_events")
    op.drop_table("filesystem_job_events")
    op.drop_index("ix_filesystem_jobs_created_by", table_name="filesystem_jobs")
    op.drop_index("ix_filesystem_jobs_status", table_name="filesystem_jobs")
    op.drop_index("ix_filesystem_jobs_root_id", table_name="filesystem_jobs")
    op.drop_index("ix_filesystem_jobs_job_type", table_name="filesystem_jobs")
    op.drop_table("filesystem_jobs")
    op.drop_index("ix_managed_files_last_seen_scan_run_id", table_name="managed_files")
    op.drop_index("ix_managed_files_status", table_name="managed_files")
    op.drop_index("ix_managed_files_root_id", table_name="managed_files")
    op.drop_table("managed_files")
    op.drop_index("ix_managed_roots_root_key", table_name="managed_roots")
    op.drop_index("ix_managed_roots_created_by", table_name="managed_roots")
    op.drop_table("managed_roots")
