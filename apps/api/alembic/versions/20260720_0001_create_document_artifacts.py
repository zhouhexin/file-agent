"""创建可复用文档派生件表。"""

from alembic import op
import sqlalchemy as sa


revision = "20260720_0001"
down_revision = "20260716_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建文档派生件及其复用索引。"""

    op.create_table(
        "document_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_type", sa.String(length=50), nullable=False),
        sa.Column("storage_backend", sa.String(length=40), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("converter_name", sa.String(length=80), nullable=False),
        sa.Column("converter_version", sa.String(length=120), nullable=False),
        sa.Column("converter_config_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "artifact_type",
            "source_sha256",
            "converter_config_hash",
            name="uq_document_artifacts_source_config",
        ),
    )
    op.create_index("ix_document_artifacts_document_id", "document_artifacts", ["document_id"])
    op.create_index("ix_document_artifacts_artifact_type", "document_artifacts", ["artifact_type"])
    op.create_index("ix_document_artifacts_sha256", "document_artifacts", ["sha256"])
    op.create_index("ix_document_artifacts_source_sha256", "document_artifacts", ["source_sha256"])
    op.create_index(
        "ix_document_artifacts_converter_config_hash",
        "document_artifacts",
        ["converter_config_hash"],
    )


def downgrade() -> None:
    """删除文档派生件表。"""

    op.drop_index("ix_document_artifacts_converter_config_hash", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_source_sha256", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_sha256", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_artifact_type", table_name="document_artifacts")
    op.drop_index("ix_document_artifacts_document_id", table_name="document_artifacts")
    op.drop_table("document_artifacts")
