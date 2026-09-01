"""为异步任务增加单次执行令牌。

Revision ID: 20260831_0001
Revises: 20260827_0001
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_0001"
down_revision = "20260827_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加执行令牌，阻止超时旧进程迟到提交。"""

    op.add_column("filesystem_jobs", sa.Column("execution_token", sa.String(length=36), nullable=True))
    op.create_index(
        "ix_filesystem_jobs_execution_token",
        "filesystem_jobs",
        ["execution_token"],
        unique=False,
    )


def downgrade() -> None:
    """移除执行令牌。"""

    op.drop_index("ix_filesystem_jobs_execution_token", table_name="filesystem_jobs")
    op.drop_column("filesystem_jobs", "execution_token")
