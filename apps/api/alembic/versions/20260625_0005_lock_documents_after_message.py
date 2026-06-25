"""增加 Document 对话锁定字段。

Revision ID: 20260625_0005
Revises: 20260625_0004
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0005"
down_revision = "20260625_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """记录文件进入对话后的锁定位置。"""

    op.add_column("documents", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("locked_message_id", sa.String(length=36), nullable=True))
    op.add_column("documents", sa.Column("locked_conversation_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    """回滚 Document 锁定字段。"""

    op.drop_column("documents", "locked_conversation_id")
    op.drop_column("documents", "locked_message_id")
    op.drop_column("documents", "locked_at")
