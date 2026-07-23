"""创建阶段四 document_search_profiles 瘦检索投影表。

本迁移创建 document_search_profiles 表及其索引，用于两阶段检索的
第一阶段廉价文档级候选召回。该表是工作副本级可重建派生数据，
不替代 WorkingCopy、DocumentSummary、分类建议或 Evidence 等事实表。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260724_0001"
down_revision = "20260723_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 document_search_profiles 表及索引。

    该表为瘦投影设计：不复制完整 category_path_json、summary_preview 或 entities_json，
    只保存检索必需的规范化词项和稳定业务 ID。
    """

    op.create_table(
        "document_search_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("workspace_id", sa.String(36), nullable=False, index=True),
        sa.Column("working_copy_id", sa.String(36), unique=True, nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False, index=True),
        sa.Column("document_version_id", sa.String(36), nullable=False, index=True),
        sa.Column(
            "status",
            sa.String(40),
            nullable=False,
            server_default="ACTIVE",
            index=True,
        ),
        sa.Column("normalized_filename", sa.Text, nullable=True),
        sa.Column("filename_search_text", sa.Text, nullable=True),
        sa.Column("category_search_text", sa.Text, nullable=True),
        sa.Column("metadata_search_text", sa.Text, nullable=True),
        sa.Column("summary_search_text", sa.Text, nullable=True),
        sa.Column("combined_search_text", sa.Text, nullable=True),
        sa.Column("search_vector", sa.Text, nullable=True),
        sa.Column("source_fingerprint", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    # 组合索引：用户 + 工作区 + 状态，用于快速筛选可搜索范围
    op.create_index(
        "ix_dsp_user_ws_status",
        "document_search_profiles",
        ["user_id", "workspace_id", "status"],
    )

    # normalized_filename B-tree 索引，用于精确文件名匹配
    op.create_index(
        "ix_dsp_normalized_filename",
        "document_search_profiles",
        ["normalized_filename"],
        postgresql_using="btree",
    )

    # PostgreSQL 专用索引
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        # search_vector 使用 GIN 索引（simple 分词配置）
        # migration 内用 ALTER TABLE 添加 TSVECTOR 列，因 SQLAlchemy Text 与 TSVECTOR 不兼容
        op.execute(
            "ALTER TABLE document_search_profiles "
            "ALTER COLUMN search_vector TYPE TSVECTOR "
            "USING search_vector::TSVECTOR"
        )
        op.create_index(
            "ix_dsp_search_vector_gin",
            "document_search_profiles",
            ["search_vector"],
            postgresql_using="gin",
            postgresql_ops={"search_vector": "gin"},
        )

        # combined_search_text pg_trgm GIN 索引，用于受限的长短语和轻微错字补召回
        op.execute(
            "CREATE INDEX ix_dsp_combined_trgm ON document_search_profiles "
            "USING gin (combined_search_text gin_trgm_ops)"
        )


def downgrade() -> None:
    """删除 document_search_profiles 表。"""

    if op.get_context().dialect.name == "postgresql":
        op.drop_index("ix_dsp_search_vector_gin", table_name="document_search_profiles")
        op.drop_index("ix_dsp_combined_trgm", table_name="document_search_profiles")
    op.drop_index("ix_dsp_normalized_filename", table_name="document_search_profiles")
    op.drop_index("ix_dsp_user_ws_status", table_name="document_search_profiles")
    op.drop_table("document_search_profiles")
