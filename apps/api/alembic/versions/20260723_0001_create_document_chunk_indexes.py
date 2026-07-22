"""创建 CPU-only DocumentVersion 原文索引与证据引用表。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260723_0001"
down_revision = "20260722_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建版本级 Chunk/Evidence，并只预留默认关闭的 pgvector 列。"""

    # 扩展由迁移显式创建；应用启动和普通 Tool 无权动态安装数据库扩展。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # 旧数据保持 NULL，避免把历史解析运行武断绑定到错误版本；新解析运行由后端固化版本 ID。
    op.add_column(
        "document_extraction_runs",
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_document_extraction_runs_version",
        "document_extraction_runs",
        "document_versions",
        ["document_version_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_document_extraction_runs_document_version_id",
        "document_extraction_runs",
        ["document_version_id"],
    )
    op.create_table(
        "document_index_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=False),
        sa.Column("index_version", sa.String(length=80), nullable=False),
        sa.Column("tokenizer", sa.String(length=40), nullable=False),
        sa.Column("tokenizer_version", sa.String(length=80), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("embedding_status", sa.String(length=40), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "extraction_run_id",
            "config_hash",
            name="uq_document_index_runs_version_extraction_config",
        ),
    )
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("index_run_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_type", sa.String(length=40), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("location_hash", sa.String(length=64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("sheet_name", sa.String(length=255), nullable=True),
        sa.Column("cell_range", sa.String(length=80), nullable=True),
        sa.Column("element_ids_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("embedding_status", sa.String(length=40), nullable=False),
        sa.Column("embedding_provider", sa.String(length=80), nullable=False),
        sa.Column("embedding_model", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["index_run_id"], ["document_index_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_run_id", "chunk_index", name="uq_document_chunks_run_index"),
    )
    # Alembic 的通用类型不能表达 pgvector 维度，因此在建表后替换为明确扩展类型。
    op.execute("ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(1536) USING NULL::vector(1536)")
    op.create_table(
        "evidence_spans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("chunk_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=False),
        sa.Column("span_index", sa.Integer(), nullable=False),
        sa.Column("evidence_type", sa.String(length=40), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("sheet_name", sa.String(length=255), nullable=True),
        sa.Column("cell_range", sa.String(length=80), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chunk_id", "span_index", name="uq_evidence_spans_chunk_index"),
    )
    op.create_table(
        "qa_answers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("retrieval_trace_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "answer_references",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("qa_answer_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_span_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("reference_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["qa_answer_id"], ["qa_answers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["evidence_span_id"], ["evidence_spans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("qa_answer_id", "reference_index", name="uq_answer_references_answer_index"),
    )
    for table_name, columns in {
        "document_index_runs": ("document_id", "document_version_id", "extraction_run_id", "config_hash", "status", "embedding_status"),
        "document_chunks": ("index_run_id", "document_id", "document_version_id", "extraction_run_id", "content_hash", "embedding_status"),
        "evidence_spans": ("chunk_id", "document_id", "document_version_id", "extraction_run_id"),
        "qa_answers": ("conversation_id", "user_id", "agent_run_id", "status"),
        "answer_references": ("qa_answer_id", "evidence_span_id", "document_id", "document_version_id"),
    }.items():
        for column in columns:
            op.create_index(f"ix_{table_name}_{column}", table_name, [column])
    op.create_index(
        "ix_document_chunks_search_vector_gin",
        "document_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_document_chunks_search_text_trgm",
        "document_chunks",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_document_versions_filename_trgm",
        "document_versions",
        ["filename"],
        postgresql_using="gin",
        postgresql_ops={"filename": "gin_trgm_ops"},
    )


def downgrade() -> None:
    """删除派生索引和回答占位表，不删除 DocumentVersion 或原始文件。"""

    op.drop_table("answer_references")
    op.drop_table("qa_answers")
    op.drop_table("evidence_spans")
    op.drop_table("document_chunks")
    op.drop_table("document_index_runs")
    op.drop_index("ix_document_versions_filename_trgm", table_name="document_versions")
    op.drop_index("ix_document_extraction_runs_document_version_id", table_name="document_extraction_runs")
    op.drop_constraint(
        "fk_document_extraction_runs_version",
        "document_extraction_runs",
        type_="foreignkey",
    )
    op.drop_column("document_extraction_runs", "document_version_id")
