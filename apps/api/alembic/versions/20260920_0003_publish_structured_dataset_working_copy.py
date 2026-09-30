"""将累计表格导出发布为单一工作副本及其版本。

Revision ID: 20260920_0003
Revises: 20260920_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "20260920_0003"
down_revision = "20260920_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加数据集到工作副本、导出到文档版本的稳定关联。"""

    op.add_column(
        "structured_datasets",
        sa.Column("working_copy_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_structured_datasets_working_copy_id",
        "structured_datasets",
        "working_copies",
        ["working_copy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_structured_datasets_working_copy_id",
        "structured_datasets",
        ["working_copy_id"],
        unique=True,
    )
    op.add_column(
        "structured_dataset_exports",
        sa.Column("document_version_id", sa.String(36), nullable=True),
    )
    op.create_foreign_key(
        "fk_structured_dataset_exports_document_version_id",
        "structured_dataset_exports",
        "document_versions",
        ["document_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_structured_dataset_exports_document_version_id",
        "structured_dataset_exports",
        ["document_version_id"],
        unique=True,
    )


def downgrade() -> None:
    """移除发布关联，不删除已经生成的工作副本和版本。"""

    op.drop_index(
        "ix_structured_dataset_exports_document_version_id",
        table_name="structured_dataset_exports",
    )
    op.drop_constraint(
        "fk_structured_dataset_exports_document_version_id",
        "structured_dataset_exports",
        type_="foreignkey",
    )
    op.drop_column("structured_dataset_exports", "document_version_id")
    op.drop_index(
        "ix_structured_datasets_working_copy_id",
        table_name="structured_datasets",
    )
    op.drop_constraint(
        "fk_structured_datasets_working_copy_id",
        "structured_datasets",
        type_="foreignkey",
    )
    op.drop_column("structured_datasets", "working_copy_id")
