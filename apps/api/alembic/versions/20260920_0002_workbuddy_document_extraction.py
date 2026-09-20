"""增加 WorkBuddy 完整文档提取策略和任务摘要。

Revision ID: 20260920_0002
Revises: 20260920_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260920_0002"
down_revision = "20260920_0001"
branch_labels = None
depends_on = None


def upgrade():
    """新增默认空 JSON，旧文件继续走其既有解析和 OCR 行为。"""

    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "document_versions",
        sa.Column("extraction_policy_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "external_extraction_tasks",
        sa.Column("source_manifest_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "external_extraction_tasks",
        sa.Column("result_summary_json", json_type, nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade():
    """回退仅移除新字段；发布前应先处理仍等待的文档提取任务。"""

    op.drop_column("external_extraction_tasks", "result_summary_json")
    op.drop_column("external_extraction_tasks", "source_manifest_json")
    op.drop_column("document_versions", "extraction_policy_json")
