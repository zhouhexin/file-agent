"""为受管目录增加分类模式和文件分类路径。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260707_0002"
down_revision = "20260707_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加已分类目录所需字段。"""

    op.add_column("managed_roots", sa.Column("classification_mode", sa.String(length=40), nullable=False, server_default="NONE"))
    op.add_column("managed_files", sa.Column("category_path", sa.String(length=1000), nullable=True))
    op.create_index("ix_managed_files_category_path", "managed_files", ["category_path"])
    op.alter_column("managed_roots", "classification_mode", server_default=None)


def downgrade() -> None:
    """移除已分类目录字段。"""

    op.drop_index("ix_managed_files_category_path", table_name="managed_files")
    op.drop_column("managed_files", "category_path")
    op.drop_column("managed_roots", "classification_mode")
