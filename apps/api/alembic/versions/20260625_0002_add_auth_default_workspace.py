"""增加认证默认工作区字段。

Revision ID: 20260625_0002
Revises: 20260625_0001
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260625_0002"
down_revision = "20260625_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为 users 增加 default_workspace_id，并补充 workspace owner 索引。"""

    op.add_column("users", sa.Column("default_workspace_id", sa.String(length=36), nullable=True))
    op.create_index("ix_workspaces_owner_id", "workspaces", ["owner_id"])


def downgrade() -> None:
    """回滚 default workspace 字段和索引。"""

    op.drop_index("ix_workspaces_owner_id", table_name="workspaces")
    op.drop_column("users", "default_workspace_id")
