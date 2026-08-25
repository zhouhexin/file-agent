"""建立源侧预分析和按需物化工作副本所需的持久化事实。

Revision ID: 20260813_0001
Revises: 20260730_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260813_0001"
down_revision = "20260730_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建原始文件修订、分析、检索和相关文件集合，不复制任何原始文件。"""

    json_type = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "managed_file_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_identity", sa.String(length=160), nullable=True),
        sa.Column("quick_fingerprint", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="ANALYSIS_PENDING"),
        sa.Column("analysis_status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("analysis_document_id", sa.String(length=36), nullable=True),
        sa.Column("analysis_document_version_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["managed_file_id"], ["managed_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["analysis_document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["analysis_document_version_id"], ["document_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("managed_file_id", "revision_number", name="uq_managed_file_revisions_file_number"),
        sa.UniqueConstraint("analysis_document_id"),
        sa.UniqueConstraint("analysis_document_version_id"),
    )
    op.create_index("ix_managed_file_revisions_managed_file_id", "managed_file_revisions", ["managed_file_id"])
    op.create_index("ix_managed_file_revisions_quick_fingerprint", "managed_file_revisions", ["quick_fingerprint"])
    op.create_index("ix_managed_file_revisions_content_sha256", "managed_file_revisions", ["content_sha256"])
    op.create_index("ix_managed_file_revisions_status", "managed_file_revisions", ["status"])
    op.create_index("ix_managed_file_revisions_analysis_status", "managed_file_revisions", ["analysis_status"])
    op.create_index("ix_managed_file_revisions_is_current", "managed_file_revisions", ["is_current"])
    op.create_index("ix_managed_file_revisions_current", "managed_file_revisions", ["managed_file_id", "is_current"])

    op.create_table(
        "managed_file_analysis_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_revision_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="RUNNING"),
        sa.Column("parser_name", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("parser_version", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("converter_name", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("converter_version", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("summary_provider", sa.String(length=80), nullable=False, server_default="extractive"),
        sa.Column("summary_version", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("index_version", sa.String(length=80), nullable=False, server_default="chunk-index-v1"),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=True),
        sa.Column("index_run_id", sa.String(length=36), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["managed_file_revision_id"], ["managed_file_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["index_run_id"], ["document_index_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for field in ("managed_file_revision_id", "status", "extraction_run_id", "index_run_id"):
        op.create_index(f"ix_managed_file_analysis_runs_{field}", "managed_file_analysis_runs", [field])

    op.add_column("document_versions", sa.Column("source_managed_file_revision_id", sa.String(length=36), nullable=True))
    op.add_column("document_versions", sa.Column("source_analysis_run_id", sa.String(length=36), nullable=True))
    op.create_foreign_key("fk_document_versions_source_managed_file_revision", "document_versions", "managed_file_revisions", ["source_managed_file_revision_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_document_versions_source_analysis_run", "document_versions", "managed_file_analysis_runs", ["source_analysis_run_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_document_versions_source_managed_file_revision_id", "document_versions", ["source_managed_file_revision_id"])
    op.create_index("ix_document_versions_source_analysis_run_id", "document_versions", ["source_analysis_run_id"])

    op.create_table(
        "managed_file_search_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_revision_id", sa.String(length=36), nullable=False),
        sa.Column("analysis_run_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_filename", sa.Text(), nullable=False, server_default=""),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("topic_summary_json", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("keywords_json", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("entities_json", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("years_json", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("document_type", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("sheet_names_json", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["managed_file_revision_id"], ["managed_file_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["managed_file_analysis_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("managed_file_revision_id"),
    )
    for field in ("managed_file_revision_id", "analysis_run_id", "status"):
        op.create_index(f"ix_managed_file_search_profiles_{field}", "managed_file_search_profiles", [field])
    op.create_index("ix_managed_file_search_profiles_search_vector_gin", "managed_file_search_profiles", ["search_vector"], postgresql_using="gin")

    op.create_table(
        "managed_file_text_chunks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_revision_id", sa.String(length=36), nullable=False),
        sa.Column("document_chunk_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("sheet_name", sa.String(length=255), nullable=True),
        sa.Column("cell_range", sa.String(length=80), nullable=True),
        sa.Column("section_title", sa.Text(), nullable=True),
        sa.Column("text_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["managed_file_revision_id"], ["managed_file_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_chunk_id"], ["document_chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("managed_file_revision_id", "chunk_index", name="uq_managed_file_text_chunks_revision_index"),
        sa.UniqueConstraint("document_chunk_id"),
    )
    op.create_index("ix_managed_file_text_chunks_managed_file_revision_id", "managed_file_text_chunks", ["managed_file_revision_id"])
    op.create_index("ix_managed_file_text_chunks_document_chunk_id", "managed_file_text_chunks", ["document_chunk_id"])
    op.create_index("ix_managed_file_text_chunks_search_vector_gin", "managed_file_text_chunks", ["search_vector"], postgresql_using="gin")

    op.create_table(
        "managed_file_table_structures",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_revision_id", sa.String(length=36), nullable=False),
        sa.Column("sheet_name", sa.String(length=255), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("column_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("headers_json", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("column_types_json", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("date_ranges_json", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("numeric_statistics_json", json_type, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sample_values_json", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["managed_file_revision_id"], ["managed_file_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("managed_file_revision_id", "sheet_name", name="uq_managed_file_table_structures_revision_sheet"),
    )
    op.create_index("ix_managed_file_table_structures_managed_file_revision_id", "managed_file_table_structures", ["managed_file_revision_id"])

    op.create_table(
        "relevant_file_sets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("query_fingerprint", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="READY"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for field in ("workspace_id", "user_id", "conversation_id", "agent_run_id", "query_fingerprint", "status"):
        op.create_index(f"ix_relevant_file_sets_{field}", "relevant_file_sets", [field])
    op.create_table(
        "relevant_file_set_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("relevant_file_set_id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_id", sa.String(length=36), nullable=True),
        sa.Column("managed_file_revision_id", sa.String(length=36), nullable=True),
        sa.Column("working_copy_id", sa.String(length=36), nullable=True),
        sa.Column("resource_type", sa.String(length=40), nullable=False, server_default="WORKING_COPY"),
        sa.Column("relevance_tier", sa.String(length=40), nullable=False, server_default="RELATED"),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="READY"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["relevant_file_set_id"], ["relevant_file_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["managed_file_id"], ["managed_files.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["managed_file_revision_id"], ["managed_file_revisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["working_copy_id"], ["working_copies.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("relevant_file_set_id", "managed_file_revision_id", name="uq_relevant_file_set_revision"),
    )
    for field in ("relevant_file_set_id", "managed_file_id", "managed_file_revision_id", "working_copy_id", "resource_type", "relevance_tier", "status"):
        op.create_index(f"ix_relevant_file_set_items_{field}", "relevant_file_set_items", [field])


def downgrade() -> None:
    """删除可重建源侧分析派生表，并保留既有工作副本事实。"""

    for field in ("relevant_file_set_id", "managed_file_id", "managed_file_revision_id", "working_copy_id", "resource_type", "relevance_tier", "status"):
        op.drop_index(f"ix_relevant_file_set_items_{field}", table_name="relevant_file_set_items")
    op.drop_table("relevant_file_set_items")
    for field in ("workspace_id", "user_id", "conversation_id", "agent_run_id", "query_fingerprint", "status"):
        op.drop_index(f"ix_relevant_file_sets_{field}", table_name="relevant_file_sets")
    op.drop_table("relevant_file_sets")
    op.drop_index("ix_managed_file_table_structures_managed_file_revision_id", table_name="managed_file_table_structures")
    op.drop_table("managed_file_table_structures")
    op.drop_index("ix_managed_file_text_chunks_search_vector_gin", table_name="managed_file_text_chunks")
    op.drop_index("ix_managed_file_text_chunks_document_chunk_id", table_name="managed_file_text_chunks")
    op.drop_index("ix_managed_file_text_chunks_managed_file_revision_id", table_name="managed_file_text_chunks")
    op.drop_table("managed_file_text_chunks")
    op.drop_index("ix_managed_file_search_profiles_search_vector_gin", table_name="managed_file_search_profiles")
    for field in ("managed_file_revision_id", "analysis_run_id", "status"):
        op.drop_index(f"ix_managed_file_search_profiles_{field}", table_name="managed_file_search_profiles")
    op.drop_table("managed_file_search_profiles")
    op.drop_index("ix_document_versions_source_analysis_run_id", table_name="document_versions")
    op.drop_index("ix_document_versions_source_managed_file_revision_id", table_name="document_versions")
    op.drop_constraint("fk_document_versions_source_analysis_run", "document_versions", type_="foreignkey")
    op.drop_constraint("fk_document_versions_source_managed_file_revision", "document_versions", type_="foreignkey")
    op.drop_column("document_versions", "source_analysis_run_id")
    op.drop_column("document_versions", "source_managed_file_revision_id")
    for field in ("managed_file_revision_id", "status", "extraction_run_id", "index_run_id"):
        op.drop_index(f"ix_managed_file_analysis_runs_{field}", table_name="managed_file_analysis_runs")
    op.drop_table("managed_file_analysis_runs")
    op.drop_index("ix_managed_file_revisions_current", table_name="managed_file_revisions")
    for field in ("managed_file_id", "quick_fingerprint", "content_sha256", "status", "analysis_status", "is_current"):
        op.drop_index(f"ix_managed_file_revisions_{field}", table_name="managed_file_revisions")
    op.drop_table("managed_file_revisions")
