"""为复杂 Excel 模板保存表头和填写区域，不改动既有文件。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260922_0001"
down_revision = "20260920_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """已有表保留空布局，继续使用兼容写入策略。"""
    op.add_column("structured_datasets", sa.Column(
        "target_layout_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False, server_default=sa.text("'{}'")))
    op.alter_column("structured_extraction_fields", "field_label", existing_type=sa.String(80), type_=sa.Text())


def downgrade() -> None:
    """只移除布局元数据，不删除用户工作副本或历史版本。"""
    op.drop_column("structured_datasets", "target_layout_json")
    # 保留 Text，避免降级时截断已经保存的用户长表头。
