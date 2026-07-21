"""建立受管原始目录、工作副本目录和回收站目录三层生命周期。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260721_0001"
down_revision = "20260720_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建工作副本、上传查重确认和可恢复异步任务所需结构。"""

    op.add_column("managed_roots", sa.Column("archive_write_enabled", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("managed_roots", sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("managed_files", sa.Column("content_sha256", sa.String(length=64), nullable=True))
    op.add_column("managed_files", sa.Column("file_identity", sa.String(length=160), nullable=True))
    op.add_column(
        "managed_files",
        sa.Column("source_type", sa.String(length=40), server_default="DEPLOYED_FILE", nullable=False),
    )
    op.add_column("managed_files", sa.Column("source_upload_version_id", sa.String(length=36), nullable=True))
    op.add_column("managed_files", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_managed_files_content_sha256", "managed_files", ["content_sha256"])
    op.create_index("ix_managed_files_source_type", "managed_files", ["source_type"])
    op.create_index(
        "ix_managed_files_source_upload_version_id",
        "managed_files",
        ["source_upload_version_id"],
        unique=True,
    )

    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", sa.String(length=36), nullable=True),
        sa.Column("working_copy_id", sa.String(length=36), nullable=True),
        sa.Column("storage_tier", sa.String(length=40), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("source_managed_file_id", sa.String(length=36), nullable=True),
        sa.Column("operation_plan_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operation_plan_id"], ["operation_plans.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_version_id"], ["document_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_managed_file_id"], ["managed_files.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version_number", name="uq_document_versions_document_number"),
    )
    for column in (
        "document_id",
        "parent_version_id",
        "working_copy_id",
        "storage_tier",
        "sha256",
        "source_type",
        "source_managed_file_id",
        "operation_plan_id",
        "created_by",
    ):
        op.create_index(f"ix_document_versions_{column}", "document_versions", [column])

    op.create_foreign_key(
        "fk_managed_files_source_upload_version",
        "managed_files",
        "document_versions",
        ["source_upload_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "working_copy_roots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("managed_root_id", sa.String(length=36), nullable=False),
        sa.Column("root_key", sa.String(length=100), nullable=False),
        sa.Column("relative_storage_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["managed_root_id"], ["managed_roots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "managed_root_id", name="uq_working_copy_roots_workspace_managed"),
    )
    op.create_index("ix_working_copy_roots_workspace_id", "working_copy_roots", ["workspace_id"])
    op.create_index("ix_working_copy_roots_managed_root_id", "working_copy_roots", ["managed_root_id"])
    op.create_index("ix_working_copy_roots_root_key", "working_copy_roots", ["root_key"])
    op.create_index("ix_working_copy_roots_status", "working_copy_roots", ["status"])

    op.create_table(
        "working_copies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("working_copy_root_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("managed_file_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=True),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("relative_path_hash", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("extension", sa.String(length=40), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("imported_source_sha256", sa.String(length=64), nullable=False),
        sa.Column("is_primary_import", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("sync_status", sa.String(length=40), nullable=False),
        sa.Column("last_operation_plan_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["current_version_id"], ["document_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["last_operation_plan_id"], ["operation_plans.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["managed_file_id"], ["managed_files.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["working_copy_root_id"], ["working_copy_roots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "working_copy_root_id",
        "workspace_id",
        "managed_file_id",
        "document_id",
        "current_version_id",
        "relative_path_hash",
        "content_sha256",
        "imported_source_sha256",
        "is_primary_import",
        "status",
        "sync_status",
        "last_operation_plan_id",
    ):
        op.create_index(f"ix_working_copies_{column}", "working_copies", [column])
    op.create_index(
        "uq_working_copies_active_path",
        "working_copies",
        ["workspace_id", "working_copy_root_id", "relative_path_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "uq_working_copies_primary_import",
        "working_copies",
        ["working_copy_root_id", "managed_file_id"],
        unique=True,
        postgresql_where=sa.text("is_primary_import = true"),
    )
    op.create_foreign_key(
        "fk_document_versions_working_copy",
        "document_versions",
        "working_copies",
        ["working_copy_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "working_copy_path_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("working_copy_id", sa.String(length=36), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("operation_type", sa.String(length=40), nullable=False),
        sa.Column("before_relative_path", sa.Text(), nullable=False),
        sa.Column("after_relative_path", sa.Text(), nullable=False),
        sa.Column("before_filename", sa.Text(), nullable=False),
        sa.Column("after_filename", sa.Text(), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("operation_plan_id", sa.String(length=36), nullable=True),
        sa.Column("operation_confirmation_id", sa.String(length=36), nullable=True),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("tool_invocation_id", sa.String(length=36), nullable=True),
        sa.Column("changeset_id", sa.String(length=36), nullable=True),
        sa.Column("change_item_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("executed_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"]),
        sa.ForeignKeyConstraint(["change_item_id"], ["change_items.id"]),
        sa.ForeignKeyConstraint(["changeset_id"], ["change_sets.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["executed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["operation_confirmation_id"], ["operation_confirmations.id"]),
        sa.ForeignKeyConstraint(["operation_plan_id"], ["operation_plans.id"]),
        sa.ForeignKeyConstraint(["tool_invocation_id"], ["tool_invocations.id"]),
        sa.ForeignKeyConstraint(["working_copy_id"], ["working_copies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("working_copy_id", "sequence_number", name="uq_working_copy_path_sequence"),
    )
    op.create_index("ix_working_copy_path_records_working_copy_id", "working_copy_path_records", ["working_copy_id"])
    op.create_index("ix_working_copy_path_records_status", "working_copy_path_records", ["status"])
    op.create_index("ix_working_copy_path_records_updated_at", "working_copy_path_records", ["updated_at"])

    op.create_table(
        "trash_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("working_copy_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("entry_type", sa.String(length=40), nullable=False),
        sa.Column("original_relative_path", sa.Text(), nullable=False),
        sa.Column("trash_relative_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("operation_plan_id", sa.String(length=36), nullable=True),
        sa.Column("deleted_by", sa.String(length=36), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deleted_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.ForeignKeyConstraint(["operation_plan_id"], ["operation_plans.id"]),
        sa.ForeignKeyConstraint(["working_copy_id"], ["working_copies.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("workspace_id", "working_copy_id", "document_version_id", "entry_type", "status", "retention_until"):
        op.create_index(f"ix_trash_entries_{column}", "trash_entries", [column])

    op.create_table(
        "managed_file_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("root_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("source_relative_path", sa.Text(), nullable=False),
        sa.Column("target_relative_path", sa.Text(), nullable=True),
        sa.Column("observed_size", sa.BigInteger(), nullable=True),
        sa.Column("observed_mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("origin", sa.String(length=40), nullable=False),
        sa.Column("deduplication_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["root_id"], ["managed_roots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key"),
    )
    for column in ("root_id", "event_type", "origin", "deduplication_key", "status"):
        op.create_index(f"ix_managed_file_events_{column}", "managed_file_events", [column])

    _extend_filesystem_jobs()
    _create_upload_lifecycle_tables()
    _retire_legacy_rename_state()


def _retire_legacy_rename_state() -> None:
    """使尚未执行的原始目录/上传暂存重命名状态失效，禁止迁移后继续确认。"""

    op.execute(
        sa.text(
            """
            UPDATE operation_plans
            SET status = 'STALE', updated_at = CURRENT_TIMESTAMP
            WHERE operation_type IN ('RENAME_FILES', 'RENAME_UPLOADED_FILES')
              AND status IN ('PLANNED', 'WAITING_CONFIRMATION')
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE file_rename_review_items
            SET status = 'INVALIDATED', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'NEEDS_REVIEW'
            """
        )
    )


def _extend_filesystem_jobs() -> None:
    """给既有数据库队列增加分队列、重试和租约字段。"""

    op.add_column("filesystem_jobs", sa.Column("queue_name", sa.String(length=40), server_default="RECONCILE", nullable=False))
    op.add_column("filesystem_jobs", sa.Column("deduplication_key", sa.String(length=200), nullable=True))
    op.add_column("filesystem_jobs", sa.Column("priority", sa.Integer(), server_default="100", nullable=False))
    op.add_column("filesystem_jobs", sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("filesystem_jobs", sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False))
    op.add_column("filesystem_jobs", sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.add_column("filesystem_jobs", sa.Column("lease_owner", sa.String(length=100), nullable=True))
    op.add_column("filesystem_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("filesystem_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("filesystem_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("filesystem_jobs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    for column in ("queue_name", "deduplication_key", "priority", "available_at", "lease_expires_at"):
        op.create_index(
            f"ix_filesystem_jobs_{column}",
            "filesystem_jobs",
            [column],
            unique=column == "deduplication_key",
        )


def _create_upload_lifecycle_tables() -> None:
    """创建上传归档、重复确认和候选表。"""

    op.create_table(
        "upload_archive_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("upload_document_version_id", sa.String(length=36), nullable=False),
        sa.Column("managed_root_id", sa.String(length=36), nullable=True),
        sa.Column("managed_file_id", sa.String(length=36), nullable=True),
        sa.Column("archive_relative_path", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("filesystem_job_id", sa.String(length=36), nullable=True),
        sa.Column("changeset_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["changeset_id"], ["change_sets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["filesystem_job_id"], ["filesystem_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["managed_file_id"], ["managed_files.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["managed_root_id"], ["managed_roots.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["upload_document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("upload_document_version_id"),
    )
    for column in ("upload_document_version_id", "managed_root_id", "managed_file_id", "content_sha256", "status", "next_retry_at", "filesystem_job_id", "changeset_id"):
        op.create_index(f"ix_upload_archive_records_{column}", "upload_archive_records", [column])

    op.create_table(
        "upload_duplicate_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("upload_document_version_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("decision", sa.String(length=40), nullable=True),
        sa.Column("selected_existing_working_copy_id", sa.String(length=36), nullable=True),
        sa.Column("notification_message_id", sa.String(length=36), nullable=True),
        sa.Column("confirmation_message_id", sa.String(length=36), nullable=True),
        sa.Column("duplicate_check_job_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["confirmation_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["duplicate_check_job_id"], ["filesystem_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["notification_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["selected_existing_working_copy_id"], ["working_copies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["upload_document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("upload_document_version_id"),
    )
    for column in ("upload_document_version_id", "conversation_id", "workspace_id", "user_id", "status", "selected_existing_working_copy_id", "duplicate_check_job_id", "expires_at"):
        op.create_index(f"ix_upload_duplicate_reviews_{column}", "upload_duplicate_reviews", [column])

    op.create_table(
        "upload_duplicate_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("duplicate_review_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_managed_file_id", sa.String(length=36), nullable=True),
        sa.Column("candidate_working_copy_id", sa.String(length=36), nullable=True),
        sa.Column("match_type", sa.String(length=40), nullable=False),
        sa.Column("match_scope", sa.String(length=40), nullable=False),
        sa.Column("similarity_score", sa.Float(), nullable=False),
        sa.Column("match_evidence_json", sa.JSON(), nullable=False),
        sa.Column("user_visible_summary_json", sa.JSON(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["candidate_managed_file_id"], ["managed_files.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["candidate_working_copy_id"], ["working_copies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["duplicate_review_id"], ["upload_duplicate_reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "duplicate_review_id",
            "candidate_managed_file_id",
            "candidate_working_copy_id",
            "match_type",
            name="uq_upload_duplicate_candidate_target",
        ),
    )
    for column in ("duplicate_review_id", "candidate_managed_file_id", "candidate_working_copy_id", "match_type", "match_scope"):
        op.create_index(f"ix_upload_duplicate_candidates_{column}", "upload_duplicate_candidates", [column])


def downgrade() -> None:
    """按依赖顺序移除三层文件生命周期结构。"""

    op.drop_table("upload_duplicate_candidates")
    op.drop_table("upload_duplicate_reviews")
    op.drop_table("upload_archive_records")
    for column in ("lease_expires_at", "available_at", "priority", "deduplication_key", "queue_name"):
        op.drop_index(f"ix_filesystem_jobs_{column}", table_name="filesystem_jobs")
    for column in (
        "finished_at", "started_at", "heartbeat_at", "lease_expires_at", "lease_owner",
        "available_at", "max_attempts", "attempt_count", "priority", "deduplication_key", "queue_name",
    ):
        op.drop_column("filesystem_jobs", column)
    op.drop_table("managed_file_events")
    op.drop_table("trash_entries")
    op.drop_table("working_copy_path_records")
    op.drop_constraint("fk_document_versions_working_copy", "document_versions", type_="foreignkey")
    op.drop_table("working_copies")
    op.drop_table("working_copy_roots")
    op.drop_constraint("fk_managed_files_source_upload_version", "managed_files", type_="foreignkey")
    op.drop_table("document_versions")
    op.drop_index("ix_managed_files_source_upload_version_id", table_name="managed_files")
    op.drop_index("ix_managed_files_source_type", table_name="managed_files")
    op.drop_index("ix_managed_files_content_sha256", table_name="managed_files")
    for column in ("archived_at", "source_upload_version_id", "source_type", "file_identity", "content_sha256"):
        op.drop_column("managed_files", column)
    op.drop_column("managed_roots", "last_reconciled_at")
    op.drop_column("managed_roots", "archive_write_enabled")
