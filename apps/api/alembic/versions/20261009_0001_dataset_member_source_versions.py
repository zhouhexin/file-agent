"""Record all source versions used by a grouped Excel row."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20261009_0001"
down_revision = "20260924_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("structured_dataset_members", sa.Column(
        "source_versions_json", sa.JSON().with_variant(JSONB(), "postgresql"),
        nullable=False, server_default=sa.text("'[]'")))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("UPDATE structured_dataset_members SET source_versions_json = json_build_array(document_version_id)")
    else:
        op.execute("UPDATE structured_dataset_members SET source_versions_json = json_array(document_version_id)")


def downgrade() -> None:
    op.drop_column("structured_dataset_members", "source_versions_json")
