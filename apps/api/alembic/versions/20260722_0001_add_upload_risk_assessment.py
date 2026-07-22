"""为上传归档保存基础文件风险检查结果。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260722_0001"
down_revision = "20260721_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增不包含正文的基础风险 JSON；病毒扫描状态只能记录为未实现。"""

    op.add_column(
        "upload_archive_records",
        sa.Column(
            "risk_assessment_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """移除风险结果字段，不删除上传原件或工作副本。"""

    op.drop_column("upload_archive_records", "risk_assessment_json")
