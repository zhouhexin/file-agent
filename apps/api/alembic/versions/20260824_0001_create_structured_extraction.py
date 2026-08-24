"""创建图片动态结构化抽取运行和字段证据表。

Revision ID: 20260824_0001
Revises: 20260730_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260824_0001"
down_revision = "20260730_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建结构化抽取持久化事实和必要索引。"""

    op.create_table(
        "structured_extraction_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("layout_extraction_run_id", sa.String(length=36), nullable=True),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("schema_mode", sa.String(length=40), nullable=False),
        sa.Column(
            "field_schema_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("schema_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("record_mode", sa.String(length=40), nullable=False),
        sa.Column("presentation", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model_name", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("retry_strategy", sa.String(length=40), nullable=False, server_default="INITIAL"),
        sa.Column(
            "target_field_keys_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("parent_run_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column("record_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_required_field_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("quality_band", sa.String(length=20), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["layout_extraction_run_id"],
            ["document_extraction_runs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["structured_extraction_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, column in (
        ("ix_structured_extraction_runs_document_id", "document_id"),
        ("ix_structured_extraction_runs_document_version_id", "document_version_id"),
        ("ix_structured_extraction_runs_layout_extraction_run_id", "layout_extraction_run_id"),
        ("ix_structured_extraction_runs_agent_run_id", "agent_run_id"),
        ("ix_structured_extraction_runs_schema_fingerprint", "schema_fingerprint"),
        ("ix_structured_extraction_runs_parent_run_id", "parent_run_id"),
        ("ix_structured_extraction_runs_status", "status"),
    ):
        op.create_index(name, "structured_extraction_runs", [column])
    op.create_index(
        "ix_structured_extraction_runs_cache_lookup",
        "structured_extraction_runs",
        [
            "document_version_id",
            "schema_fingerprint",
            "provider",
            "model_name",
            "prompt_version",
            "retry_strategy",
        ],
    )

    op.create_table(
        "structured_extraction_fields",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("structured_extraction_run_id", sa.String(length=36), nullable=False),
        sa.Column("record_index", sa.Integer(), nullable=False),
        sa.Column("field_key", sa.String(length=64), nullable=False),
        sa.Column("field_label", sa.String(length=80), nullable=False),
        sa.Column("field_type", sa.String(length=40), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column(
            "normalized_value_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column(
            "bbox_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "evidence_element_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "warning_codes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["structured_extraction_run_id"],
            ["structured_extraction_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "structured_extraction_run_id",
            "record_index",
            "field_key",
            name="uq_structured_extraction_fields_record_key",
        ),
    )
    op.create_index(
        "ix_structured_extraction_fields_run_id",
        "structured_extraction_fields",
        ["structured_extraction_run_id"],
    )
    op.create_index(
        "ix_structured_extraction_fields_status",
        "structured_extraction_fields",
        ["status"],
    )


def downgrade() -> None:
    """移除图片结构化抽取表。"""

    op.drop_index("ix_structured_extraction_fields_status", table_name="structured_extraction_fields")
    op.drop_index("ix_structured_extraction_fields_run_id", table_name="structured_extraction_fields")
    op.drop_table("structured_extraction_fields")
    op.drop_index(
        "ix_structured_extraction_runs_cache_lookup",
        table_name="structured_extraction_runs",
    )
    for name in (
        "ix_structured_extraction_runs_status",
        "ix_structured_extraction_runs_parent_run_id",
        "ix_structured_extraction_runs_schema_fingerprint",
        "ix_structured_extraction_runs_agent_run_id",
        "ix_structured_extraction_runs_layout_extraction_run_id",
        "ix_structured_extraction_runs_document_version_id",
        "ix_structured_extraction_runs_document_id",
    ):
        op.drop_index(name, table_name="structured_extraction_runs")
    op.drop_table("structured_extraction_runs")
