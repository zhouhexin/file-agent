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
    assert data["classification_mode"] == "NONE"
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


def test_admin_can_mark_managed_root_as_path_classified(monkeypatch):
    """管理员可以显式声明某个受管目录使用父目录作为分类。"""

    monkeypatch.setenv("MANAGED_ROOT_CLASSIFIED_LIBRARY", "/managed/classified-library")
    client, SessionLocal = client_with_database()
    user_id, token = _register_and_login(client, "managed-root-classified")
    _make_admin(SessionLocal, user_id)

    response = client.post(
        "/api/admin/managed-roots",
        headers=_auth_header(token),
        json={
            "root_key": "classified_library",
            "display_name": "已分类文件库",
            "classification_mode": "PATH_AS_CATEGORY",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["classification_mode"] == "PATH_AS_CATEGORY"

    db = SessionLocal()
    try:
        root = db.query(ManagedRoot).one()
        assert root.classification_mode == "PATH_AS_CATEGORY"
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


def test_managed_files_query_returns_logical_metadata_only(monkeypatch, tmp_path):
    """用户可以按扩展名查询受管文件，响应不能泄露 container_path。"""

    managed_root = tmp_path / "student-affairs"
    file_dir = managed_root / "2026"
    file_dir.mkdir(parents=True)
    (file_dir / "a.pdf").write_text("demo", encoding="utf-8")
    (file_dir / "b.xlsx").write_text("demo", encoding="utf-8")
    monkeypatch.setenv("MANAGED_ROOT_STUDENT_AFFAIRS", str(managed_root))
    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "managed-file-reader")

    response = client.get(
        "/api/managed-files?root_key=student_affairs&extension=pdf",
        headers=_auth_header(token),
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["root_key"] == "student_affairs"
    assert data[0]["relative_path"] == "2026/a.pdf"
    assert data[0]["category_path"] is None
    assert "container_path" not in data[0]
    clear_overrides()


def test_managed_file_preview_returns_safe_text_blob(monkeypatch, tmp_path):
    """搜索结果预览只能通过 root_key + relative_path 读取安全文本文件。"""

    managed_root = tmp_path / "student-affairs"
    file_dir = managed_root / "2026"
    file_dir.mkdir(parents=True)
    (file_dir / "notice.txt").write_text("第一行通知\n第二行要求", encoding="utf-8")
    monkeypatch.setenv("MANAGED_ROOT_STUDENT_AFFAIRS", str(managed_root))
    client, _ = client_with_database()
    _, token = _register_and_login(client, "managed-preview-reader")

    response = client.get(
        "/api/managed-files/preview?root_key=student_affairs&relative_path=2026/notice.txt",
        headers=_auth_header(token),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "第一行通知\n第二行要求"
    clear_overrides()


def test_managed_file_preview_rejects_path_escape(monkeypatch, tmp_path):
    """受管文件预览必须复用路径策略，不能读取根目录外文件。"""

    managed_root = tmp_path / "student-affairs"
    managed_root.mkdir()
    monkeypatch.setenv("MANAGED_ROOT_STUDENT_AFFAIRS", str(managed_root))
    client, _ = client_with_database()
    _, token = _register_and_login(client, "managed-preview-escape")

    response = client.get(
        "/api/managed-files/preview?root_key=student_affairs&relative_path=../secret.txt",
        headers=_auth_header(token),
    )

    assert response.status_code == 400
    clear_overrides()


def test_user_query_auto_reads_env_managed_root_without_admin_registration(monkeypatch, tmp_path):
    """普通用户查询 env 受管目录时，系统应自动登记和扫描，不需要 Admin 预操作。"""

    managed_root = tmp_path / "spreadsheet-patches"
    managed_root.mkdir()
    (managed_root / "a.xlsx").write_text("name,amount\nalice,10\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_FILE_AGENT_SPREADSHEET_PATCH_FILES", str(managed_root))
    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "managed-env-reader")

    response = client.get(
        "/api/managed-files?root_key=file_agent_spreadsheet_patch_files",
        headers=_auth_header(token),
    )

    assert response.status_code == 200
    data = response.json()
    assert [file["relative_path"] for file in data] == ["a.xlsx"]
    assert data[0]["display_name"] == "file_agent_spreadsheet_patch_files"

    db = SessionLocal()
    try:
        root = db.query(ManagedRoot).one()
        assert root.root_key == "file_agent_spreadsheet_patch_files"
        assert root.display_name == "file_agent_spreadsheet_patch_files"
        assert root.container_path == str(managed_root)
    finally:
        db.close()
        clear_overrides()


def test_managed_files_query_filters_path_prefix(monkeypatch, tmp_path):
    """普通用户可以按受管目录内子目录查询文件。"""

    managed_root = tmp_path / "spreadsheet-patches"
    nested_dir = managed_root / "deploy" / "nested"
    nested_dir.mkdir(parents=True)
    (managed_root / "README.md").write_text("root", encoding="utf-8")
    (managed_root / "deploy" / "a.ps1").write_text("deploy", encoding="utf-8")
    (nested_dir / "b.txt").write_text("nested", encoding="utf-8")
    monkeypatch.setenv("MANAGED_ROOT_FILE_AGENT_SPREADSHEET_PATCH_FILES", str(managed_root))
    client, _ = client_with_database()
    _, token = _register_and_login(client, "managed-path-reader")

    response = client.get(
        "/api/managed-files"
        "?root_key=file_agent_spreadsheet_patch_files"
        "&path_prefix=deploy",
        headers=_auth_header(token),
    )

    assert response.status_code == 200
    data = response.json()
    assert [file["relative_path"] for file in data] == [
        "deploy/a.ps1",
        "deploy/nested/b.txt",
    ]
    clear_overrides()


def test_managed_files_query_treats_unknown_root_as_single_configured_subdirectory(monkeypatch, tmp_path):
    """HTTP 查询遇到未配置 root_key 时，应优先按唯一受管根下子目录处理。"""

    downloads_root = tmp_path / "Downloads"
    target_dir = downloads_root / "file_agent_spreadsheet_patch_files"
    target_dir.mkdir(parents=True)
    (downloads_root / "parent.xlsx").write_text("parent", encoding="utf-8")
    (downloads_root / ".DS_Store").write_text("hidden", encoding="utf-8")
    (target_dir / "README.md").write_text("readme", encoding="utf-8")
    (target_dir / ".DS_Store").write_text("hidden", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_DOWNLOADS", str(downloads_root))
    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "managed-unknown-root-reader")
    db = SessionLocal()
    try:
        stale_root = ManagedRoot(
            root_key="file_agent_spreadsheet_patch_files",
            display_name="file_agent_spreadsheet_patch_files",
            container_path=str(downloads_root),
            classification_mode="NONE",
        )
        db.add(stale_root)
        db.flush()
        db.add(
            ManagedFile(
                root_id=stale_root.id,
                relative_path="parent.xlsx",
                category_path=None,
                filename="parent.xlsx",
                extension=".xlsx",
                size_bytes=100,
                modified_at=datetime.now(timezone.utc),
                fingerprint="stale",
                status="ACTIVE",
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/managed-files?root_key=file_agent_spreadsheet_patch_files",
        headers=_auth_header(token),
    )

    assert response.status_code == 200
    data = response.json()
    assert [file["relative_path"] for file in data] == [
        "file_agent_spreadsheet_patch_files/README.md",
    ]
    assert data[0]["root_key"] == "downloads"
    clear_overrides()


def test_category_tree_only_uses_path_classified_roots():
    """分类目录树只能来自 PATH_AS_CATEGORY 受管目录。"""

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "managed-category-reader")
    db = SessionLocal()
    try:
        classified_root = ManagedRoot(
            root_key="classified_library",
            display_name="已分类文件库",
            container_path="/managed/classified-library",
            classification_mode="PATH_AS_CATEGORY",
        )
        plain_root = ManagedRoot(
            root_key="plain_inbox",
            display_name="普通收件箱",
            container_path="/managed/plain-inbox",
            classification_mode="NONE",
        )
        db.add_all([classified_root, plain_root])
        db.flush()
        db.add(
            ManagedFile(
                root_id=classified_root.id,
                relative_path="奖学金/国家励志奖学金/a.pdf",
                category_path="奖学金/国家励志奖学金",
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
                root_id=plain_root.id,
                relative_path="临时/b.pdf",
                category_path=None,
                filename="b.pdf",
                extension=".pdf",
                size_bytes=100,
                modified_at=datetime.now(timezone.utc),
                fingerprint="fp2",
                status="ACTIVE",
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get(
        "/api/managed-file-categories?root_key=classified_library",
        headers=_auth_header(token),
    )

    assert response.status_code == 200
    data = response.json()
    assert data == [
        {
            "root_key": "classified_library",
            "display_name": "已分类文件库",
            "category_path": "奖学金/国家励志奖学金",
            "file_count": 1,
        }
    ]
    clear_overrides()
