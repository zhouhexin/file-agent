"""为已发布累计 Excel 建立不可变逐格更正记录。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260924_0002"
down_revision = "20260924_0001"
branch_labels = None
depends_on = None


def upgrade():
    """更正和同一数据集的新 revision 一一对应，不修改旧成员事实。"""
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "structured_dataset_corrections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_id", sa.String(36), sa.ForeignKey("structured_datasets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("structured_dataset_tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("record_id", sa.String(100), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("dataset_revision", sa.Integer(), nullable=False),
        sa.Column("changes_json", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dataset_id", "dataset_revision", name="uq_dataset_correction_revision"),
        sa.UniqueConstraint("task_id", name="uq_dataset_correction_task"),
    )
    op.create_index("ix_structured_dataset_corrections_dataset_id", "structured_dataset_corrections", ["dataset_id"])
    op.create_index("ix_structured_dataset_corrections_record_id", "structured_dataset_corrections", ["record_id"])


def downgrade():
    """仅回退结构；已有更正须先另行保全审计与版本。"""
    op.drop_table("structured_dataset_corrections")
