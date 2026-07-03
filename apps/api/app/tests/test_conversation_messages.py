"""会话消息入口的行为测试。

这些测试保护 `/api/conversations/{conversation_id}/messages` 的第一阶段目标：
HTTP 消息必须能进入 LangGraph Agent Runtime，但当前不依赖真实大模型或数据库。
"""

from fastapi.testclient import TestClient

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


def test_post_message_starts_agent_run():
    """发送用户消息后，接口必须返回 message 和持久化 AgentRun 结果。"""

    client, _ = client_with_database()
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
    assert data["agent_run"]["status"] == "COMPLETED"
    assert data["agent_run"]["intent"] == "CLASSIFY_FILES"
    assert data["agent_run"]["selected_skills"] == [
        "chat-intake",
        "document-text-extract",
        "document-classification",
        "change-report",
    ]
    assert [item["tool_name"] for item in data["agent_run"]["tool_invocations"]] == [
        "extract-document-text",
    ]
    clear_overrides()


def test_get_conversation_returns_messages_with_agent_runs_and_attachments():
    """读取会话详情时必须返回刷新页面所需的消息、附件和 AgentRun 结果。"""

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
    assert history_message["agent_run"]["status"] == "COMPLETED"
    assert history_message["agent_run"]["final_response"]
    assert len(history_message["agent_run"]["document_results"]) == 1
    assert history_message["agent_run"]["document_results"][0]["document_id"] == document_id
    assert history_message["agent_run"]["document_results"][0]["filename"] == "message.txt"
    assert history_message["agent_run"]["document_results"][0]["extraction_status"] == "COMPLETED"
    assert history_message["agent_run"]["tool_invocations"][0]["tool_name"] == "extract-document-text"
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
    assert data["agent_run"]["status"] == "COMPLETED"
    assert data["agent_run"]["document_results"][0]["document_id"] == document_id
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
    assert data["agent_run"]["status"] == "COMPLETED"
    assert data["agent_run"]["document_results"][0]["document_id"] == second_document_id
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
    final_response = second_response.json()["agent_run"]["final_response"]
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
    final_response = data["agent_run"]["final_response"]
    assert "最新批次-财务.txt" in final_response
    assert "旧批次-职称.txt" not in final_response
    assert "旧批次-科研.txt" not in final_response
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
