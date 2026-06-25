"""消息、AgentRun 和 ToolInvocation 持久化链路测试。

这些测试使用隔离 SQLite 临时库，只验证 ORM 和服务边界；生产目标数据库仍是 PostgreSQL。
"""

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.db.base import Base
from app.db.models import AgentRun, Message, ToolInvocation
from app.main import app


def _client_with_database() -> TestClient:
    """创建带隔离测试数据库的 TestClient，避免测试污染本地开发库。"""

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
    return TestClient(app)


def test_database_tables_can_be_created():
    """核心运行时表必须能通过 ORM metadata 创建。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(bind=engine)

    assert Message.__tablename__ == "messages"
    assert AgentRun.__tablename__ == "agent_runs"
    assert ToolInvocation.__tablename__ == "tool_invocations"


def test_post_message_persists_message_agent_run_and_tool_invocations():
    """发送消息后，message、agent_run 和 tool_invocation 必须全部入库。"""

    client = _client_with_database()

    response = client.post(
        "/api/conversations/conv-1/messages",
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": "doc-1"}],
        },
    )

    assert response.status_code == 200
    data = response.json()
    agent_run_id = data["agent_run"]["agent_run_id"]
    assert data["message"]["conversation_id"] == "conv-1"
    assert data["agent_run"]["status"] == "COMPLETED"
    assert len(data["agent_run"]["tool_invocations"]) == 4

    db = next(app.dependency_overrides[get_db]())
    try:
        assert db.query(Message).count() == 1
        assert db.query(AgentRun).count() == 1
        assert db.query(ToolInvocation).count() == 4
        stored_run = db.get(AgentRun, agent_run_id)
        assert stored_run is not None
        assert stored_run.plan_json["intent"] == "CLASSIFY_FILES"
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_agent_run_query_endpoints_return_persisted_records():
    """AgentRun 查询接口必须返回同一次持久化运行和对应 Tool 调用。"""

    client = _client_with_database()

    create_response = client.post(
        "/api/conversations/conv-1/messages",
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": "doc-1"}],
        },
    )
    agent_run_id = create_response.json()["agent_run"]["agent_run_id"]

    run_response = client.get(f"/api/agent-runs/{agent_run_id}")
    invocations_response = client.get(f"/api/agent-runs/{agent_run_id}/tool-invocations")

    assert run_response.status_code == 200
    assert run_response.json()["agent_run_id"] == agent_run_id
    assert run_response.json()["status"] == "COMPLETED"
    assert invocations_response.status_code == 200
    assert [
        item["tool_name"]
        for item in invocations_response.json()["tool_invocations"]
    ] == [
        "document-convert",
        "metadata-extract",
        "multi-label-classify",
        "change-report",
    ]
    app.dependency_overrides.clear()


def test_alembic_files_exist_for_runtime_tables():
    """迁移配置必须存在，避免 ORM 表只停留在测试内存里。"""

    assert Path("apps/api/alembic.ini").exists()
    assert Path("apps/api/alembic/env.py").exists()
    versions = list(Path("apps/api/alembic/versions").glob("*_create_runtime_tables.py"))
    assert len(versions) == 1
