"""测试辅助工具。

这里集中创建隔离数据库客户端，避免每个测试文件重复覆盖 `get_db`。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.db.base import Base
from app.main import app


def client_with_database() -> tuple[TestClient, sessionmaker]:
    """创建带隔离 SQLite 内存库的 TestClient 和 Session 工厂。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        """为当前测试提供同一个内存数据库连接。"""

        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), TestingSessionLocal


def clear_overrides() -> None:
    """清理 FastAPI dependency override，避免测试之间相互污染。"""

    app.dependency_overrides.clear()
