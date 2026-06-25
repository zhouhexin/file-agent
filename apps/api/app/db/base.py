"""SQLAlchemy ORM 基类。

所有 ORM 模型必须挂在同一个 Base 上，Alembic 才能读取完整 metadata。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """项目统一 ORM 基类。"""

    pass
