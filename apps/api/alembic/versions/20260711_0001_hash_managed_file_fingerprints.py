"""将受管文件 fingerprint 收敛为固定长度哈希。"""

from __future__ import annotations

import hashlib

from alembic import op
import sqlalchemy as sa


revision = "20260711_0001"
down_revision = "20260707_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展线上兼容性，并把历史可变长 fingerprint 转成 SHA-256。"""

    connection = op.get_bind()
    rows = connection.execute(sa.text("select id, fingerprint from managed_files")).mappings()
    for row in rows:
        value = str(row["fingerprint"] or "")
        if len(value) == 64 and _is_hex(value):
            continue
        connection.execute(
            sa.text("update managed_files set fingerprint = :fingerprint where id = :id"),
            {"id": row["id"], "fingerprint": hashlib.sha256(value.encode("utf-8")).hexdigest()},
        )
    op.alter_column(
        "managed_files",
        "fingerprint",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    """回退字段长度；哈希后的内容保持不变。"""

    op.alter_column(
        "managed_files",
        "fingerprint",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=False,
    )


def _is_hex(value: str) -> bool:
    """判断字符串是否已经是十六进制摘要。"""

    try:
        int(value, 16)
    except ValueError:
        return False
    return True
