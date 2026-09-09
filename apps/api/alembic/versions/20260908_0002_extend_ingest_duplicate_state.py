"""扩展批内重复组和版本化确认状态。

Revision ID: 20260908_0002
Revises: 20260908_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260908_0002"
down_revision = "20260908_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加并发确认快照和同批哈希重复组，不改写历史决策含义。"""

    op.add_column("working_copies", sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("upload_duplicate_reviews", sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column(
        "upload_duplicate_reviews",
        sa.Column("comparison_phase", sa.String(length=30), nullable=False, server_default="EXACT"),
    )
    op.add_column("upload_duplicate_reviews", sa.Column("ingest_item_id", sa.String(length=36), nullable=True))
    op.add_column("upload_duplicate_reviews", sa.Column("selected_candidate_id", sa.String(length=36), nullable=True))
    op.add_column(
        "upload_duplicate_reviews",
        sa.Column(
            "decision_scope_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_foreign_key(
        "fk_upload_duplicate_reviews_ingest_item",
        "upload_duplicate_reviews", "ingest_items", ["ingest_item_id"], ["id"], ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_upload_duplicate_reviews_ingest_item", "upload_duplicate_reviews", ["ingest_item_id"]
    )
    op.create_index("ix_upload_duplicate_reviews_ingest_item_id", "upload_duplicate_reviews", ["ingest_item_id"])
    op.create_index("ix_upload_duplicate_reviews_selected_candidate_id", "upload_duplicate_reviews", ["selected_candidate_id"])

    for name, column_type in (
        ("candidate_ingest_item_id", sa.String(length=36)),
        ("compared_version_id", sa.String(length=36)),
        ("compared_sha256", sa.String(length=64)),
        ("compared_working_copy_revision", sa.BigInteger()),
    ):
        op.add_column("upload_duplicate_candidates", sa.Column(name, column_type, nullable=True))
    op.create_foreign_key(
        "fk_upload_duplicate_candidates_ingest_item",
        "upload_duplicate_candidates", "ingest_items", ["candidate_ingest_item_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_upload_duplicate_candidates_compared_version",
        "upload_duplicate_candidates", "document_versions", ["compared_version_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_upload_duplicate_candidates_candidate_ingest_item_id",
        "upload_duplicate_candidates", ["candidate_ingest_item_id"],
    )
    op.create_index(
        "ix_upload_duplicate_candidates_compared_version_id",
        "upload_duplicate_candidates", ["compared_version_id"],
    )

    op.create_table(
        "ingest_duplicate_groups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("primary_item_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["batch_id"], ["ingest_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["primary_item_id"], ["ingest_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("batch_id", "user_id", "workspace_id", "content_sha256", "primary_item_id", "status"):
        op.create_index(f"ix_ingest_duplicate_groups_{column}", "ingest_duplicate_groups", [column])
    op.create_index(
        "uq_ingest_duplicate_groups_active_content",
        "ingest_duplicate_groups",
        ["batch_id", "content_sha256"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "ingest_duplicate_group_members",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("ingest_item_id", sa.String(length=36), nullable=False),
        sa.Column("joined_revision", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.String(length=40), nullable=True),
        sa.Column("waits_for_item_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["ingest_duplicate_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ingest_item_id"], ["ingest_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["waits_for_item_id"], ["ingest_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "ingest_item_id", name="uq_ingest_duplicate_group_member"),
        sa.UniqueConstraint("ingest_item_id", name="uq_ingest_duplicate_group_member_item"),
    )
    for column in ("group_id", "ingest_item_id", "waits_for_item_id"):
        op.create_index(
            f"ix_ingest_duplicate_group_members_{column}", "ingest_duplicate_group_members", [column]
        )
    op.create_foreign_key(
        "fk_upload_duplicate_reviews_selected_candidate",
        "upload_duplicate_reviews", "upload_duplicate_candidates", ["selected_candidate_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    """逆序移除重复组扩展，保留既有上传确认记录。"""

    op.drop_constraint("fk_upload_duplicate_reviews_selected_candidate", "upload_duplicate_reviews", type_="foreignkey")
    op.drop_table("ingest_duplicate_group_members")
    op.drop_table("ingest_duplicate_groups")
    op.drop_index("ix_upload_duplicate_candidates_compared_version_id", table_name="upload_duplicate_candidates")
    op.drop_index("ix_upload_duplicate_candidates_candidate_ingest_item_id", table_name="upload_duplicate_candidates")
    op.drop_constraint("fk_upload_duplicate_candidates_compared_version", "upload_duplicate_candidates", type_="foreignkey")
    op.drop_constraint("fk_upload_duplicate_candidates_ingest_item", "upload_duplicate_candidates", type_="foreignkey")
    for column in ("compared_working_copy_revision", "compared_sha256", "compared_version_id", "candidate_ingest_item_id"):
        op.drop_column("upload_duplicate_candidates", column)
    op.drop_index("ix_upload_duplicate_reviews_selected_candidate_id", table_name="upload_duplicate_reviews")
    op.drop_index("ix_upload_duplicate_reviews_ingest_item_id", table_name="upload_duplicate_reviews")
    op.drop_constraint("uq_upload_duplicate_reviews_ingest_item", "upload_duplicate_reviews", type_="unique")
    op.drop_constraint("fk_upload_duplicate_reviews_ingest_item", "upload_duplicate_reviews", type_="foreignkey")
    for column in ("decision_scope_json", "selected_candidate_id", "ingest_item_id", "comparison_phase", "revision"):
        op.drop_column("upload_duplicate_reviews", column)
    op.drop_column("working_copies", "revision")
