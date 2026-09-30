"""增加可恢复 Excel 发布任务和字段展示判定，不改写旧内容。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260922_0002"
down_revision = "20260922_0001"
branch_labels = None
depends_on = None


def upgrade():
    """旧字段默认无标记，任务正文仅在确认后持久化。"""
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column("structured_datasets", sa.Column("active_task_id", sa.String(36), nullable=True))
    op.add_column("structured_extraction_fields", sa.Column("presentation_json", json_type,
        nullable=False, server_default=sa.text("'{}'")))
    op.create_table("structured_dataset_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_key", sa.String(120), nullable=False),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("review_mode", sa.String(30), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        *(sa.Column(name, json_type, nullable=False, server_default=sa.text("'{}'"))
          for name in ("manifest_json", "payload_json", "progress_json", "result_json", "error_json")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "request_key", name="uq_dataset_task_request"))
    op.create_index("ix_structured_dataset_tasks_user_id", "structured_dataset_tasks", ["user_id"])


def downgrade():
    """仅移除任务和判定结构，不删除已发布文件或历史版本。"""
    op.drop_table("structured_dataset_tasks")
    op.drop_column("structured_extraction_fields", "presentation_json")
    op.drop_column("structured_datasets", "active_task_id")
