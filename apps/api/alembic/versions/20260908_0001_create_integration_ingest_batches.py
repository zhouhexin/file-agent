"""创建 WorkBuddy/MCP 批量导入与外部请求幂等表。

Revision ID: 20260908_0001
Revises: 20260901_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260908_0001"
down_revision = "20260901_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立批次业务事实；阶段任务仍由既有 ``filesystem_jobs`` 单独承载。"""

    op.create_table(
        "ingest_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.String(length=100), nullable=False),
        sa.Column("request_id", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("manifest_status", sa.String(length=30), nullable=False, server_default="OPEN"),
        sa.Column("manifest_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("result_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "policy_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("policy_version", sa.String(length=40), nullable=False, server_default="ingest-v1"),
        sa.Column("user_request", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "client_id",
            "idempotency_key",
            name="uq_ingest_batches_user_client_idempotency",
        ),
    )
    for name, column in (
        ("ix_ingest_batches_user_id", "user_id"),
        ("ix_ingest_batches_workspace_id", "workspace_id"),
        ("ix_ingest_batches_request_id", "request_id"),
        ("ix_ingest_batches_manifest_status", "manifest_status"),
        ("ix_ingest_batches_conversation_id", "conversation_id"),
        ("ix_ingest_batches_status", "status"),
    ):
        op.create_index(name, "ingest_batches", [column])
    op.create_index("ix_ingest_batches_user_status", "ingest_batches", ["user_id", "status"])

    op.create_table(
        "ingest_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("client_item_id", sa.String(length=160), nullable=False),
        sa.Column("source_root_ref", sa.String(length=100), nullable=False),
        sa.Column("source_relative_path", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("expected_size", sa.BigInteger(), nullable=False),
        sa.Column("source_mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("actual_sha256", sa.String(length=64), nullable=True),
        sa.Column("upload_document_version_id", sa.String(length=36), nullable=True),
        sa.Column("archive_record_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("stage", sa.String(length=40), nullable=False, server_default="RECEIVE"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column("decision", sa.String(length=40), nullable=True),
        sa.Column("final_document_id", sa.String(length=36), nullable=True),
        sa.Column("final_version_id", sa.String(length=36), nullable=True),
        sa.Column("final_working_copy_id", sa.String(length=36), nullable=True),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=True),
        sa.Column("current_job_id", sa.String(length=36), nullable=True),
        sa.Column(
            "error_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["archive_record_id"], ["upload_archive_records.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["batch_id"], ["ingest_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["current_job_id"], ["filesystem_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["final_document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["final_version_id"], ["document_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["final_working_copy_id"], ["working_copies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["upload_document_version_id"], ["document_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "client_item_id", name="uq_ingest_items_batch_client_item"),
    )
    for name, column in (
        ("ix_ingest_items_batch_id", "batch_id"),
        ("ix_ingest_items_actual_sha256", "actual_sha256"),
        ("ix_ingest_items_upload_document_version_id", "upload_document_version_id"),
        ("ix_ingest_items_archive_record_id", "archive_record_id"),
        ("ix_ingest_items_stage", "stage"),
        ("ix_ingest_items_status", "status"),
        ("ix_ingest_items_final_document_id", "final_document_id"),
        ("ix_ingest_items_final_version_id", "final_version_id"),
        ("ix_ingest_items_final_working_copy_id", "final_working_copy_id"),
        ("ix_ingest_items_extraction_run_id", "extraction_run_id"),
        ("ix_ingest_items_current_job_id", "current_job_id"),
    ):
        op.create_index(name, "ingest_items", [column])
    op.create_index("ix_ingest_items_batch_status", "ingest_items", ["batch_id", "status"])

    op.create_table(
        "integration_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.String(length=100), nullable=False),
        sa.Column("request_id", sa.String(length=120), nullable=False),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "target_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "client_id",
            "idempotency_key",
            name="uq_integration_requests_user_client_idempotency",
        ),
    )
    for name, column in (
        ("ix_integration_requests_user_id", "user_id"),
        ("ix_integration_requests_request_id", "request_id"),
        ("ix_integration_requests_operation", "operation"),
        ("ix_integration_requests_status", "status"),
    ):
        op.create_index(name, "integration_requests", [column])
    op.create_index(
        "ix_integration_requests_user_operation",
        "integration_requests",
        ["user_id", "operation"],
    )


def downgrade() -> None:
    """按依赖逆序移除新通道表，不触碰既有上传和工作副本数据。"""

    op.drop_table("integration_requests")
    op.drop_table("ingest_items")
    op.drop_table("ingest_batches")
