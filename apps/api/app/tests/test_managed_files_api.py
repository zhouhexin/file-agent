"""受管目录 Admin API 和查询 API 测试。"""

from datetime import datetime, timezone

from app.db.models import ManagedFile, ManagedRoot, User
from app.tests.helpers import clear_overrides, client_with_database


def _register_and_login(client, username: str) -> tuple[str, str]:
    """注册并登录测试用户。"""

    register_response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    login_response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return register_response.json()["id"], login_response.json()["access_token"]


def _auth_header(token: str) -> dict[str, str]:
    """构造认证请求头。"""

    return {"Authorization": f"Bearer {token}"}


def _make_admin(SessionLocal, user_id: str) -> None:
    """把测试用户提升为 admin。"""

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        user.role = "admin"
        db.commit()
    finally:
        db.close()


def test_admin_can_enable_predefined_managed_root(monkeypatch):
    """管理员只能启用部署层通过环境变量预定义的逻辑目录。"""

    monkeypatch.setenv("MANAGED_ROOT_STUDENT_AFFAIRS", "/managed/student-affairs")
    monkeypatch.setenv("MANAGED_ROOT_STUDENT_AFFAIRS_NAME", "学工收件箱")
    client, SessionLocal = client_with_database()
    user_id, token = _register_and_login(client, "managed-root-admin")
    _make_admin(SessionLocal, user_id)

    response = client.post(
        "/api/admin/managed-roots",
        headers=_auth_header(token),
        json={"root_key": "student_affairs", "display_name": "学工收件箱"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["root_key"] == "student_affairs"
    assert data["display_name"] == "学工收件箱"
    assert data["read_only"] is True
    assert "container_path" not in data

    db = SessionLocal()
    try:
        root = db.query(ManagedRoot).one()
        assert root.container_path == "/managed/student-affairs"
        assert root.created_by == user_id
    finally:
        db.close()
        clear_overrides()


def test_user_cannot_enable_managed_root(monkeypatch):
    """普通用户不能启用服务器目录。"""

    monkeypatch.setenv("MANAGED_ROOT_STUDENT_AFFAIRS", "/managed/student-affairs")
    client, _ = client_with_database()
    _, token = _register_and_login(client, "managed-root-user")

    response = client.post(
        "/api/admin/managed-roots",
        headers=_auth_header(token),
        json={"root_key": "student_affairs", "display_name": "学工收件箱"},
    )

    assert response.status_code == 403
    clear_overrides()


def test_admin_rejects_unconfigured_root_key():
    """未由部署层声明的 root_key 不能通过 API 启用。"""

    client, SessionLocal = client_with_database()
    user_id, token = _register_and_login(client, "managed-root-missing")
    _make_admin(SessionLocal, user_id)

    response = client.post(
        "/api/admin/managed-roots",
        headers=_auth_header(token),
        json={"root_key": "unknown", "display_name": "未知目录"},
    )

    assert response.status_code == 400
    clear_overrides()


def test_managed_files_query_returns_logical_metadata_only():
    """用户可以按扩展名查询受管文件，响应不能泄露 container_path。"""

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "managed-file-reader")
    db = SessionLocal()
    try:
        root = ManagedRoot(root_key="student_affairs", display_name="学工收件箱", container_path="/managed/student-affairs")
        db.add(root)
        db.flush()
        db.add(
            ManagedFile(
                root_id=root.id,
                relative_path="2026/a.pdf",
                filename="a.pdf",
                extension=".pdf",
                size_bytes=100,
                modified_at=datetime.now(timezone.utc),
                fingerprint="fp",
                status="ACTIVE",
            )
        )
        db.add(
            ManagedFile(
                root_id=root.id,
                relative_path="2026/b.xlsx",
                filename="b.xlsx",
                extension=".xlsx",
                size_bytes=200,
                modified_at=datetime.now(timezone.utc),
                fingerprint="fp2",
                status="ACTIVE",
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/managed-files?root_key=student_affairs&extension=pdf",
        headers=_auth_header(token),
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["root_key"] == "student_affairs"
    assert data[0]["relative_path"] == "2026/a.pdf"
    assert "container_path" not in data[0]
    clear_overrides()
