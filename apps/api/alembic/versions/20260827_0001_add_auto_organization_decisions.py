"""增加置信门控自动主分类和首次落位审计。

Revision ID: 20260827_0001
Revises: 20260826_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260827_0001"
down_revision = "20260826_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展活动分类约束并创建组织决策审计表。"""

    bind = op.get_bind()
    json_type = (
        postgresql.JSONB(astext_type=sa.Text())
        if bind.dialect.name == "postgresql"
        else sa.JSON()
    )
    empty_object = (
        sa.text("'{}'::jsonb") if bind.dialect.name == "postgresql" else sa.text("'{}'")
    )
    empty_array = (
        sa.text("'[]'::jsonb") if bind.dialect.name == "postgresql" else sa.text("'[]'")
    )

    op.drop_index("uq_document_categories_active_primary", table_name="document_categories")
    op.drop_index("uq_document_categories_active_relation", table_name="document_categories")
    active_predicate = sa.text("status IN ('AUTO_APPLIED', 'CONFIRMED')")
    active_primary_predicate = sa.text(
        "status IN ('AUTO_APPLIED', 'CONFIRMED') AND relation_role = 'PRIMARY'"
    )
    dialect_where = (
        {"postgresql_where": active_predicate}
        if bind.dialect.name == "postgresql"
        else {"sqlite_where": active_predicate}
    )
    primary_dialect_where = (
        {"postgresql_where": active_primary_predicate}
        if bind.dialect.name == "postgresql"
        else {"sqlite_where": active_primary_predicate}
    )
    op.create_index(
        "uq_document_categories_active_relation",
        "document_categories",
        ["working_copy_id", "document_version_id", "category_id", "relation_role"],
        unique=True,
        **dialect_where,
    )
    op.create_index(
        "uq_document_categories_active_primary",
        "document_categories",
        ["working_copy_id", "document_version_id"],
        unique=True,
        **primary_dialect_where,
    )

    op.create_table(
        "document_organization_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("working_copy_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("classification_run_id", sa.String(length=36), nullable=True),
        sa.Column("primary_suggestion_id", sa.String(length=36), nullable=True),
        sa.Column("category_id", sa.String(length=255), nullable=True),
        sa.Column("taxonomy_key", sa.String(length=120), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=80), nullable=False),
        sa.Column("classifier_version", sa.String(length=120), nullable=False),
        sa.Column("calibration_version", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("decision", sa.String(length=40), nullable=False),
        sa.Column("calibrated_confidence", sa.Float(), nullable=True),
        sa.Column("required_threshold", sa.Float(), nullable=True),
        sa.Column("top_margin", sa.Float(), nullable=True),
        sa.Column("required_margin", sa.Float(), nullable=True),
        sa.Column(
            "feature_snapshot_json", json_type, nullable=False, server_default=empty_object
        ),
        sa.Column("reason_codes_json", json_type, nullable=False, server_default=empty_array),
        sa.Column("target_relative_path_snapshot", sa.Text(), nullable=True),
        sa.Column("path_record_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["working_copy_id"], ["working_copies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["classification_run_id"], ["document_classification_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["primary_suggestion_id"], ["document_category_suggestions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["path_record_id"], ["working_copy_path_records.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_document_organization_decision_idempotency"),
    )
    for name, columns in (
        ("ix_document_organization_decisions_working_copy_id", ["working_copy_id"]),
        ("ix_document_organization_decisions_document_id", ["document_id"]),
        ("ix_document_organization_decisions_document_version_id", ["document_version_id"]),
        ("ix_document_organization_decisions_classification_run_id", ["classification_run_id"]),
        ("ix_document_organization_decisions_primary_suggestion_id", ["primary_suggestion_id"]),
        ("ix_document_organization_decisions_category_id", ["category_id"]),
        ("ix_document_organization_decisions_decision", ["decision"]),
        ("ix_document_organization_decisions_path_record_id", ["path_record_id"]),
        ("ix_document_organization_decisions_idempotency_key", ["idempotency_key"]),
    ):
        op.create_index(name, "document_organization_decisions", columns)


def downgrade() -> None:
    """移除组织决策，并恢复只有人工确认关系属于活动分类的旧约束。"""

    bind = op.get_bind()
    op.drop_table("document_organization_decisions")
    op.drop_index("uq_document_categories_active_primary", table_name="document_categories")
    op.drop_index("uq_document_categories_active_relation", table_name="document_categories")
    confirmed = sa.text("status = 'CONFIRMED'")
    confirmed_primary = sa.text("status = 'CONFIRMED' AND relation_role = 'PRIMARY'")
    dialect_where = (
        {"postgresql_where": confirmed}
        if bind.dialect.name == "postgresql"
        else {"sqlite_where": confirmed}
    )
    primary_dialect_where = (
        {"postgresql_where": confirmed_primary}
        if bind.dialect.name == "postgresql"
        else {"sqlite_where": confirmed_primary}
    )
    op.create_index(
        "uq_document_categories_active_relation",
        "document_categories",
        ["working_copy_id", "document_version_id", "category_id", "relation_role"],
        unique=True,
        **dialect_where,
    )
    op.create_index(
        "uq_document_categories_active_primary",
        "document_categories",
        ["working_copy_id", "document_version_id"],
        unique=True,
        **primary_dialect_where,
    )
