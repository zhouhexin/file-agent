"""创建共享工作目录的系统工作区标识。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260724_0003"
down_revision = "20260724_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为 Workspace 增加系统类型和稳定键，不迁移旧工作副本数据。

    本阶段配套的开发重置会删除旧的按用户副本；迁移本身不擅自重写生产文件。
    """

    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.add_column(sa.Column("workspace_type", sa.String(length=30), nullable=False, server_default="USER"))
        batch_op.add_column(sa.Column("system_key", sa.String(length=100), nullable=True))
        batch_op.create_unique_constraint("uq_workspaces_system_key", ["system_key"])
        batch_op.create_index("ix_workspaces_system_key", ["system_key"], unique=False)
    op.alter_column("workspaces", "workspace_type", server_default=None)


def downgrade() -> None:
    """移除共享工作区标识字段。"""

    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.drop_index("ix_workspaces_system_key")
        batch_op.drop_constraint("uq_workspaces_system_key", type_="unique")
        batch_op.drop_column("system_key")
        batch_op.drop_column("workspace_type")
