"""增加结构化文档元素和解析器版本字段。"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_0002"
down_revision = "20260713_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展解析运行并创建结构化元素表。"""

    op.add_column(
        "document_extraction_runs",
        sa.Column("parser_name", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column(
        "document_extraction_runs",
        sa.Column("parser_version", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column(
        "document_extraction_runs",
        sa.Column("parser_config_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_document_extraction_runs_parser_config_hash",
        "document_extraction_runs",
        ["parser_config_hash"],
    )
    op.create_table(
        "document_elements",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("extraction_run_id", sa.String(length=36), nullable=False),
        sa.Column("element_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False, server_default="text"),
        sa.Column("text_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("bbox_json", sa.JSON(), nullable=False),
        sa.Column("content_layer", sa.String(length=80), nullable=False, server_default="body"),
        sa.Column("parent_ref", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["document_extraction_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("extraction_run_id", "element_index", name="uq_document_elements_run_index"),
    )
    op.create_index("ix_document_elements_document_id", "document_elements", ["document_id"])
    op.create_index("ix_document_elements_extraction_run_id", "document_elements", ["extraction_run_id"])


def downgrade() -> None:
    """删除结构化元素及解析器版本字段。"""

    op.drop_index("ix_document_elements_extraction_run_id", table_name="document_elements")
    op.drop_index("ix_document_elements_document_id", table_name="document_elements")
    op.drop_table("document_elements")
    op.drop_index("ix_document_extraction_runs_parser_config_hash", table_name="document_extraction_runs")
    op.drop_column("document_extraction_runs", "parser_config_hash")
    op.drop_column("document_extraction_runs", "parser_version")
    op.drop_column("document_extraction_runs", "parser_name")
