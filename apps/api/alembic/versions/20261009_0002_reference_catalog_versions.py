"""Store versioned shared reference catalogs."""

from alembic import op
import sqlalchemy as sa


revision = "20261009_0002"
down_revision = "20261009_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_catalog_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("catalog_key", sa.String(100), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("document_version_id", sa.String(36), sa.ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("derivative_artifact_id", sa.String(36), sa.ForeignKey("document_artifacts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("index_storage_path", sa.Text(), nullable=False),
        sa.Column("index_sha256", sa.String(64), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("catalog_key", "version_number", name="uq_reference_catalog_version"),
        sa.UniqueConstraint("catalog_key", "source_sha256", name="uq_reference_catalog_source_sha"),
    )
    op.create_index("ix_reference_catalog_versions_catalog_key", "reference_catalog_versions", ["catalog_key"])
    op.create_index("ix_reference_catalog_versions_status", "reference_catalog_versions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_reference_catalog_versions_status", table_name="reference_catalog_versions")
    op.drop_index("ix_reference_catalog_versions_catalog_key", table_name="reference_catalog_versions")
    op.drop_table("reference_catalog_versions")
