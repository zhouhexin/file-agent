"""加固受管文件长路径存储。

长目录路径只作为展示和预览所需的相对路径保存，数据库唯一性改用固定长度 hash。
"""

from __future__ import annotations

import hashlib

from alembic import op
import sqlalchemy as sa


revision = "20260711_0002"
down_revision = "20260711_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """把长路径字段改为 TEXT，并用 relative_path_hash 承担唯一约束。"""

    connection = op.get_bind()
    inspector = sa.inspect(connection)
    unique_constraints = {item["name"] for item in inspector.get_unique_constraints("managed_files")}
    indexes = {item["name"] for item in inspector.get_indexes("managed_files")}
    columns = {item["name"] for item in inspector.get_columns("managed_files")}

    if "uq_managed_files_root_relative_path" in unique_constraints:
        op.drop_constraint("uq_managed_files_root_relative_path", "managed_files", type_="unique")

    if "relative_path_hash" not in columns:
        op.add_column("managed_files", sa.Column("relative_path_hash", sa.String(length=64), nullable=True))

    rows = connection.execute(sa.text("select id, relative_path from managed_files")).mappings()
    for row in rows:
        relative_path = str(row["relative_path"] or "")
        connection.execute(
            sa.text("update managed_files set relative_path_hash = :relative_path_hash where id = :id"),
            {
                "id": row["id"],
                "relative_path_hash": hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
            },
        )

    op.alter_column("managed_files", "relative_path", existing_type=sa.String(length=1000), type_=sa.Text(), existing_nullable=False)
    op.alter_column("managed_files", "category_path", existing_type=sa.String(length=1000), type_=sa.Text(), existing_nullable=True)
    op.alter_column("managed_files", "filename", existing_type=sa.String(length=255), type_=sa.Text(), existing_nullable=False)
    op.alter_column("managed_files", "fingerprint", existing_type=sa.String(length=64), type_=sa.String(length=64), existing_nullable=False)

    if "ix_managed_files_relative_path_hash" not in indexes:
        op.create_index("ix_managed_files_relative_path_hash", "managed_files", ["relative_path_hash"])
    if "uq_managed_files_root_relative_path_hash" not in unique_constraints:
        op.create_unique_constraint(
            "uq_managed_files_root_relative_path_hash",
            "managed_files",
            ["root_id", "relative_path_hash"],
        )


def downgrade() -> None:
    """回退到旧的相对路径唯一约束；超长路径数据需要业务侧先清理。"""

    inspector = sa.inspect(op.get_bind())
    unique_constraints = {item["name"] for item in inspector.get_unique_constraints("managed_files")}
    indexes = {item["name"] for item in inspector.get_indexes("managed_files")}

    if "uq_managed_files_root_relative_path_hash" in unique_constraints:
        op.drop_constraint("uq_managed_files_root_relative_path_hash", "managed_files", type_="unique")
    if "ix_managed_files_relative_path_hash" in indexes:
        op.drop_index("ix_managed_files_relative_path_hash", table_name="managed_files")

    op.alter_column("managed_files", "filename", existing_type=sa.Text(), type_=sa.String(length=255), existing_nullable=False)
    op.alter_column("managed_files", "category_path", existing_type=sa.Text(), type_=sa.String(length=1000), existing_nullable=True)
    op.alter_column("managed_files", "relative_path", existing_type=sa.Text(), type_=sa.String(length=1000), existing_nullable=False)
    op.drop_column("managed_files", "relative_path_hash")
    if "uq_managed_files_root_relative_path" not in unique_constraints:
        op.create_unique_constraint("uq_managed_files_root_relative_path", "managed_files", ["root_id", "relative_path"])
