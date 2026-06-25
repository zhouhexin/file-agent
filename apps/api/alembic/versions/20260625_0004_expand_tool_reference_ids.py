"""扩展 ToolInvocation 引用 ID 字段长度。

Revision ID: 20260625_0004
Revises: 20260625_0003
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0004"
down_revision = "20260625_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """支持 changeset-<uuid> 这类真实文件处理引用。"""

    op.alter_column("tool_invocations", "changeset_id", type_=sa.String(length=100), existing_nullable=True)
    op.alter_column("tool_invocations", "operation_plan_id", type_=sa.String(length=100), existing_nullable=True)


def downgrade() -> None:
    """回滚 ToolInvocation 引用 ID 字段长度。"""

    op.alter_column("tool_invocations", "operation_plan_id", type_=sa.String(length=36), existing_nullable=True)
    op.alter_column("tool_invocations", "changeset_id", type_=sa.String(length=36), existing_nullable=True)
