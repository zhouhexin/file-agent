"""回填旧累计表的用户可见目标文件名。

Revision ID: 20260920_0006
Revises: 20260920_0005
"""

from alembic import op
import sqlalchemy as sa


revision = "20260920_0006"
down_revision = "20260920_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """优先使用稳定工作副本名称，未发布的数据集再从业务名称生成 xlsx 名称。"""

    op.execute(
        sa.text(
            """
            UPDATE structured_datasets AS dataset
            SET target_filename = COALESCE(
                NULLIF(copy.filename, ''),
                CASE
                    WHEN lower(dataset.name) LIKE '%.xlsx' THEN dataset.name
                    ELSE dataset.name || '.xlsx'
                END
            )
            FROM working_copies AS copy
            WHERE dataset.working_copy_id = copy.id
              AND dataset.target_filename = ''
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE structured_datasets
            SET target_filename = CASE
                WHEN lower(name) LIKE '%.xlsx' THEN name
                ELSE name || '.xlsx'
            END
            WHERE target_filename = ''
            """
        )
    )


def downgrade() -> None:
    """回填结果是有效业务数据，降级不把已确定的用户文件名重新清空。"""
