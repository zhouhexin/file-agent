"""补充外部批次首次整理的授权和修订审计字段。

Revision ID: 20260908_0004
Revises: 20260908_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20260908_0004"
down_revision = "20260908_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为组织决策记录授权来源、外部请求和执行前后工作副本修订。"""

    op.add_column(
        "document_organization_decisions",
        sa.Column(
            "authorization_source",
            sa.String(length=80),
            nullable=False,
            server_default="LEGACY_CONFIGURATION",
        ),
    )
    op.add_column(
        "document_organization_decisions",
        sa.Column("source_request_id", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "document_organization_decisions",
        sa.Column("before_revision", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "document_organization_decisions",
        sa.Column("after_revision", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_document_organization_decisions_authorization_source",
        "document_organization_decisions",
        ["authorization_source"],
    )
    op.create_index(
        "ix_document_organization_decisions_source_request_id",
        "document_organization_decisions",
        ["source_request_id"],
    )


def downgrade() -> None:
    """移除新增审计字段，不回滚已完成的文件整理结果。"""

    op.drop_index(
        "ix_document_organization_decisions_source_request_id",
        table_name="document_organization_decisions",
    )
    op.drop_index(
        "ix_document_organization_decisions_authorization_source",
        table_name="document_organization_decisions",
    )
    op.drop_column("document_organization_decisions", "after_revision")
    op.drop_column("document_organization_decisions", "before_revision")
    op.drop_column("document_organization_decisions", "source_request_id")
    op.drop_column("document_organization_decisions", "authorization_source")
