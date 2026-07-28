"""创建阶段六正式分类、确认来源、澄清状态和图谱事务 Outbox。

Revision ID: 20260728_0001
Revises: 20260727_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260728_0001"
down_revision = "20260727_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补齐共享工作副本分类决定闭环所需的持久化事实。"""

    op.add_column(
        "document_category_feedback",
        sa.Column("working_copy_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "document_category_feedback",
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "document_category_feedback",
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_document_category_feedback_working_copy_id",
        "document_category_feedback",
        "working_copies",
        ["working_copy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_document_category_feedback_document_version_id",
        "document_category_feedback",
        "document_versions",
        ["document_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_document_category_feedback_working_copy_id",
        "document_category_feedback",
        ["working_copy_id"],
    )
    op.create_index(
        "ix_document_category_feedback_document_version_id",
        "document_category_feedback",
        ["document_version_id"],
    )
    op.create_index(
        "ix_document_category_feedback_idempotency_key",
        "document_category_feedback",
        ["idempotency_key"],
        unique=True,
    )

    op.create_table(
        "document_categories",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("working_copy_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("category_id", sa.String(length=255), nullable=False),
        sa.Column(
            "category_path_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("relation_role", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("taxonomy_key", sa.String(length=120), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=80), nullable=False),
        sa.Column("classifier_version", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_suggestion_id", sa.String(length=36), nullable=True),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["working_copy_id"], ["working_copies.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_suggestion_id"],
            ["document_category_suggestions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_document_categories_working_copy_id", ["working_copy_id"]),
        ("ix_document_categories_document_id", ["document_id"]),
        ("ix_document_categories_document_version_id", ["document_version_id"]),
        ("ix_document_categories_category_id", ["category_id"]),
        ("ix_document_categories_relation_role", ["relation_role"]),
        ("ix_document_categories_status", ["status"]),
        ("ix_document_categories_source_suggestion_id", ["source_suggestion_id"]),
    ):
        op.create_index(name, "document_categories", columns)
    op.create_index(
        "uq_document_categories_active_relation",
        "document_categories",
        ["working_copy_id", "document_version_id", "category_id", "relation_role"],
        unique=True,
        postgresql_where=sa.text("status = 'CONFIRMED'"),
    )
    op.create_index(
        "uq_document_categories_active_primary",
        "document_categories",
        ["working_copy_id", "document_version_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'CONFIRMED' AND relation_role = 'PRIMARY'"
        ),
    )

    op.create_table(
        "document_category_confirmation_sources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_category_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("feedback_id", sa.String(length=36), nullable=False),
        sa.Column("suggestion_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("supersedes_source_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_category_id"], ["document_categories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["feedback_id"], ["document_category_feedback.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["suggestion_id"],
            ["document_category_suggestions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_source_id"],
            ["document_category_confirmation_sources.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        (
            "ix_document_category_confirmation_sources_document_category_id",
            ["document_category_id"],
        ),
        ("ix_document_category_confirmation_sources_user_id", ["user_id"]),
        ("ix_document_category_confirmation_sources_feedback_id", ["feedback_id"]),
        ("ix_document_category_confirmation_sources_suggestion_id", ["suggestion_id"]),
        ("ix_document_category_confirmation_sources_status", ["status"]),
        (
            "ix_document_category_confirmation_sources_supersedes_source_id",
            ["supersedes_source_id"],
        ),
    ):
        op.create_index(
            name, "document_category_confirmation_sources", columns
        )
    op.create_index(
        "uq_document_category_sources_active_user",
        "document_category_confirmation_sources",
        ["document_category_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "classification_clarifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column(
            "options_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("selected_option_id", sa.String(length=80), nullable=True),
        sa.Column(
            "resolution_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_classification_clarifications_conversation_id", ["conversation_id"]),
        ("ix_classification_clarifications_user_id", ["user_id"]),
        ("ix_classification_clarifications_agent_run_id", ["agent_run_id"]),
        ("ix_classification_clarifications_status", ["status"]),
    ):
        op.create_index(name, "classification_clarifications", columns)

    op.create_table(
        "classification_graph_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_category_id", sa.String(length=36), nullable=False),
        sa.Column("working_copy_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("expected_status", sa.String(length=40), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_category_id"], ["document_categories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["working_copy_id"], ["working_copies.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deduplication_key",
            name="uq_classification_graph_outbox_deduplication_key",
        ),
    )
    for name, columns in (
        ("ix_classification_graph_outbox_document_category_id", ["document_category_id"]),
        ("ix_classification_graph_outbox_working_copy_id", ["working_copy_id"]),
        ("ix_classification_graph_outbox_document_version_id", ["document_version_id"]),
        ("ix_classification_graph_outbox_deduplication_key", ["deduplication_key"]),
        ("ix_classification_graph_outbox_status", ["status"]),
        ("ix_classification_graph_outbox_available_at", ["available_at"]),
    ):
        op.create_index(name, "classification_graph_outbox", columns)

    # 历史反馈先回填建议对应版本；只有能唯一关联当前共享工作副本的记录才补上
    # working_copy_id 和规范文档 ID，歧义或已失效记录保持 NULL，等待应用层复核。
    op.execute(
        """
        UPDATE document_category_feedback AS feedback
        SET document_version_id = suggestion.document_version_id
        FROM document_category_suggestions AS suggestion
        WHERE suggestion.id = feedback.suggestion_id
          AND feedback.document_version_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE document_category_feedback AS feedback
        SET working_copy_id = candidate.working_copy_id,
            document_id = candidate.document_id,
            document_version_id = candidate.current_version_id
        FROM (
            SELECT suggestion.id AS suggestion_id,
                   MIN(working_copy.id) AS working_copy_id,
                   MIN(working_copy.document_id) AS document_id,
                   MIN(working_copy.current_version_id) AS current_version_id
            FROM document_category_suggestions AS suggestion
            JOIN working_copies AS working_copy
              ON working_copy.document_id = suggestion.document_id
             AND working_copy.current_version_id = suggestion.document_version_id
            JOIN document_versions AS current_version
              ON current_version.id = working_copy.current_version_id
            WHERE working_copy.status = 'ACTIVE'
              AND working_copy.content_sha256 = current_version.sha256
            GROUP BY suggestion.id
            HAVING COUNT(*) = 1
        ) AS candidate
        WHERE candidate.suggestion_id = feedback.suggestion_id
          AND feedback.working_copy_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE document_category_feedback AS feedback
        SET working_copy_id = candidate.working_copy_id,
            document_id = candidate.document_id,
            document_version_id = candidate.current_version_id
        FROM (
            SELECT suggestion.id AS suggestion_id,
                   MIN(working_copy.id) AS working_copy_id,
                   MIN(working_copy.document_id) AS document_id,
                   MIN(working_copy.current_version_id) AS current_version_id
            FROM document_category_suggestions AS suggestion
            JOIN document_versions AS source_version
              ON source_version.id = suggestion.document_version_id
            JOIN upload_archive_records AS archive
              ON archive.upload_document_version_id = source_version.id
            JOIN working_copies AS working_copy
              ON working_copy.managed_file_id = archive.managed_file_id
             AND working_copy.is_primary_import = TRUE
            JOIN document_versions AS current_version
              ON current_version.id = working_copy.current_version_id
            WHERE source_version.sha256 = current_version.sha256
              AND working_copy.content_sha256 = current_version.sha256
              AND working_copy.status = 'ACTIVE'
            GROUP BY suggestion.id
            HAVING COUNT(*) = 1
        ) AS candidate
        WHERE candidate.suggestion_id = feedback.suggestion_id
          AND feedback.working_copy_id IS NULL
        """
    )


def downgrade() -> None:
    """移除阶段六新增事实，恢复原有建议反馈边界。"""

    op.drop_table("classification_graph_outbox")
    op.drop_table("classification_clarifications")
    op.drop_table("document_category_confirmation_sources")
    op.drop_table("document_categories")
    op.drop_index(
        "ix_document_category_feedback_idempotency_key",
        table_name="document_category_feedback",
    )
    op.drop_index(
        "ix_document_category_feedback_document_version_id",
        table_name="document_category_feedback",
    )
    op.drop_index(
        "ix_document_category_feedback_working_copy_id",
        table_name="document_category_feedback",
    )
    op.drop_constraint(
        "fk_document_category_feedback_document_version_id",
        "document_category_feedback",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_document_category_feedback_working_copy_id",
        "document_category_feedback",
        type_="foreignkey",
    )
    op.drop_column("document_category_feedback", "idempotency_key")
    op.drop_column("document_category_feedback", "document_version_id")
    op.drop_column("document_category_feedback", "working_copy_id")
