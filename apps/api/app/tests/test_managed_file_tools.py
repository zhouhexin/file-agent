"""受管文件 Agent Tool 测试。"""

from datetime import datetime, timezone

from app.db.models import ManagedFile, ManagedRoot
from app.modules.agent.tool_registry import ToolRegistry
from app.tests.helpers import clear_overrides, client_with_database


def test_managed_file_list_tool_returns_logical_paths_only(monkeypatch, tmp_path):
    """managed-file-list Tool 只能返回逻辑路径和元数据，不能暴露容器路径。"""

    managed_root = tmp_path / "student-affairs"
    file_dir = managed_root / "2026"
    file_dir.mkdir(parents=True)
    (file_dir / "a.pdf").write_text("demo", encoding="utf-8")
    monkeypatch.setenv("MANAGED_ROOT_STUDENT_AFFAIRS", str(managed_root))
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {"root_key": "student_affairs", "extension": "pdf"},
        )

        assert result.status == "COMPLETED"
        assert result.output_json["ok"] is True
        assert result.output_json["files"][0]["relative_path"] == "2026/a.pdf"
        assert "container_path" not in result.output_json["files"][0]
    finally:
        db.close()
        clear_overrides()


def test_managed_file_list_tool_auto_reads_env_root_without_registration(monkeypatch, tmp_path):
    """env 配置的受管目录必须对 Agent Tool 自动生效，不需要先通过 Admin API 登记。"""

    managed_root = tmp_path / "spreadsheet-patches"
    managed_root.mkdir()
    (managed_root / "a.xlsx").write_text("name,amount\nalice,10\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_FILE_AGENT_SPREADSHEET_PATCH_FILES", str(managed_root))
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {"root_key": "file_agent_spreadsheet_patch_files"},
        )

        assert result.status == "COMPLETED"
        assert result.output_json["ok"] is True
        assert [file["relative_path"] for file in result.output_json["files"]] == ["a.xlsx"]
        assert result.output_json["files"][0]["display_name"] == "file_agent_spreadsheet_patch_files"
    finally:
        db.close()
        clear_overrides()


def test_managed_file_list_tool_filters_path_prefix(monkeypatch, tmp_path):
    """managed-file-list Tool 应按受管目录内的相对路径前缀过滤。"""

    managed_root = tmp_path / "spreadsheet-patches"
    deploy_dir = managed_root / "deploy" / "nested"
    deploy_dir.mkdir(parents=True)
    (managed_root / "README.md").write_text("root", encoding="utf-8")
    (managed_root / "deploy" / "a.ps1").write_text("deploy", encoding="utf-8")
    (deploy_dir / "b.txt").write_text("nested", encoding="utf-8")
    monkeypatch.setenv("MANAGED_ROOT_FILE_AGENT_SPREADSHEET_PATCH_FILES", str(managed_root))
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {
                "root_key": "file_agent_spreadsheet_patch_files",
                "path_prefix": "deploy",
            },
        )

        assert result.status == "COMPLETED"
        assert result.output_json["ok"] is True
        assert [file["relative_path"] for file in result.output_json["files"]] == [
            "deploy/a.ps1",
            "deploy/nested/b.txt",
        ]
        assert result.output_json["query"]["path_prefix"] == "deploy"
    finally:
        db.close()
        clear_overrides()


def test_env_managed_root_classification_mode_uses_parent_path(monkeypatch, tmp_path):
    """env 声明 PATH_AS_CATEGORY 时，父目录应自动作为受管文件分类路径。"""

    managed_root = tmp_path / "classified-library"
    category_dir = managed_root / "奖学金" / "国家励志奖学金"
    category_dir.mkdir(parents=True)
    (category_dir / "a.pdf").write_text("demo", encoding="utf-8")
    monkeypatch.setenv("MANAGED_ROOT_CLASSIFIED_LIBRARY", str(managed_root))
    monkeypatch.setenv("MANAGED_ROOT_CLASSIFIED_LIBRARY_CLASSIFICATION_MODE", "PATH_AS_CATEGORY")
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {"root_key": "classified_library"},
        )

        assert result.status == "COMPLETED"
        assert result.output_json["files"][0]["relative_path"] == "奖学金/国家励志奖学金/a.pdf"
        assert result.output_json["files"][0]["category_path"] == "奖学金/国家励志奖学金"
    finally:
        db.close()
        clear_overrides()


def test_managed_file_list_tool_filters_path_classified_categories():
    """managed-file-list Tool 应能按已分类目录和分类路径筛选。"""

    client, SessionLocal = client_with_database()
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
                relative_path="奖学金/国家励志奖学金/b.pdf",
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

        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {
                "root_key": "classified_library",
                "classification_mode": "PATH_AS_CATEGORY",
                "category_path": "奖学金/国家励志奖学金",
            },
        )

        assert result.status == "COMPLETED"
        assert [file["relative_path"] for file in result.output_json["files"]] == ["奖学金/国家励志奖学金/a.pdf"]
        assert result.output_json["files"][0]["category_path"] == "奖学金/国家励志奖学金"
    finally:
        db.close()
        clear_overrides()
