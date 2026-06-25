"""数据库连接和会话管理。

业务代码必须通过 `get_db` 获取会话，不能在路由或 LangGraph 节点里直接创建连接。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.base import Base


settings = get_settings()
engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """为 FastAPI 请求提供数据库会话，并在请求结束时关闭连接。"""

    # 这是开发阶段的兜底：有些测试客户端不会触发 startup，首次请求前仍要保证表存在。
    init_database()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_database() -> None:
    """开发环境自动建表入口。

    当前服务数据库必须是 PostgreSQL；正常启动应通过 Alembic migration 管理表结构。
    """

    if settings.auto_create_tables:
        # 导入模型是为了把表注册到 Base.metadata；不能删除这个看似未使用的导入。
        from app.db import models  # noqa: F401

        Base.metadata.create_all(bind=engine)
