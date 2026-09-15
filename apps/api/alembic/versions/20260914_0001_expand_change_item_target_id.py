"""扩大 ChangeItem 多态目标标识的长度。

Revision ID: 20260914_0001
Revises: 20260910_0001
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260914_0001"
down_revision = "20260910_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """允许 target_id 保存 taxonomy 稳定 ID 等非 UUID 审计定位符。"""

    op.alter_column(
        "change_items",
        "target_id",
        existing_type=sa.String(length=36),
        type_=sa.String(length=255),
        existing_nullable=True,
    )


def downgrade() -> None:
    """回退到旧长度；存在超长 target_id 时数据库会拒绝有损回退。"""

    op.alter_column(
        "change_items",
        "target_id",
        existing_type=sa.String(length=255),
        type_=sa.String(length=36),
        existing_nullable=True,
    )
