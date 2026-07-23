"""完善阶段四检索投影的 PostgreSQL 词法索引。

投影必须在写入事务内生成加权 TSVECTOR，不能把 GIN 索引留给查询时回填。
同时把 pg_trgm 的辅助索引限定到规范化文件名，避免摘要文本参与文件名模糊匹配。
"""

from __future__ import annotations

from alembic import op


revision = "20260724_0002"
down_revision = "20260724_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建投影 TSVECTOR trigger，并回填已有投影。"""

    if op.get_context().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_dsp_combined_trgm")
    op.execute(
        "CREATE INDEX ix_dsp_normalized_filename_trgm ON document_search_profiles "
        "USING gin (normalized_filename gin_trgm_ops)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION document_search_profiles_refresh_vector()
        RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('simple', coalesce(NEW.filename_search_text, '')), 'A') ||
                setweight(to_tsvector('simple', coalesce(NEW.category_search_text, '')), 'B') ||
                setweight(to_tsvector('simple', coalesce(NEW.metadata_search_text, '')), 'C') ||
                setweight(to_tsvector('simple', coalesce(NEW.summary_search_text, '')), 'D');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_search_profiles_refresh_vector
        BEFORE INSERT OR UPDATE OF filename_search_text, category_search_text,
            metadata_search_text, summary_search_text
        ON document_search_profiles
        FOR EACH ROW EXECUTE FUNCTION document_search_profiles_refresh_vector();
        """
    )
    op.execute(
        """
        UPDATE document_search_profiles
        SET search_vector =
            setweight(to_tsvector('simple', coalesce(filename_search_text, '')), 'A') ||
            setweight(to_tsvector('simple', coalesce(category_search_text, '')), 'B') ||
            setweight(to_tsvector('simple', coalesce(metadata_search_text, '')), 'C') ||
            setweight(to_tsvector('simple', coalesce(summary_search_text, '')), 'D')
        """
    )


def downgrade() -> None:
    """移除 trigger 和辅助索引，保留投影业务数据。"""

    if op.get_context().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_document_search_profiles_refresh_vector ON document_search_profiles")
    op.execute("DROP FUNCTION IF EXISTS document_search_profiles_refresh_vector()")
    op.execute("DROP INDEX IF EXISTS ix_dsp_normalized_filename_trgm")
    op.execute(
        "CREATE INDEX ix_dsp_combined_trgm ON document_search_profiles "
        "USING gin (combined_search_text gin_trgm_ops)"
    )
