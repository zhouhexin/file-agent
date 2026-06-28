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
from app.db.models import (
    AgentRun,
    ChangeItem,
    ChangeSet,
    DocumentCategoryFeedback,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentExtractionRun,
    DocumentPage,
    Message,
    ToolInvocation,
    User,
)
from app.main import app
from app.modules.agent.service import AgentRuntimeService
from app.modules.classification.service import persist_document_results_classifications
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


def _upload_document(
    client: TestClient,
    headers: dict[str, str],
    filename: str = "persist.txt",
    content: bytes = b"persist-file",
    content_type: str = "text/plain",
) -> str:
    """上传测试文件并返回 document_id。"""

    response = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": (filename, content, content_type)},
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
    assert ChangeSet.__tablename__ == "change_sets"
    assert ChangeItem.__tablename__ == "change_items"
    assert DocumentClassificationRun.__tablename__ == "document_classification_runs"
    assert DocumentCategorySuggestion.__tablename__ == "document_category_suggestions"
    assert DocumentCategoryFeedback.__tablename__ == "document_category_feedback"
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
            "content": "帮我分类这批文件",
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
            "content": "帮我分类这批文件",
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
    document_id = _upload_document(
        client,
        headers,
        filename="extract.txt",
        content="本文件涉及教师职称申报材料。".encode(),
    )

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
        assert "extract.txt" in (response.agent_run.final_response or "")
        assert "学校/人事师资/职称" in (response.agent_run.final_response or "")
        assert db.query(DocumentExtractionRun).count() == 1
        page = db.query(DocumentPage).one()
        assert page.document_id == document_id
        assert "职称申报" in page.text_content
        stored_run = db.query(AgentRun).one()
        document_results = stored_run.graph_state_json["document_results"]
        assert document_results[0]["document_id"] == document_id
        assert document_results[0]["extraction_status"] == "COMPLETED"
        assert document_results[0]["categories"][0]["name"] == "学校/人事师资/职称"
        assert document_results[0]["categories"][0]["taxonomy_key"] == "school_file_classification"
        classification_run = db.query(DocumentClassificationRun).one()
        assert classification_run.agent_run_id == stored_run.id
        assert classification_run.document_id == document_id
        assert classification_run.status == "COMPLETED"
        suggestion_names = [item.category_name for item in db.query(DocumentCategorySuggestion).all()]
        assert "学校/人事师资/职称" in suggestion_names
        suggestion = (
            db.query(DocumentCategorySuggestion)
            .filter(DocumentCategorySuggestion.category_name == "学校/人事师资/职称")
            .one()
        )
        assert suggestion.status == "SUGGESTED"
        assert suggestion.source == "rule"
        assert suggestion.evidence_json == ["职称"]
        assert response.agent_run.changeset_id
        assert stored_run.changeset_id == response.agent_run.changeset_id
        changeset = db.query(ChangeSet).one()
        assert changeset.agent_run_id == stored_run.id
        assert changeset.summary == f"已处理 1 个文件，生成 {db.query(ChangeItem).count()} 项变更记录。"
        change_types = [item.change_type for item in db.query(ChangeItem).order_by(ChangeItem.created_at.asc()).all()]
        assert change_types[:2] == ["TEXT_EXTRACTED", "DOCUMENT_PAGES_CREATED"]
        assert change_types.count("CATEGORY_SUGGESTED") >= 1
        category_item = next(
            item
            for item in db.query(ChangeItem).filter(ChangeItem.change_type == "CATEGORY_SUGGESTED").all()
            if item.after_value_json["category_name"] == "学校/人事师资/职称"
        )
        assert category_item.target_document_id == document_id
        assert category_item.after_value_json["category_name"] == "学校/人事师资/职称"
        assert category_item.evidence_json["evidence"] == ["职称"]
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_llm_message_extracts_multiple_documents_and_builds_document_results():
    """多附件对话必须逐文件解析、分类并持久化 document_results。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回多文件正文解析计划。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回全部附件，保护批量文件链路。"""

            return UserIntentPlan(
                intent="EXTRACT_DOCUMENT_TEXT",
                user_goal=message,
                needs_file_context=True,
                referenced_document_ids=[item["document_id"] for item in attachments],
                required_capabilities=["extract_document_text"],
                skip_completed_ingest=True,
                tool_plan_hint=["extract-document-text"],
                response_style="concise",
            )

    client = _client_with_database()
    headers = _auth_header(client, username="llm-batch-extract-user")
    staff_document_id = _upload_document(
        client,
        headers,
        filename="staff.txt",
        content="本文件涉及学校教师职称、干部工作和会议纪要材料。".encode(),
    )
    plan_document_id = _upload_document(
        client,
        headers,
        filename="plan.txt",
        content="本文件是学院年度计划、总结材料。".encode(),
    )
    unknown_document_id = _upload_document(
        client,
        headers,
        filename="unknown.txt",
        content="这是一段无法判断归类的普通文本。".encode(),
    )

    db = next(app.dependency_overrides[get_db]())
    try:
        user = db.query(User).filter(User.username == "llm-batch-extract-user").one()
        response = ConversationMessageService(
            db=db,
            agent_service=AgentRuntimeService(llm_intent_service=FakeLLMIntentService()),
        ).send_user_message(
            conversation_id="batch-extract-conv",
            user_id=user.id,
            request=SendMessageRequest(
                content="读取并分类这批文件",
                attachments=[
                    MessageAttachment(document_id=staff_document_id),
                    MessageAttachment(document_id=plan_document_id),
                    MessageAttachment(document_id=unknown_document_id),
                ],
            ),
        )

        assert response.agent_run.intent == "EXTRACT_DOCUMENT_TEXT"
        assert [item.tool_name for item in response.agent_run.tool_invocations] == [
            "extract-document-text",
            "extract-document-text",
            "extract-document-text",
        ]
        assert db.query(DocumentExtractionRun).count() == 3
        assert db.query(DocumentPage).count() == 3
        final_response = response.agent_run.final_response or ""
        assert "staff.txt" in final_response
        assert "plan.txt" in final_response
        assert "unknown.txt" in final_response
        assert "学校/人事师资/职称" in final_response
        assert "学校/党委相关/干部工作" in final_response
        assert "置信度" in final_response
        assert "学院/行政管理/年度计划、总结" in final_response
        assert "其他（暂无明确关键词依据）" in final_response

        stored_run = db.query(AgentRun).one()
        document_results = stored_run.graph_state_json["document_results"]
        assert [item["document_id"] for item in document_results] == [
            staff_document_id,
            plan_document_id,
            unknown_document_id,
        ]
        staff_category_names = [category["name"] for category in document_results[0]["categories"]]
        staff_confidences = [category["confidence"] for category in document_results[0]["categories"]]
        assert "学校/党委相关/干部工作" in staff_category_names
        assert "学校/行政综合管理类/会议纪要" in staff_category_names
        assert "学校/人事师资/职称" in staff_category_names
        assert staff_confidences == sorted(staff_confidences, reverse=True)
        assert document_results[1]["categories"][0]["name"] == "学院/行政管理/年度计划、总结"
        assert document_results[2]["categories"][0]["name"] == "其他"
        assert db.query(DocumentClassificationRun).count() == 3
        suggestion_names = [
            item.category_name
            for item in db.query(DocumentCategorySuggestion)
            .order_by(DocumentCategorySuggestion.document_id.asc(), DocumentCategorySuggestion.rank.asc())
            .all()
        ]
        assert "学校/人事师资/职称" in suggestion_names
        assert "学院/行政管理/年度计划、总结" in suggestion_names
        assert "其他" in suggestion_names
        assert response.agent_run.changeset_id
        assert db.query(ChangeSet).count() == 1
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "TEXT_EXTRACTED").count() == 3
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "DOCUMENT_PAGES_CREATED").count() == 3
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "CATEGORY_SUGGESTED").count() >= 3
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_get_changeset_returns_items_for_owner():
    """当前用户可以查询自己 AgentRun 生成的 ChangeSet 明细。"""

    client = _client_with_database()
    headers = _auth_header(client, username="changeset-owner-user")
    document_id = _upload_document(
        client,
        headers,
        filename="changeset.txt",
        content="本文件涉及教师职称申报材料。".encode(),
    )

    response = client.post(
        "/api/conversations/changeset-conv/messages",
        headers=headers,
        json={
            "content": "帮我读取并分类这个文件",
            "attachments": [{"document_id": document_id}],
        },
    )

    assert response.status_code == 200
    changeset_id = response.json()["agent_run"]["changeset_id"]
    detail_response = client.get(f"/api/changesets/{changeset_id}", headers=headers)

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["id"] == changeset_id
    assert detail["status"] == "COMPLETED"
    assert detail["summary"] == f"已处理 1 个文件，生成 {len(detail['items'])} 项变更记录。"
    assert [item["change_type"] for item in detail["items"][:2]] == [
        "TEXT_EXTRACTED",
        "DOCUMENT_PAGES_CREATED",
    ]
    assert [item["change_type"] for item in detail["items"]].count("CATEGORY_SUGGESTED") >= 1
    assert detail["items"][2]["target_document_id"] == document_id


