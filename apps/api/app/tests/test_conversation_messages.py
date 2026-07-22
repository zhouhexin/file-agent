"""会话消息入口的行为测试。

这些测试保护 `/api/conversations/{conversation_id}/messages` 的第一阶段目标：
HTTP 消息必须能进入 LangGraph Agent Runtime，但当前不依赖真实大模型或数据库。
"""

from datetime import datetime, timedelta, timezone
from io import BytesIO

from fastapi.testclient import TestClient
import openpyxl

from app.db.models import AgentRun, Conversation, Message, ToolInvocation
from app.tests.helpers import clear_overrides, client_with_database


def _auth_header(client: TestClient, username: str = "message-user") -> dict[str, str]:
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
    filename: str = "message.txt",
    content: bytes = b"message-file",
) -> str:
    """上传测试文件并返回 document_id。"""

    response = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": (filename, content, "text/plain")},
    )
    return response.json()["document_id"]


def _xlsx_with_formula_error() -> bytes:
    """构造包含显式公式错误的 Excel 测试文件。"""

    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "汇总"
    worksheet.append(["项目", "公式"])
    worksheet.append(["A", "=SUM(#REF!)"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _latest_agent_audit(session_factory) -> tuple[AgentRun, list[str]]:
    """从测试数据库读取最近一次内部运行，普通消息响应本身不得暴露这些字段。"""

    with session_factory() as db:
        run = db.query(AgentRun).order_by(AgentRun.created_at.desc()).first()
        assert run is not None
        tool_names = [
            item.tool_name
            for item in (
                db.query(ToolInvocation)
                .filter(ToolInvocation.agent_run_id == run.id)
                .order_by(ToolInvocation.created_at.asc())
                .all()
            )
        ]
        db.expunge(run)
        return run, tool_names


def test_post_message_starts_agent_run():
    """发送消息后必须持久化 AgentRun，但普通响应只能返回安全任务投影。"""

    client, session_factory = client_with_database()
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
    assert data["message"]["conversation_id"] == "conv-1"
    assert data["message"]["role"] == "user"
    assert "agent_run" not in data
    run, tool_names = _latest_agent_audit(session_factory)
    assert run.status == "COMPLETED"
    assert run.intent == "CLASSIFY_FILES"
    assert run.selected_skills_json == [
        "chat-intake",
        "document-text-extract",
        "document-classification",
        "change-report",
    ]
    assert tool_names == ["extract-document-text"]
    # 普通用户投影不能要求前端理解 Skill 或 Tool，也不能携带解析器和内部路径字段。
    task_result = data["task_result"]
    assert task_result["task_id"] == run.id
    assert task_result["task_status"] == "completed"
    assert task_result["response_type"] == "file_results"
    assert "selected_skills" not in task_result
    assert "tool_invocations" not in task_result
    assert "tool_results" not in task_result
    assert "extractor" not in task_result["document_results"][0]
    assert "relative_path" not in task_result["document_results"][0]
    assert "index_run_id" not in task_result["document_results"][0]
    assert "search_text" not in task_result["document_results"][0]
    assert "embedding" not in task_result["document_results"][0]
    clear_overrides()


def test_get_conversation_returns_messages_with_task_results_and_attachments():
    """读取会话详情时必须返回附件和安全任务投影，不返回内部 AgentRun。"""

    client, _ = client_with_database()
    headers = _auth_header(client, "history-user")
    document_id = _upload_document(client, headers)

    post_response = client.post(
        "/api/conversations/web-chat/messages",
        headers=headers,
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": document_id}],
        },
    )
    assert post_response.status_code == 200

    response = client.get("/api/conversations/web-chat", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "web-chat"
    assert len(data["messages"]) == 1
    history_message = data["messages"][0]
    assert history_message["content"] == "帮我读取并分类这批文件"
    assert history_message["attachments"][0]["document_id"] == document_id
    assert history_message["attachments"][0]["filename"] == "message.txt"
    assert "agent_run" not in history_message
    assert history_message["task_result"]["task_status"] == "completed"
    assert history_message["task_result"]["final_response"]
    assert len(history_message["task_result"]["document_results"]) == 1
    assert history_message["task_result"]["document_results"][0]["document_id"] == document_id
    assert history_message["task_result"]["document_results"][0]["filename"] == "message.txt"
    assert history_message["task_result"]["document_results"][0]["extraction_status"] == "COMPLETED"
    assert "tool_invocations" not in history_message["task_result"]
    clear_overrides()


def test_get_conversation_returns_latest_page_with_pagination():
    """会话详情默认只返回最近一页消息，避免聊天页首屏加载完整历史。"""

    client, session_factory = client_with_database()
    headers = _auth_header(client, "paged-history-user")
    me_response = client.get("/api/auth/me", headers=headers)
    current_user_id = me_response.json()["id"]
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with session_factory() as db:
        db.add(Conversation(id="paged-chat", user_id=current_user_id, title=""))
        for index in range(15):
            db.add(
                Message(
                    conversation_id="paged-chat",
                    user_id=current_user_id,
                    role="user",
                    content=f"历史消息 {index + 1}",
                    attachments_json=[],
                    created_at=base_time + timedelta(seconds=index),
                )
            )
        db.commit()

    response = client.get("/api/conversations/paged-chat?limit=10", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert [message["content"] for message in data["messages"]] == [
        f"历史消息 {index}" for index in range(6, 16)
    ]
    assert data["pagination"]["has_more"] is True
    assert data["pagination"]["oldest_message_id"] == data["messages"][0]["id"]
    assert data["pagination"]["limit"] == 10
    clear_overrides()


def test_get_conversation_returns_older_page_before_message_id():
    """传入 before_message_id 时返回该消息之前的更早历史。"""

    client, session_factory = client_with_database()
    headers = _auth_header(client, "older-history-user")
    current_user_id = client.get("/api/auth/me", headers=headers).json()["id"]
    message_ids: list[str] = []
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with session_factory() as db:
        db.add(Conversation(id="older-chat", user_id=current_user_id, title=""))
        for index in range(12):
            message = Message(
                conversation_id="older-chat",
                user_id=current_user_id,
                role="user",
                content=f"消息 {index + 1}",
                attachments_json=[],
                created_at=base_time + timedelta(seconds=index),
            )
            db.add(message)
            db.flush()
            message_ids.append(message.id)
        db.commit()

    response = client.get(
        f"/api/conversations/older-chat?limit=5&before_message_id={message_ids[7]}",
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert [message["content"] for message in data["messages"]] == [
        "消息 3",
        "消息 4",
        "消息 5",
        "消息 6",
        "消息 7",
    ]
    assert data["pagination"]["has_more"] is True
    assert data["pagination"]["oldest_message_id"] == data["messages"][0]["id"]
    clear_overrides()


def test_message_can_reference_previous_uploaded_attachment():
    """用户说“上面上传的文件”时，应自动引用当前会话最近的附件。"""

    client, _ = client_with_database()
    headers = _auth_header(client, "previous-file-user")
    document_id = _upload_document(client, headers)

    first_response = client.post(
        "/api/conversations/context-chat/messages",
        headers=headers,
        json={
            "content": "帮我读取这个文件",
            "attachments": [{"document_id": document_id}],
        },
    )
    assert first_response.status_code == 200

    second_response = client.post(
        "/api/conversations/context-chat/messages",
        headers=headers,
        json={
            "content": "读取上面上传的文件，给我讲解大概总结一下文章内容",
            "attachments": [],
        },
    )

    assert second_response.status_code == 200
    data = second_response.json()
    assert data["message"]["attachments"] == [{"document_id": document_id}]
    assert data["task_result"]["task_status"] == "completed"
    assert data["task_result"]["document_results"][0]["document_id"] == document_id
    clear_overrides()


def test_message_can_reference_second_previous_attachment_by_ordinal():
    """用户说“第二个文件”时，应只引用当前会话上文附件中的第二个文件。"""

    client, _ = client_with_database()
    headers = _auth_header(client, "second-file-user")
    first_document_id = _upload_document(client, headers, filename="first.txt", content=b"first-file")
    second_document_id = _upload_document(client, headers, filename="电子发票承诺书.doc", content=b"second-file")

    first_response = client.post(
        "/api/conversations/ordinal-chat/messages",
        headers=headers,
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [
                {"document_id": first_document_id},
                {"document_id": second_document_id},
            ],
        },
    )
    assert first_response.status_code == 200

    second_response = client.post(
        "/api/conversations/ordinal-chat/messages",
        headers=headers,
        json={
            "content": "重新对第二个文件：电子发票承诺书.doc进行分类",
            "attachments": [],
        },
    )

    assert second_response.status_code == 200
    data = second_response.json()
    assert data["message"]["attachments"] == [{"document_id": second_document_id}]
    assert data["task_result"]["task_status"] == "completed"
    assert data["task_result"]["document_results"][0]["document_id"] == second_document_id
    clear_overrides()


def test_message_can_reference_previous_attachment_by_filename_fragment():
    """用户按文件名片段提问时，应自动引用当前会话中的对应历史附件。"""

    client, session_factory = client_with_database()
    headers = _auth_header(client, "filename-reference-user")
    document_id = _upload_document(
        client,
        headers,
        filename="2019年学院科研成果资助表.xlsx",
        content="姓名,金额\n张三,100\n李四,200\n".encode(),
    )

    first_response = client.post(
        "/api/conversations/filename-reference-chat/messages",
        headers=headers,
        json={
            "content": "帮我读取这个文件",
            "attachments": [{"document_id": document_id}],
        },
    )
    assert first_response.status_code == 200

    second_response = client.post(
        "/api/conversations/filename-reference-chat/messages",
        headers=headers,
        json={
            "content": "汇总2019年学院科研成果资助表中的金额",
            "attachments": [],
        },
    )

    assert second_response.status_code == 200
    data = second_response.json()
    assert data["message"]["attachments"] == [{"document_id": document_id}]
    run, tool_names = _latest_agent_audit(session_factory)
    assert run.intent == "ANALYZE_SPREADSHEET"
    assert tool_names == ["analyze-spreadsheet"]
    assert "AgentRun completed" not in (data["task_result"]["final_response"] or "")
    clear_overrides()


def test_message_can_reference_previous_attachment_by_fuzzy_filename_tokens():
    """用户只说文件名中的核心词时，应通过年份和关键词匹配到历史附件。"""

    client, session_factory = client_with_database()
    headers = _auth_header(client, "fuzzy-filename-reference-user")
    old_document_id = _upload_document(
        client,
        headers,
        filename="2019年学院科研成果资助汇总表.xlsx",
        content="教师,资助金额\n王老师,100\n".encode(),
    )
    target_document_id = _upload_document(
        client,
        headers,
        filename="2024年度学院科研成果资助汇总表.xlsx",
        content="教师,资助金额\n张老师,300\n李老师,200\n".encode(),
    )

    first_response = client.post(
        "/api/conversations/fuzzy-filename-reference-chat/messages",
        headers=headers,
        json={
            "content": "帮我读取这批文件",
            "attachments": [{"document_id": old_document_id}, {"document_id": target_document_id}],
        },
    )
    assert first_response.status_code == 200

    second_response = client.post(
        "/api/conversations/fuzzy-filename-reference-chat/messages",
        headers=headers,
        json={
            "content": "根据教师来汇总2024科研成果资助汇总表中的资助金额",
            "attachments": [],
        },
    )

    assert second_response.status_code == 200
    data = second_response.json()
    assert data["message"]["attachments"] == [{"document_id": target_document_id}]
    run, tool_names = _latest_agent_audit(session_factory)
    assert run.intent == "ANALYZE_SPREADSHEET"
    assert tool_names == ["analyze-spreadsheet"]
    assert "AgentRun completed" not in (data["task_result"]["final_response"] or "")
    clear_overrides()


def test_message_can_validate_uploaded_spreadsheet_formula_errors():
    """聊天入口中的表格校验请求必须路由到 validate-spreadsheet。"""

    client, session_factory = client_with_database()
    headers = _auth_header(client, "spreadsheet-validation-user")
    document_id = _upload_document(
        client,
        headers,
        filename="公式错误.xlsx",
        content=_xlsx_with_formula_error(),
    )

    response = client.post(
        "/api/conversations/spreadsheet-validation-chat/messages",
        headers=headers,
        json={
            "content": "检查这份表格有没有公式错误",
            "attachments": [{"document_id": document_id}],
        },
    )

    assert response.status_code == 200
    data = response.json()
    run, tool_names = _latest_agent_audit(session_factory)
    assert run.intent == "VALIDATE_SPREADSHEET"
    assert tool_names == ["validate-spreadsheet"]
    assert "#REF!" in (data["task_result"]["final_response"] or "")
    clear_overrides()


def test_message_can_summarize_previous_classification_results():
    """用户要求总结之前文件分类时，应读取分类建议而不是只返回基础洞察文件名。"""

    client, _ = client_with_database()
    headers = _auth_header(client, "classification-summary-user")
    first_document_id = _upload_document(client, headers, filename="职称材料.txt", content="教师职称申报材料".encode())
    second_document_id = _upload_document(client, headers, filename="科研成果.txt", content="学院科研成果资助材料".encode())

    first_response = client.post(
        "/api/conversations/classification-summary-chat/messages",
        headers=headers,
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [
                {"document_id": first_document_id},
                {"document_id": second_document_id},
            ],
        },
    )
    assert first_response.status_code == 200

    second_response = client.post(
        "/api/conversations/classification-summary-chat/messages",
        headers=headers,
        json={
            "content": "帮我总结一下刚刚上传文件的分类",
            "attachments": [],
        },
    )

    assert second_response.status_code == 200
    final_response = second_response.json()["task_result"]["final_response"]
    assert "已汇总" in final_response
    assert "分类建议" in final_response
    assert "基础洞察" not in final_response
    assert "职称材料.txt" in final_response
    assert "科研成果.txt" in final_response
    clear_overrides()


def test_just_uploaded_classification_uses_latest_attachment_batch_only():
    """“刚刚上传文件”应指向最近一条带附件消息中的整批文件，而不是所有历史附件。"""

    client, _ = client_with_database()
    headers = _auth_header(client, "latest-batch-user")
    old_first_id = _upload_document(client, headers, filename="旧批次-职称.txt", content="教师职称申报材料".encode())
    old_second_id = _upload_document(client, headers, filename="旧批次-科研.txt", content="学院科研成果资助材料".encode())
    latest_id = _upload_document(client, headers, filename="最新批次-财务.txt", content="电子发票财务承诺材料".encode())

    old_response = client.post(
        "/api/conversations/latest-batch-chat/messages",
        headers=headers,
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": old_first_id}, {"document_id": old_second_id}],
        },
    )
    assert old_response.status_code == 200

    latest_response = client.post(
        "/api/conversations/latest-batch-chat/messages",
        headers=headers,
        json={
            "content": "帮我读取并分类这个文件",
            "attachments": [{"document_id": latest_id}],
        },
    )
    assert latest_response.status_code == 200

    historical_summary_response = client.post(
        "/api/conversations/latest-batch-chat/messages",
        headers=headers,
        json={
            "content": "总结一下之前上传的所有项目分类",
            "attachments": [],
        },
    )
    assert historical_summary_response.status_code == 200
    assert len(historical_summary_response.json()["message"]["attachments"]) == 3

    summary_response = client.post(
        "/api/conversations/latest-batch-chat/messages",
        headers=headers,
        json={
            "content": "帮我总结一下刚刚上传的所有文件分类",
            "attachments": [],
        },
    )

    assert summary_response.status_code == 200
    data = summary_response.json()
    assert data["message"]["attachments"] == [{"document_id": latest_id}]
    final_response = data["task_result"]["final_response"]
    assert "最新批次-财务.txt" in final_response
    assert "旧批次-职称.txt" not in final_response
    assert "旧批次-科研.txt" not in final_response
    clear_overrides()


def test_uploaded_message_attachments_share_batch_id():
    """同一条用户消息里的真实上传附件必须带同一个 batch_id，供后续“刚刚上传”精确引用。"""

    client, session_factory = client_with_database()
    headers = _auth_header(client, "batch-marker-user")
    first_document_id = _upload_document(client, headers, filename="批次-1.txt", content=b"first")
    second_document_id = _upload_document(client, headers, filename="批次-2.txt", content=b"second")

    response = client.post(
        "/api/conversations/batch-marker-chat/messages",
        headers=headers,
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [
                {"document_id": first_document_id},
                {"document_id": second_document_id},
            ],
        },
    )

    assert response.status_code == 200
    with session_factory() as db:
        message = (
            db.query(Message)
            .filter(Message.conversation_id == "batch-marker-chat")
            .order_by(Message.created_at.desc())
            .first()
        )
        assert message is not None
        sources = {item.get("source") for item in message.attachments_json}
        batch_ids = {item.get("batch_id") for item in message.attachments_json}
    assert sources == {"uploaded"}
    assert len(batch_ids) == 1
    assert None not in batch_ids
    clear_overrides()


def test_post_message_rejects_invalid_attachment():
    """附件引用缺少 document_id 时必须由请求 schema 拒绝。"""

    client, _ = client_with_database()

    response = client.post(
        "/api/conversations/conv-1/messages",
        headers=_auth_header(client),
        json={
            "content": "帮我读取文件",
            "attachments": [{"filename": "bad.pdf"}],
        },
    )

    assert response.status_code == 422
    clear_overrides()
