"""创建 WorkBuddy 外部 OCR 任务和逐页状态表。

Revision ID: 20260908_0003
Revises: 20260908_0002
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260908_0003"
down_revision = "20260908_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立带租约、幂等提交和正式提取运行映射的外部 OCR 表。"""

    op.create_table(
        "external_extraction_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("ingest_item_id", sa.String(length=36), nullable=False),
        sa.Column("source_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=40), nullable=False, server_default="PARSE_STAGING"),
        sa.Column("provider_contract_version", sa.String(length=40), nullable=False),
        sa.Column("page_manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_submission_key", sa.String(length=200), nullable=True),
        sa.Column("last_submission_digest", sa.String(length=64), nullable=True),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=True),
        sa.Column("error_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ingest_item_id"], ["ingest_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ingest_item_id", "source_version_id", "provider_contract_version", name="uq_external_extraction_task_source_contract"),
    )
    for column in ("ingest_item_id", "source_version_id", "status", "lease_expires_at", "extraction_run_id"):
        op.create_index(f"ix_external_extraction_tasks_{column}", "external_extraction_tasks", [column])
    op.create_table(
        "external_extraction_pages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id"], ["external_extraction_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "page_number", name="uq_external_extraction_page"),
    )
    op.create_index("ix_external_extraction_pages_task_id", "external_extraction_pages", ["task_id"])
    op.create_index("ix_external_extraction_pages_status", "external_extraction_pages", ["status"])


def downgrade() -> None:
    """移除外部 OCR 派生状态，不触碰已经形成的 DocumentPage。"""

    op.drop_table("external_extraction_pages")
    op.drop_table("external_extraction_tasks")
