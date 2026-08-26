"""为旧版 Office 持久化派生件增加内容版本关联和转换元数据。

Revision ID: 20260826_0001
Revises: 20260825_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260826_0001"
down_revision = "20260825_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可回滚字段，并只回填能够唯一确认的历史 DOC 内容版本。"""

    bind = op.get_bind()
    metadata_type = (
        postgresql.JSONB(astext_type=sa.Text())
        if bind.dialect.name == "postgresql"
        else sa.JSON()
    )
    metadata_default = (
        sa.text("'{}'::jsonb")
        if bind.dialect.name == "postgresql"
        else sa.text("'{}'")
    )
    with op.batch_alter_table("document_artifacts") as batch_op:
        batch_op.add_column(sa.Column("document_version_id", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column(
                "metadata_json",
                metadata_type,
                nullable=False,
                server_default=metadata_default,
            )
        )

    # 只有 document_id + source_sha256 唯一对应一个版本时才回填；历史歧义数据
    # 保持 NULL，不能为了提高覆盖率猜测派生件属于哪个内容版本。
    op.execute(
        sa.text(
            """
            UPDATE document_artifacts
               SET document_version_id = (
                   SELECT MIN(document_versions.id)
                     FROM document_versions
                    WHERE document_versions.document_id = document_artifacts.document_id
                      AND document_versions.sha256 = document_artifacts.source_sha256
               )
             WHERE document_version_id IS NULL
               AND artifact_type = 'CONVERTED_DOCX'
               AND 1 = (
                   SELECT COUNT(*)
                     FROM document_versions
                    WHERE document_versions.document_id = document_artifacts.document_id
                      AND document_versions.sha256 = document_artifacts.source_sha256
               )
            """
        )
    )

    with op.batch_alter_table("document_artifacts") as batch_op:
        batch_op.create_index(
            "ix_document_artifacts_document_version_id",
            ["document_version_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_document_artifacts_document_version_id",
            "document_versions",
            ["document_version_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_document_artifacts_version_source_config",
            [
                "document_version_id",
                "artifact_type",
                "source_sha256",
                "converter_config_hash",
            ],
        )


def downgrade() -> None:
    """仅移除新增数据库结构，保留磁盘上的派生文件供代码回滚后审计。"""

    with op.batch_alter_table("document_artifacts") as batch_op:
        batch_op.drop_constraint(
            "uq_document_artifacts_version_source_config",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_document_artifacts_document_version_id",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_document_artifacts_document_version_id")
        batch_op.drop_column("metadata_json")
        batch_op.drop_column("document_version_id")
