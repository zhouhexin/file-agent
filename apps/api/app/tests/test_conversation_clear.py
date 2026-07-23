"""会话清空接口测试。

该接口只能隐藏当前用户的聊天记录，不能删除文件或破坏 AgentRun 的审计外键。
"""

from app.db.models import AgentRun, Message
from app.tests.helpers import client_with_database


def _auth(client, username: str) -> dict[str, str]:
    """注册并登录测试用户，返回认证头。"""

    client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    response = client.post("/api/auth/login", json={"username": username, "password": "password123"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_clear_conversation_hides_messages_but_preserves_agent_run_audit():
    """清空后历史不可见，但 AgentRun 仍引用原消息而不会破坏审计。"""

    client, session_factory = client_with_database()
    headers = _auth(client, "clear-conversation-user")
    sent = client.post(
        "/api/conversations/clear-conversation/messages",
        headers=headers,
        json={"content": "你好", "attachments": []},
    )
    assert sent.status_code == 200
    message_id = sent.json()["message"]["id"]

    cleared = client.delete("/api/conversations/clear-conversation", headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["cleared_message_count"] == 1

    history = client.get("/api/conversations/clear-conversation", headers=headers)
    assert history.status_code == 200
    assert history.json()["messages"] == []

    with session_factory() as db:
        message = db.get(Message, message_id)
        assert message is not None
        assert message.role == "CLEARED"
        assert db.query(AgentRun).filter(AgentRun.message_id == message_id).count() == 1


def test_clear_conversation_rejects_another_users_history():
    """普通用户不得清空其他用户的会话。"""

    client, _ = client_with_database()
    owner_headers = _auth(client, "clear-owner")
    other_headers = _auth(client, "clear-other")
    sent = client.post(
        "/api/conversations/private-clear-conversation/messages",
        headers=owner_headers,
        json={"content": "你好", "attachments": []},
    )
    assert sent.status_code == 200

    rejected = client.delete("/api/conversations/private-clear-conversation", headers=other_headers)
    assert rejected.status_code == 403
