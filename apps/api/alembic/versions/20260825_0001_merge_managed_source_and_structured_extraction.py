"""合并受管源侧索引与图片结构化抽取两条迁移分支。

Revision ID: 20260825_0001
Revises: 20260813_0001, 20260824_0001
"""


revision = "20260825_0001"
down_revision = ("20260813_0001", "20260824_0001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """合并迁移图；两侧功能迁移已经各自负责全部 DDL。"""

    # merge revision 只消除并行 head，不能重复创建任一分支的表或索引。
    pass


def downgrade() -> None:
    """拆分回两个迁移头；具体 DDL 仍由各父迁移负责回滚。"""

    pass
