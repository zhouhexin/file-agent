"""创建受管文件用户快照关系表。"""

from alembic import op
import sqlalchemy as sa


revision = "20260713_0001"
down_revision = "20260711_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建按用户、受管文件和内容哈希唯一的快照关系。"""

    op.create_table(
        "managed_file_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["managed_file_id"], ["managed_files.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", name="uq_managed_file_snapshots_document_id"),
        sa.UniqueConstraint(
            "user_id",
            "managed_file_id",
            "source_sha256",
            name="uq_managed_file_snapshots_user_file_sha256",
        ),
    )
    op.create_index("ix_managed_file_snapshots_user_id", "managed_file_snapshots", ["user_id"])
    op.create_index("ix_managed_file_snapshots_managed_file_id", "managed_file_snapshots", ["managed_file_id"])
    op.create_index("ix_managed_file_snapshots_source_sha256", "managed_file_snapshots", ["source_sha256"])
    op.create_index("ix_managed_file_snapshots_status", "managed_file_snapshots", ["status"])


def downgrade() -> None:
    """删除受管文件快照关系；Document 和文件副本不自动删除。"""

    op.drop_index("ix_managed_file_snapshots_status", table_name="managed_file_snapshots")
    op.drop_index("ix_managed_file_snapshots_source_sha256", table_name="managed_file_snapshots")
    op.drop_index("ix_managed_file_snapshots_managed_file_id", table_name="managed_file_snapshots")
    op.drop_index("ix_managed_file_snapshots_user_id", table_name="managed_file_snapshots")
    op.drop_table("managed_file_snapshots")
