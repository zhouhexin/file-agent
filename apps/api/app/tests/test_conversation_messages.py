"""会话消息入口的行为测试。

这些测试保护 `/api/conversations/{conversation_id}/messages` 的第一阶段目标：
HTTP 消息必须能进入 LangGraph Agent Runtime，但当前不依赖真实大模型或数据库。
"""

from fastapi.testclient import TestClient

from app.main import app


def test_post_message_starts_agent_run():
    """发送用户消息后，接口必须返回 message 和持久化 AgentRun 结果。"""

    client = TestClient(app)

    response = client.post(
        "/api/conversations/conv-1/messages",
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": "doc-1"}],
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
        "file-ingest",
        "document-classification",
        "change-report",
    ]
    assert [item["tool_name"] for item in data["agent_run"]["tool_invocations"]] == [
        "document-convert",
        "metadata-extract",
        "multi-label-classify",
        "change-report",
    ]


def test_post_message_rejects_invalid_attachment():
    """附件引用缺少 document_id 时必须由请求 schema 拒绝。"""

    client = TestClient(app)

    response = client.post(
        "/api/conversations/conv-1/messages",
        json={
            "content": "帮我读取文件",
            "attachments": [{"filename": "bad.pdf"}],
        },
    )

    assert response.status_code == 422
