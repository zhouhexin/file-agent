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
from app.db.models import AgentRun, DocumentExtractionRun, DocumentPage, Message, ToolInvocation, User
from app.main import app
from app.modules.agent.service import AgentRuntimeService
from app.modules.conversations.schemas import MessageAttachment, SendMessageRequest
from app.modules.conversations.service import ConversationMessageService
from app.modules.llm.schemas import UserIntentPlan


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


def _auth_header(client: TestClient, username: str = "persist-user") -> dict[str, str]:
    """注册并登录测试用户，返回 Authorization header。"""

    client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    login_response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {login_response.json()['access_token']}"}


def _upload_document(client: TestClient, headers: dict[str, str], filename: str = "persist.txt") -> str:
    """上传测试文件并返回 document_id。"""

    response = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": (filename, b"persist-file", "text/plain")},
    )
    return response.json()["document_id"]


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
    assert ToolInvocation.__table__.c.changeset_id.type.length >= 100


def test_post_message_persists_message_agent_run_and_tool_invocations():
    """发送消息后，message、agent_run 和 tool_invocation 必须全部入库。"""

    client = _client_with_database()
    headers = _auth_header(client)
    document_id = _upload_document(client, headers)

    response = client.post(
        "/api/conversations/conv-1/messages",
        headers=headers,
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": document_id}],
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
    headers = _auth_header(client, username="persist-query-user")
    document_id = _upload_document(client, headers, filename="persist-query.txt")

    create_response = client.post(
        "/api/conversations/conv-1/messages",
        headers=headers,
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": document_id}],
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


def test_llm_message_reuses_persisted_document_insights():
    """LLM 对话阶段应复用上传阶段已持久化的 document_insights。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回读取文件洞察的计划。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回依赖已上传文件洞察的用户意图。"""

            return UserIntentPlan(
                intent="SUMMARIZE_DOCUMENTS",
                user_goal=message,
                needs_file_context=True,
                referenced_document_ids=[attachments[0]["document_id"]],
                required_capabilities=["read_document_insights"],
                skip_completed_ingest=True,
                tool_plan_hint=["read-document-insights"],
                response_style="concise",
            )

    client = _client_with_database()
    headers = _auth_header(client, username="llm-insight-user")
    document_id = _upload_document(client, headers, filename="student.txt")

    db = next(app.dependency_overrides[get_db]())
    try:
        user = db.query(User).filter(User.username == "llm-insight-user").one()
        response = ConversationMessageService(
            db=db,
            agent_service=AgentRuntimeService(llm_intent_service=FakeLLMIntentService()),
        ).send_user_message(
            conversation_id="llm-conv",
            user_id=user.id,
            request=SendMessageRequest(
                content="总结我刚才上传的文件",
                attachments=[MessageAttachment(document_id=document_id)],
            ),
        )

        assert response.agent_run.intent == "SUMMARIZE_DOCUMENTS"
        assert [item.tool_name for item in response.agent_run.tool_invocations] == ["read-document-insights"]
        assert response.agent_run.tool_results[0]["documents"][0]["document_id"] == document_id
        assert response.agent_run.tool_results[0]["documents"][0]["ingest_status"] == "INGESTED"
        assert db.query(ToolInvocation).one().tool_name == "read-document-insights"
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_llm_message_extracts_document_text_and_persists_pages():
    """对话触发 extract-document-text 时，必须解析文件并写入 document_pages。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回读取文件正文的计划。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回依赖原始文件正文解析的用户意图。"""

            return UserIntentPlan(
                intent="EXTRACT_DOCUMENT_TEXT",
                user_goal=message,
                needs_file_context=True,
                referenced_document_ids=[attachments[0]["document_id"]],
                required_capabilities=["extract_document_text"],
                skip_completed_ingest=True,
                tool_plan_hint=["extract-document-text"],
                response_style="concise",
            )

    client = _client_with_database()
    headers = _auth_header(client, username="llm-extract-user")
    document_id = _upload_document(client, headers, filename="extract.txt")

    db = next(app.dependency_overrides[get_db]())
    try:
        user = db.query(User).filter(User.username == "llm-extract-user").one()
        response = ConversationMessageService(
            db=db,
            agent_service=AgentRuntimeService(llm_intent_service=FakeLLMIntentService()),
        ).send_user_message(
            conversation_id="extract-conv",
            user_id=user.id,
            request=SendMessageRequest(
                content="读取这个文件内容",
                attachments=[MessageAttachment(document_id=document_id)],
            ),
        )

        assert response.agent_run.intent == "EXTRACT_DOCUMENT_TEXT"
        assert [item.tool_name for item in response.agent_run.tool_invocations] == ["extract-document-text"]
        assert response.agent_run.tool_results[0]["status"] == "COMPLETED"
        assert "已解析" in (response.agent_run.final_response or "")
        assert db.query(DocumentExtractionRun).count() == 1
        page = db.query(DocumentPage).one()
        assert page.document_id == document_id
        assert "persist-file" in page.text_content
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_alembic_files_exist_for_runtime_tables():
    """迁移配置必须存在，避免 ORM 表只停留在测试内存里。"""

    assert Path("apps/api/alembic.ini").exists()
    assert Path("apps/api/alembic/env.py").exists()
    versions = list(Path("apps/api/alembic/versions").glob("*_create_runtime_tables.py"))
    assert len(versions) == 1
