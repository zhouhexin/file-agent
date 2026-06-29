"""认证后的消息入口测试。

这些测试确保 message、conversation 和 AgentRun 都使用真实登录用户，而不是 `user-memory`。
"""

from app.db.models import AgentRun, Conversation, Message
from app.tests.helpers import clear_overrides, client_with_database


def _register_and_login(client, username: str = "zhangsan") -> tuple[str, str]:
    """注册并登录测试用户，返回 user_id 和 access_token。"""

    register_response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    user_id = register_response.json()["id"]
    login_response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return user_id, login_response.json()["access_token"]


def _upload_document(client, token: str, filename: str = "message.txt") -> str:
    """上传测试文件并返回 document_id。"""

    response = client.post(
        "/api/files/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, b"message-file", "text/plain")},
    )
    return response.json()["document_id"]


def test_post_message_requires_token():
    """消息入口必须要求登录，未带 token 返回 401。"""

    client, _ = client_with_database()

    response = client.post(
        "/api/conversations/conv-1/messages",
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": "doc-1"}],
        },
    )

    assert response.status_code == 401
    clear_overrides()


def test_post_message_uses_authenticated_user_id():
    """发送消息后，message、conversation 和 AgentRun 必须写入当前用户 id。"""

    client, SessionLocal = client_with_database()
    user_id, token = _register_and_login(client)
    document_id = _upload_document(client, token)

    response = client.post(
        "/api/conversations/conv-1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": document_id}],
        },
    )

    assert response.status_code == 200
    assert response.json()["message"]["user_id"] == user_id
    assert response.json()["agent_run"]["user_id"] == user_id

    db = SessionLocal()
    try:
        assert db.get(Conversation, "conv-1").user_id == user_id
        assert db.query(Message).one().user_id == user_id
        assert db.query(AgentRun).one().user_id == user_id
    finally:
        db.close()
        clear_overrides()


def test_user_cannot_write_to_another_users_conversation():
    """用户不能通过 URL 操作另一个用户已有的 conversation。"""

    client, _ = client_with_database()
    _, first_token = _register_and_login(client, "first")
    _, second_token = _register_and_login(client, "second")
    first_document_id = _upload_document(client, first_token, "first.txt")
    second_document_id = _upload_document(client, second_token, "second.txt")

    first_response = client.post(
        "/api/conversations/shared-conv/messages",
        headers={"Authorization": f"Bearer {first_token}"},
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": first_document_id}],
        },
    )
    assert first_response.status_code == 200

    second_response = client.post(
        "/api/conversations/shared-conv/messages",
        headers={"Authorization": f"Bearer {second_token}"},
        json={
            "content": "我也要写入这个会话",
            "attachments": [{"document_id": second_document_id}],
        },
    )

    assert second_response.status_code == 403
    clear_overrides()


def test_user_cannot_read_another_users_conversation():
    """用户不能通过会话详情接口读取另一个用户的历史记录。"""

    client, _ = client_with_database()
    _, first_token = _register_and_login(client, "history-first")
    _, second_token = _register_and_login(client, "history-second")
    first_document_id = _upload_document(client, first_token, "first-history.txt")

    create_response = client.post(
        "/api/conversations/private-conv/messages",
        headers={"Authorization": f"Bearer {first_token}"},
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": first_document_id}],
        },
    )
    assert create_response.status_code == 200

    response = client.get(
        "/api/conversations/private-conv",
        headers={"Authorization": f"Bearer {second_token}"},
    )

    assert response.status_code == 403
    clear_overrides()
