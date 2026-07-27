"""补齐阶段五证据回答缓存、模型审计和工作副本引用字段。

Revision ID: 20260727_0001
Revises: 20260725_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260727_0001"
down_revision = "20260725_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为回答增加可失效缓存指纹，并把引用绑定到回答时的活动工作副本。"""

    op.add_column(
        "qa_answers",
        sa.Column("answer_mode", sa.String(length=40), nullable=False, server_default="FOCUSED"),
    )
    op.add_column(
        "qa_answers",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "qa_answers",
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "qa_answers",
        sa.Column("prompt_version", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column(
        "qa_answers",
        sa.Column("schema_version", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column(
        "qa_answers",
        sa.Column("provider", sa.String(length=80), nullable=False, server_default="disabled"),
    )
    op.add_column(
        "qa_answers",
        sa.Column("model_name", sa.String(length=160), nullable=False, server_default=""),
    )
    op.add_column(
        "qa_answers",
        sa.Column(
            "usage_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index("ix_qa_answers_answer_mode", "qa_answers", ["answer_mode"])
    op.create_index("ix_qa_answers_request_fingerprint", "qa_answers", ["request_fingerprint"])
    op.create_index("ix_qa_answers_evidence_fingerprint", "qa_answers", ["evidence_fingerprint"])

    op.add_column(
        "answer_references",
        sa.Column("working_copy_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_answer_references_working_copy_id",
        "answer_references",
        "working_copies",
        ["working_copy_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_answer_references_working_copy_id",
        "answer_references",
        ["working_copy_id"],
    )
    # 历史阶段三数据没有工作副本引用；只回填仍为活动版本的记录。无法回填的旧引用继续
    # 保持 NULL，但阶段五服务不会读取或复用这些引用。
    op.execute(
        """
        UPDATE answer_references AS ar
        SET working_copy_id = wc.id
        FROM working_copies AS wc
        WHERE wc.document_id = ar.document_id
          AND wc.current_version_id = ar.document_version_id
          AND wc.status = 'ACTIVE'
          AND ar.working_copy_id IS NULL
        """
    )


def downgrade() -> None:
    """移除阶段五补充字段，保留阶段三原始回答与引用表。"""

    op.drop_index("ix_answer_references_working_copy_id", table_name="answer_references")
    op.drop_constraint(
        "fk_answer_references_working_copy_id",
        "answer_references",
        type_="foreignkey",
    )
    op.drop_column("answer_references", "working_copy_id")
    op.drop_index("ix_qa_answers_evidence_fingerprint", table_name="qa_answers")
    op.drop_index("ix_qa_answers_request_fingerprint", table_name="qa_answers")
    op.drop_index("ix_qa_answers_answer_mode", table_name="qa_answers")
    op.drop_column("qa_answers", "usage_json")
    op.drop_column("qa_answers", "model_name")
    op.drop_column("qa_answers", "provider")
    op.drop_column("qa_answers", "schema_version")
    op.drop_column("qa_answers", "prompt_version")
    op.drop_column("qa_answers", "evidence_fingerprint")
    op.drop_column("qa_answers", "request_fingerprint")
    op.drop_column("qa_answers", "answer_mode")