def test_get_changeset_rejects_other_user():
    """用户不能越权读取其他用户的 ChangeSet。"""

    client = _client_with_database()
    owner_headers = _auth_header(client, username="changeset-private-owner")
    other_headers = _auth_header(client, username="changeset-private-other")
    document_id = _upload_document(
        client,
        owner_headers,
        filename="private-changeset.txt",
        content="本文件涉及教师职称申报材料。".encode(),
    )
    response = client.post(
        "/api/conversations/private-changeset-conv/messages",
        headers=owner_headers,
        json={
            "content": "帮我读取并分类这个文件",
            "attachments": [{"document_id": document_id}],
        },
    )
    changeset_id = response.json()["agent_run"]["changeset_id"]

    detail_response = client.get(f"/api/changesets/{changeset_id}", headers=other_headers)

    assert detail_response.status_code == 404


def test_changeset_records_success_and_failed_documents_in_one_run():
    """批量解析部分失败时，ChangeSet 必须同时记录成功文件和失败文件。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回全部附件正文解析计划。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回全部附件，保护部分失败的批量链路。"""

            return UserIntentPlan(
                intent="EXTRACT_DOCUMENT_TEXT",
                user_goal=message,
                needs_file_context=True,
                referenced_document_ids=[item["document_id"] for item in attachments],
                required_capabilities=["extract_document_text"],
                skip_completed_ingest=True,
                tool_plan_hint=["extract-document-text"],
                response_style="concise",
            )

    client = _client_with_database()
    headers = _auth_header(client, username="changeset-partial-failure-user")
    good_document_id = _upload_document(
        client,
        headers,
        filename="partial-good.txt",
        content="本文件涉及教师职称申报材料。".encode(),
    )
    bad_document_id = _upload_document(
        client,
        headers,
        filename="partial-bad.bin",
        content=b"\x00\x01",
        content_type="application/octet-stream",
    )

    db = next(app.dependency_overrides[get_db]())
    try:
        user = db.query(User).filter(User.username == "changeset-partial-failure-user").one()
        response = ConversationMessageService(
            db=db,
            agent_service=AgentRuntimeService(llm_intent_service=FakeLLMIntentService()),
        ).send_user_message(
            conversation_id="changeset-partial-failure-conv",
            user_id=user.id,
            request=SendMessageRequest(
                content="帮我读取并分类这批文件",
                attachments=[
                    MessageAttachment(document_id=good_document_id),
                    MessageAttachment(document_id=bad_document_id),
                ],
            ),
        )

        assert response.agent_run.changeset_id
        failed_item = (
            db.query(ChangeItem)
            .filter(ChangeItem.change_type == "DOCUMENT_PROCESSING_FAILED")
            .one()
        )
        assert failed_item.target_document_id == bad_document_id
        assert failed_item.execution_status == "FAILED"
        assert "暂不支持解析该文件类型" in failed_item.after_value_json["errors"][0]["message"]
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "TEXT_EXTRACTED").count() == 1
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "CATEGORY_SUGGESTED").count() >= 1
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_extract_document_text_reuses_existing_successful_pages_by_default():
    """同一文件重复读取时应复用成功解析结果，避免重复写 document_pages。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回单文件正文解析计划。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回读取正文意图，默认不要求重处理。"""

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
    headers = _auth_header(client, username="reuse-extraction-user")
    document_id = _upload_document(
        client,
        headers,
        filename="reuse.txt",
        content="本文件涉及教师职称申报材料。".encode(),
    )

    db = next(app.dependency_overrides[get_db]())
    try:
        user = db.query(User).filter(User.username == "reuse-extraction-user").one()
        service = ConversationMessageService(
            db=db,
            agent_service=AgentRuntimeService(llm_intent_service=FakeLLMIntentService()),
        )
        first_response = service.send_user_message(
            conversation_id="reuse-extraction-conv",
            user_id=user.id,
            request=SendMessageRequest(
                content="读取这个文件内容",
                attachments=[MessageAttachment(document_id=document_id)],
            ),
        )
        second_response = service.send_user_message(
            conversation_id="reuse-extraction-conv",
            user_id=user.id,
            request=SendMessageRequest(
                content="再读取这个文件内容",
                attachments=[MessageAttachment(document_id=document_id)],
            ),
        )

        assert first_response.agent_run.tool_results[0]["reused"] is False
        assert second_response.agent_run.tool_results[0]["reused"] is True
        assert db.query(DocumentExtractionRun).count() == 1
        assert db.query(DocumentPage).count() == 1
        second_changeset_id = second_response.agent_run.changeset_id
        reused_types = [
            item.change_type
            for item in db.query(ChangeItem).filter(ChangeItem.changeset_id == second_changeset_id).all()
        ]
        assert "TEXT_REUSED" in reused_types
        assert "DOCUMENT_PAGES_REUSED" in reused_types
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_extract_document_text_can_force_reprocess_when_user_requests_again():
    """用户明确要求重新解析时，应跳过复用并创建新的解析运行。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，把用户原文放入计划，便于识别重新解析。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回读取正文意图，消息中的重新解析由 Planner 转成 force_reprocess。"""

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
    headers = _auth_header(client, username="force-reprocess-user")
    document_id = _upload_document(
        client,
        headers,
        filename="force-reprocess.txt",
        content="本文件涉及教师职称申报材料。".encode(),
    )

    db = next(app.dependency_overrides[get_db]())
    try:
        user = db.query(User).filter(User.username == "force-reprocess-user").one()
        service = ConversationMessageService(
            db=db,
            agent_service=AgentRuntimeService(llm_intent_service=FakeLLMIntentService()),
        )
        service.send_user_message(
            conversation_id="force-reprocess-conv",
            user_id=user.id,
            request=SendMessageRequest(
                content="读取这个文件内容",
                attachments=[MessageAttachment(document_id=document_id)],
            ),
        )
        second_response = service.send_user_message(
            conversation_id="force-reprocess-conv",
            user_id=user.id,
            request=SendMessageRequest(
                content="重新解析这个文件内容",
                attachments=[MessageAttachment(document_id=document_id)],
            ),
        )

        assert second_response.agent_run.tool_results[0]["reused"] is False
        assert db.query(DocumentExtractionRun).count() == 2
        assert db.query(DocumentPage).count() == 2
        second_changeset_id = second_response.agent_run.changeset_id
        change_types = [
            item.change_type
            for item in db.query(ChangeItem).filter(ChangeItem.changeset_id == second_changeset_id).all()
        ]
        assert "TEXT_EXTRACTED" in change_types
        assert "TEXT_REUSED" not in change_types
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_classification_persistence_replaces_existing_suggestions_for_same_agent_run():
    """同一个 AgentRun 重写分类建议时，应删除旧建议再写入新建议，保证幂等。"""

    client = _client_with_database()
    headers = _auth_header(client, username="classification-idempotent-user")
    document_id = _upload_document(client, headers, filename="idempotent.txt")

    db = next(app.dependency_overrides[get_db]())
    try:
        user = db.query(User).filter(User.username == "classification-idempotent-user").one()
        agent_run = AgentRun(conversation_id="conv-idempotent", message_id="msg-idempotent", user_id=user.id)
        db.add(agent_run)
        db.flush()

        persist_document_results_classifications(
            db=db,
            agent_run_id=agent_run.id,
            document_results=[
                {
                    "document_id": document_id,
                    "categories": [
                        {
                            "name": "学校/人事师资/职称",
                            "category_path": ["学校", "人事师资", "职称"],
                            "confidence": 0.72,
                            "status": "SUGGESTED",
                            "evidence": ["职称"],
                            "taxonomy_key": "school_file_classification",
                            "taxonomy_version": "2026-06",
                        }
                    ],
                }
            ],
        )
        persist_document_results_classifications(
            db=db,
            agent_run_id=agent_run.id,
            document_results=[
                {
                    "document_id": document_id,
                    "categories": [
                        {
                            "name": "学校/人事师资/职称",
                            "category_path": ["学校", "人事师资", "职称"],
                            "confidence": 0.72,
                            "status": "SUGGESTED",
                            "evidence": ["职称"],
                            "taxonomy_key": "school_file_classification",
                            "taxonomy_version": "2026-06",
                        },
                        {
                            "name": "学校/党委相关/干部工作",
                            "category_path": ["学校", "党委相关", "干部工作"],
                            "confidence": 0.7,
                            "status": "SUGGESTED",
                            "evidence": ["干部工作"],
                            "taxonomy_key": "school_file_classification",
                            "taxonomy_version": "2026-06",
                        },
                    ],
                }
            ],
        )

        assert db.query(DocumentClassificationRun).count() == 1
        assert db.query(DocumentCategorySuggestion).count() == 2
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_deterministic_message_extracts_and_classifies_multiple_documents():
    """LLM 关闭时，“读取并分类”组合意图也必须解析全部附件并输出分类建议。"""

    class DisabledLLMIntentService:
        """测试用关闭态 LLM 服务，强制消息入口走确定性 Planner。"""

        enabled = False

    client = _client_with_database()
    headers = _auth_header(client, username="deterministic-read-classify-user")
    staff_document_id = _upload_document(
        client,
        headers,
        filename="det-staff.txt",
        content="本文件涉及学校教师职称和干部工作材料。".encode(),
    )
    plan_document_id = _upload_document(
        client,
        headers,
        filename="det-plan.txt",
        content="本文件是学院年度计划、总结材料。".encode(),
    )
    unknown_document_id = _upload_document(
        client,
        headers,
        filename="det-unknown.txt",
        content="这是一段无法判断归类的普通文本。".encode(),
    )

    db = next(app.dependency_overrides[get_db]())
    try:
        user = db.query(User).filter(User.username == "deterministic-read-classify-user").one()
        response = ConversationMessageService(
            db=db,
            agent_service=AgentRuntimeService(llm_intent_service=DisabledLLMIntentService()),
        ).send_user_message(
            conversation_id="deterministic-read-classify-conv",
            user_id=user.id,
            request=SendMessageRequest(
                content="帮我读取并分类这批文件",
                attachments=[
                    MessageAttachment(document_id=staff_document_id),
                    MessageAttachment(document_id=plan_document_id),
                    MessageAttachment(document_id=unknown_document_id),
                ],
            ),
        )

        assert response.agent_run.intent == "EXTRACT_DOCUMENT_TEXT"
        assert [item.tool_name for item in response.agent_run.tool_invocations] == [
            "extract-document-text",
            "extract-document-text",
            "extract-document-text",
        ]
        assert db.query(DocumentExtractionRun).count() == 3
        assert db.query(DocumentPage).count() == 3
        document_results = db.query(AgentRun).one().graph_state_json["document_results"]
        assert len(document_results) == 3
        assert "学校/人事师资/职称" in [
            category["name"] for category in document_results[0]["categories"]
        ]
        assert document_results[1]["categories"][0]["name"] == "学院/行政管理/年度计划、总结"
        assert document_results[2]["categories"][0]["name"] == "其他"
        assert "置信度" in (response.agent_run.final_response or "")
        assert response.agent_run.changeset_id
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "TEXT_EXTRACTED").count() == 3
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "CATEGORY_SUGGESTED").count() >= 3
    finally:
        db.close()
        app.dependency_overrides.clear()


def test_alembic_files_exist_for_runtime_tables():
    """迁移配置必须存在，避免 ORM 表只停留在测试内存里。"""

    assert Path("apps/api/alembic.ini").exists()
    assert Path("apps/api/alembic/env.py").exists()
    versions = list(Path("apps/api/alembic/versions").glob("*_create_runtime_tables.py"))
    assert len(versions) == 1
    classification_versions = list(
        Path("apps/api/alembic/versions").glob("*_create_classification_suggestion_tables.py")
    )
    assert len(classification_versions) == 1
    changeset_versions = list(Path("apps/api/alembic/versions").glob("*_create_changeset_tables.py"))
    assert len(changeset_versions) == 1
