"""增加累计 Excel 的可见文件名、工作表和上传模板来源。

Revision ID: 20260920_0005
Revises: 20260920_0004
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260920_0005"
down_revision = "20260920_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为按文件名续写和上传 Excel 初始数据增加持久化字段。"""

    op.add_column(
        "structured_datasets",
        sa.Column("target_filename", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "structured_datasets",
        sa.Column("target_sheet_name", sa.Text(), nullable=False, server_default="Sheet1"),
    )
    op.add_column(
        "structured_datasets",
        sa.Column(
            "base_rows_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "structured_datasets",
        sa.Column("target_source_version_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_structured_datasets_target_source_version_id",
        "structured_datasets",
        "document_versions",
        ["target_source_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_structured_datasets_target_source_version_id",
        "structured_datasets",
        ["target_source_version_id"],
        unique=False,
    )
def downgrade() -> None:
    """移除目标表元数据，不删除已经发布的普通 Excel 文件。"""

    op.drop_index(
        "ix_structured_datasets_target_source_version_id",
        table_name="structured_datasets",
    )
    op.drop_constraint(
        "fk_structured_datasets_target_source_version_id",
        "structured_datasets",
        type_="foreignkey",
    )
    op.drop_column("structured_datasets", "target_source_version_id")
    op.drop_column("structured_datasets", "base_rows_json")
    op.drop_column("structured_datasets", "target_sheet_name")
    op.drop_column("structured_datasets", "target_filename")
