"""认证和默认工作区测试。

这些测试保护最小 Auth 闭环：注册、登录、JWT 当前用户和 default workspace 自动创建。
"""

from app.db.models import User, Workspace
from app.tests.helpers import clear_overrides, client_with_database


def test_register_creates_user_and_default_workspace():
    """注册成功后必须创建用户并自动创建 default workspace。"""

    client, SessionLocal = client_with_database()

    response = client.post(
        "/api/auth/register",
        json={"username": "zhangsan", "password": "password123", "display_name": "张三"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "zhangsan"
    assert data["display_name"] == "张三"
    assert data["role"] == "user"
    assert data["default_workspace_id"]

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "zhangsan").one()
        workspace = db.get(Workspace, data["default_workspace_id"])
        assert user.default_workspace_id == workspace.id
        assert workspace.owner_id == user.id
        assert workspace.is_default is True
    finally:
        db.close()
        clear_overrides()


def test_register_rejects_duplicate_username():
    """重复 username 必须返回 409，避免覆盖已有用户。"""

    client, _ = client_with_database()

    payload = {"username": "zhangsan", "password": "password123", "display_name": "张三"}
    assert client.post("/api/auth/register", json=payload).status_code == 200
    response = client.post("/api/auth/register", json=payload)

    assert response.status_code == 409
    clear_overrides()


def test_login_returns_bearer_token_and_me_returns_current_user():
    """登录成功返回 bearer token，`/api/auth/me` 能解析出当前用户。"""

    client, _ = client_with_database()
    client.post(
        "/api/auth/register",
        json={"username": "zhangsan", "password": "password123", "display_name": "张三"},
    )

    login_response = client.post(
        "/api/auth/login",
        json={"username": "zhangsan", "password": "password123"},
    )

    assert login_response.status_code == 200
    token_data = login_response.json()
    assert token_data["token_type"] == "bearer"
    token = token_data["access_token"]

    me_response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me_response.status_code == 200
    assert me_response.json()["username"] == "zhangsan"
    assert me_response.json()["default_workspace_id"]
    clear_overrides()


def test_login_rejects_wrong_password():
    """密码错误必须返回 401，不能泄漏用户是否存在之外的细节。"""

    client, _ = client_with_database()
    client.post(
        "/api/auth/register",
        json={"username": "zhangsan", "password": "password123", "display_name": "张三"},
    )

    response = client.post(
        "/api/auth/login",
        json={"username": "zhangsan", "password": "wrong-password"},
    )

    assert response.status_code == 401
    clear_overrides()


def test_me_requires_token():
    """当前用户接口必须要求 Authorization Bearer token。"""

    client, _ = client_with_database()

    response = client.get("/api/auth/me")

    assert response.status_code == 401
    clear_overrides()
