"""增加分类 PRIMARY、直接授权和可恢复落位的数据契约。

Revision ID: 20260910_0001
Revises: 20260908_0005
"""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260910_0001"
down_revision = "20260908_0005"
branch_labels = None
depends_on = None


JSONB_EMPTY_OBJECT = sa.text("'{}'::jsonb")
JSONB_EMPTY_ARRAY = sa.text("'[]'::jsonb")
ACTIVE_STATES = (
    "PREPARED",
    "EXECUTING",
    "FS_APPLIED",
    "RETRYABLE_FAILED",
    "RECONCILING",
)


def upgrade() -> None:
    """只扩展 schema 和审计关联，不移动文件或改写历史分类状态。"""

    op.add_column(
        "working_copies",
        sa.Column(
            "placement_status",
            sa.String(length=32),
            nullable=False,
            server_default="LEGACY_UNCHECKED",
        ),
    )
    op.add_column(
        "working_copies",
        sa.Column("placement_policy_version", sa.String(length=80), nullable=True),
    )
    op.create_index(
        "ix_working_copies_placement_status",
        "working_copies",
        ["placement_status"],
    )

    op.add_column(
        "document_classification_runs",
        sa.Column("input_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "document_classification_runs",
        sa.Column(
            "input_manifest_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_OBJECT,
        ),
    )
    op.add_column(
        "document_classification_runs",
        sa.Column(
            "decision_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_OBJECT,
        ),
    )
    op.create_index(
        "ix_document_classification_runs_input_fingerprint",
        "document_classification_runs",
        ["input_fingerprint"],
    )

    op.add_column(
        "operation_plans",
        sa.Column(
            "authorization_mode",
            sa.String(length=32),
            nullable=False,
            server_default="CONFIRMATION_REQUIRED",
        ),
    )
    op.add_column(
        "operation_plans",
        sa.Column(
            "authorization_context_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_OBJECT,
        ),
    )
    op.create_index(
        "ix_operation_plans_authorization_mode",
        "operation_plans",
        ["authorization_mode"],
    )
    op.drop_constraint(
        "operation_plans_conversation_id_fkey",
        "operation_plans",
        type_="foreignkey",
    )
    op.alter_column(
        "operation_plans",
        "conversation_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_operation_plans_conversation_nullable",
        "operation_plans",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "classification_purpose_packages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("root_key", sa.String(length=100), nullable=False),
        sa.Column("source_container_id", sa.String(length=160), nullable=False),
        sa.Column("purpose_category_id", sa.String(length=255), nullable=False),
        sa.Column("taxonomy_key", sa.String(length=120), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=80), nullable=False),
        sa.Column("policy_id", sa.String(length=120), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "members_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_ARRAY,
        ),
        sa.Column("authorization_source", sa.String(length=80), nullable=False),
        sa.Column("source_request_id", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "manifest_digest", name="uq_classification_purpose_packages_manifest"
        ),
    )
    short_index_names = {
        "expected_document_version_id": "ix_class_place_expected_version",
        "before_primary_relation_id": "ix_class_place_before_primary",
    }
    for column in (
        "workspace_id",
        "root_key",
        "source_container_id",
        "purpose_category_id",
        "manifest_digest",
        "source_request_id",
    ):
        op.create_index(
            f"ix_classification_purpose_packages_{column}",
            "classification_purpose_packages",
            [column],
        )

    op.create_table(
        "classification_placement_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("working_copy_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.String(length=120), nullable=False),
        sa.Column("request_id", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=32), nullable=False),
        sa.Column("operation_plan_id", sa.String(length=36), nullable=True),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("changeset_id", sa.String(length=36), nullable=True),
        sa.Column("authorization_source", sa.String(length=40), nullable=False),
        sa.Column("expected_revision", sa.BigInteger(), nullable=False),
        sa.Column("expected_document_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "source_identity_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_OBJECT,
        ),
        sa.Column("before_primary_relation_id", sa.String(length=36), nullable=True),
        sa.Column("before_relative_path", sa.Text(), nullable=False),
        sa.Column("target_relative_path", sa.Text(), nullable=False),
        sa.Column("target_category_id", sa.String(length=255), nullable=False),
        sa.Column("taxonomy_key", sa.String(length=120), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=80), nullable=False),
        sa.Column("taxonomy_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("target_filename", sa.Text(), nullable=False),
        sa.Column(
            "container_segments_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_ARRAY,
        ),
        sa.Column(
            "decision_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_OBJECT,
        ),
        sa.Column(
            "authorization_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_OBJECT,
        ),
        sa.Column(
            "state", sa.String(length=32), nullable=False, server_default="PREPARED"
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("execution_token", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "error_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_OBJECT,
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSONB_EMPTY_OBJECT,
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["working_copy_id"], ["working_copies.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["operation_plan_id"], ["operation_plans.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["filesystem_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["changeset_id"], ["change_sets.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["expected_document_version_id"],
            ["document_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["before_primary_relation_id"],
            ["document_categories.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "actor_user_id",
            "client_id",
            "idempotency_key",
            "working_copy_id",
            name="uq_classification_placement_idempotency",
        ),
    )
    for column in (
        "workspace_id",
        "actor_user_id",
        "working_copy_id",
        "request_id",
        "operation_type",
        "operation_plan_id",
        "job_id",
        "changeset_id",
        "authorization_source",
        "expected_document_version_id",
        "before_primary_relation_id",
        "target_category_id",
        "state",
        "execution_token",
        "lease_expires_at",
    ):
        op.create_index(
            short_index_names.get(
                column,
                f"ix_classification_placement_operations_{column}",
            ),
            "classification_placement_operations",
            [column],
        )
    op.create_index(
        "uq_classification_placement_active_copy",
        "classification_placement_operations",
        ["working_copy_id"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('PREPARED','EXECUTING','FS_APPLIED','RETRYABLE_FAILED','RECONCILING')"
        ),
    )

    op.create_table(
        "working_copy_path_reservations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("root_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_path_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_relative_path", sa.Text(), nullable=False),
        sa.Column("placement_operation_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["root_id"], ["working_copy_roots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["placement_operation_id"],
            ["classification_placement_operations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "root_id",
            "normalized_path_hash",
            name="uq_working_copy_path_reservation_target",
        ),
        sa.UniqueConstraint(
            "placement_operation_id",
            name="uq_working_copy_path_reservation_operation",
        ),
    )
    op.create_index(
        "ix_working_copy_path_reservations_root_id",
        "working_copy_path_reservations",
        ["root_id"],
    )
    op.create_index(
        "ix_working_copy_path_reservations_placement_operation_id",
        "working_copy_path_reservations",
        ["placement_operation_id"],
    )

    _add_placement_reference(
        "document_category_feedback",
        ondelete="SET NULL",
    )
    op.add_column(
        "document_category_feedback",
        sa.Column(
            "application_status",
            sa.String(length=32),
            nullable=False,
            server_default="LEGACY",
        ),
    )
    op.create_index(
        "ix_document_category_feedback_application_status",
        "document_category_feedback",
        ["application_status"],
    )
    _add_placement_reference("document_organization_decisions", ondelete="SET NULL")
    _add_placement_reference("working_copy_path_records", ondelete="SET NULL")
    _add_placement_reference("tool_invocations", ondelete="SET NULL")
    _add_placement_reference("change_sets", ondelete="SET NULL")

    op.drop_constraint(
        "tool_invocations_agent_run_id_fkey",
        "tool_invocations",
        type_="foreignkey",
    )
    op.alter_column(
        "tool_invocations",
        "agent_run_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_tool_invocations_agent_run_nullable",
        "tool_invocations",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_tool_invocations_audit_owner",
        "tool_invocations",
        "agent_run_id IS NOT NULL OR placement_operation_id IS NOT NULL",
    )

    for column, old_constraint, new_constraint, remote in (
        (
            "conversation_id",
            "change_sets_conversation_id_fkey",
            "fk_change_sets_conversation_nullable",
            "conversations",
        ),
        (
            "agent_run_id",
            "change_sets_agent_run_id_fkey",
            "fk_change_sets_agent_run_nullable",
            "agent_runs",
        ),
    ):
        op.drop_constraint(old_constraint, "change_sets", type_="foreignkey")
        op.alter_column(
            "change_sets",
            column,
            existing_type=sa.String(length=36),
            nullable=True,
        )
        op.create_foreign_key(
            new_constraint,
            "change_sets",
            remote,
            [column],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_check_constraint(
        "ck_change_sets_audit_owner",
        "change_sets",
        "agent_run_id IS NOT NULL OR placement_operation_id IS NOT NULL",
    )


def downgrade() -> None:
    """仅在没有未结束落位操作时移除本轮结构。"""

    if not context.is_offline_mode():
        active_count = op.get_bind().execute(
            sa.text(
                "SELECT count(*) FROM classification_placement_operations "
                "WHERE state IN ('PREPARED','EXECUTING','FS_APPLIED','RETRYABLE_FAILED','RECONCILING')"
            )
        ).scalar_one()
        if active_count:
            raise RuntimeError("存在未结束的分类落位操作，禁止降级删除恢复事实")

    op.drop_constraint("ck_change_sets_audit_owner", "change_sets", type_="check")
    for column, constraint, old_remote in (
        ("agent_run_id", "fk_change_sets_agent_run_nullable", "agent_runs"),
        ("conversation_id", "fk_change_sets_conversation_nullable", "conversations"),
    ):
        op.drop_constraint(constraint, "change_sets", type_="foreignkey")
        op.alter_column(
            "change_sets",
            column,
            existing_type=sa.String(length=36),
            nullable=False,
        )
        op.create_foreign_key(
            f"change_sets_{column}_fkey",
            "change_sets",
            old_remote,
            [column],
            ["id"],
        )

    op.drop_constraint(
        "ck_tool_invocations_audit_owner", "tool_invocations", type_="check"
    )
    op.drop_constraint(
        "fk_tool_invocations_agent_run_nullable",
        "tool_invocations",
        type_="foreignkey",
    )
    op.alter_column(
        "tool_invocations",
        "agent_run_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.create_foreign_key(
        "tool_invocations_agent_run_id_fkey",
        "tool_invocations",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
    )

    for table in (
        "change_sets",
        "tool_invocations",
        "working_copy_path_records",
        "document_organization_decisions",
    ):
        _drop_placement_reference(table)
    op.drop_index(
        "ix_document_category_feedback_application_status",
        table_name="document_category_feedback",
    )
    op.drop_column("document_category_feedback", "application_status")
    _drop_placement_reference("document_category_feedback")

    op.drop_table("working_copy_path_reservations")
    op.drop_index(
        "uq_classification_placement_active_copy",
        table_name="classification_placement_operations",
    )
    op.drop_table("classification_placement_operations")
    op.drop_table("classification_purpose_packages")

    op.drop_constraint(
        "fk_operation_plans_conversation_nullable",
        "operation_plans",
        type_="foreignkey",
    )
    op.alter_column(
        "operation_plans",
        "conversation_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
    op.create_foreign_key(
        "operation_plans_conversation_id_fkey",
        "operation_plans",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_index("ix_operation_plans_authorization_mode", table_name="operation_plans")
    op.drop_column("operation_plans", "authorization_context_json")
    op.drop_column("operation_plans", "authorization_mode")

    op.drop_index(
        "ix_document_classification_runs_input_fingerprint",
        table_name="document_classification_runs",
    )
    op.drop_column("document_classification_runs", "decision_json")
    op.drop_column("document_classification_runs", "input_manifest_json")
    op.drop_column("document_classification_runs", "input_fingerprint")
    op.drop_index("ix_working_copies_placement_status", table_name="working_copies")
    op.drop_column("working_copies", "placement_policy_version")
    op.drop_column("working_copies", "placement_status")


def _add_placement_reference(table_name: str, *, ondelete: str) -> None:
    """分阶段为现有表增加 placement operation 可空关联。"""

    op.add_column(
        table_name,
        sa.Column("placement_operation_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        f"fk_{table_name}_placement_operation",
        table_name,
        "classification_placement_operations",
        ["placement_operation_id"],
        ["id"],
        ondelete=ondelete,
    )
    op.create_index(
        f"ix_{table_name}_placement_operation_id",
        table_name,
        ["placement_operation_id"],
    )


def _drop_placement_reference(table_name: str) -> None:
    """按索引、外键、列的反向顺序移除关联。"""

    op.drop_index(
        f"ix_{table_name}_placement_operation_id",
        table_name=table_name,
    )
    op.drop_constraint(
        f"fk_{table_name}_placement_operation",
        table_name,
        type_="foreignkey",
    )
    op.drop_column(table_name, "placement_operation_id")
