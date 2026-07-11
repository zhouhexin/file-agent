"""受管文件 Agent Tool 测试。"""

from datetime import datetime, timezone

from app.db.models import Document, DocumentPage, ManagedFile, ManagedRoot
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
    deep_dir = managed_root / "党办" / "2026" / "科学发展观" / "材料"
    deploy_dir.mkdir(parents=True)
    deep_dir.mkdir(parents=True)
    (managed_root / "README.md").write_text("root", encoding="utf-8")
    (managed_root / "deploy" / "a.ps1").write_text("deploy", encoding="utf-8")
    (deploy_dir / "b.txt").write_text("nested", encoding="utf-8")
    (deep_dir / "通知.pdf").write_text("deep", encoding="utf-8")
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

        deep_result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {
                "root_key": "file_agent_spreadsheet_patch_files",
                "path_prefix": "党办/2026/科学发展观",
            },
        )

        assert [file["relative_path"] for file in deep_result.output_json["files"]] == [
            "党办/2026/科学发展观/材料/通知.pdf",
        ]
        assert deep_result.output_json["query"]["path_prefix"] == "党办/2026/科学发展观"

        leaf_result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {
                "root_key": "file_agent_spreadsheet_patch_files",
                "path_prefix": "科学发展观",
            },
        )

        assert [file["relative_path"] for file in leaf_result.output_json["files"]] == [
            "党办/2026/科学发展观/材料/通知.pdf",
        ]
        assert leaf_result.output_json["query"]["path_prefix"] == "科学发展观"
    finally:
        db.close()
        clear_overrides()


def test_managed_file_list_tool_filters_extension_and_filename(monkeypatch, tmp_path):
    """managed-file-list Tool 应能组合扩展名和文件名关键字过滤。"""

    managed_root = tmp_path / "downloads"
    managed_root.mkdir()
    (managed_root / "电子发票.pdf").write_text("invoice", encoding="utf-8")
    (managed_root / "合同.pdf").write_text("contract", encoding="utf-8")
    (managed_root / "电子发票.xlsx").write_text("invoice table", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_DOWNLOADS", str(managed_root))
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        pdf_result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {"root_key": "downloads", "extension": "pdf"},
        )
        invoice_result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {"root_key": "downloads", "filename_contains": "发票"},
        )
        invoice_pdf_result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {"root_key": "downloads", "extension": "pdf", "filename_contains": "发票"},
        )

        assert [file["relative_path"] for file in pdf_result.output_json["files"]] == [
            "合同.pdf",
            "电子发票.pdf",
        ]
        assert [file["relative_path"] for file in invoice_result.output_json["files"]] == [
            "电子发票.pdf",
            "电子发票.xlsx",
        ]
        assert [file["relative_path"] for file in invoice_pdf_result.output_json["files"]] == [
            "电子发票.pdf",
        ]
    finally:
        db.close()
        clear_overrides()


def test_managed_file_read_document_tool_registers_snapshot_and_extracts_text(monkeypatch, tmp_path):
    """managed-file-read-document 应定位唯一受管文件，登记当前用户快照并写入 document_pages。"""

    managed_root = tmp_path / "downloads"
    target_dir = managed_root / "党办" / "2026"
    target_dir.mkdir(parents=True)
    (target_dir / "科学发展观材料.txt").write_text("科学发展观 文件正文", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_DOWNLOADS", str(managed_root))
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-read-document",
            {
                "root_key": "downloads",
                "path_prefix": "党办",
                "filename_contains": "科学发展观",
            },
        )

        assert result.status == "COMPLETED"
        assert result.output_json["ok"] is True
        assert result.output_json["status"] == "COMPLETED"
        assert result.output_json["managed_file"]["relative_path"] == "党办/2026/科学发展观材料.txt"
        assert result.output_json["pages"][0]["text_preview"] == "科学发展观 文件正文"

        document = db.get(Document, result.output_json["document_id"])
        assert document is not None
        assert document.user_id == "user-1"
        assert document.original_filename == "科学发展观材料.txt"
        page = db.query(DocumentPage).one()
        assert page.document_id == document.id
        assert page.text_content == "科学发展观 文件正文"
    finally:
        db.close()
        clear_overrides()


def test_managed_file_read_document_tool_reads_multiple_matches(monkeypatch, tmp_path):
    """managed-file-read-document 多命中时应批量快照和解析，不要求用户二次确认。"""

    managed_root = tmp_path / "downloads"
    target_dir = managed_root / "党办"
    target_dir.mkdir(parents=True)
    (target_dir / "科学发展观材料1.txt").write_text("第一份正文", encoding="utf-8")
    (target_dir / "科学发展观材料2.txt").write_text("第二份正文", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_DOWNLOADS", str(managed_root))
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-read-document",
            {
                "root_key": "downloads",
                "path_prefix": "党办",
                "filename_contains": "科学发展观",
            },
        )

        assert result.status == "COMPLETED"
        assert result.output_json["ok"] is True
        assert result.output_json["status"] == "COMPLETED"
        assert result.output_json["matched_count"] == 2
        assert [item["managed_file"]["relative_path"] for item in result.output_json["extraction_results"]] == [
            "党办/科学发展观材料1.txt",
            "党办/科学发展观材料2.txt",
        ]
        assert db.query(Document).count() == 2
        assert db.query(DocumentPage).count() == 2
    finally:
        db.close()
        clear_overrides()


def test_managed_file_list_tool_filters_keyword_in_filename_or_relative_path(monkeypatch, tmp_path):
    """年份等关键字应同时匹配文件名和相对路径，支持“党办 2026”组合条件。"""

    managed_root = tmp_path / "downloads"
    party_2026_dir = managed_root / "党办" / "2026"
    party_2025_dir = managed_root / "党办" / "2025"
    office_dir = managed_root / "教务处"
    party_2026_dir.mkdir(parents=True)
    party_2025_dir.mkdir(parents=True)
    office_dir.mkdir(parents=True)
    (party_2026_dir / "通知.pdf").write_text("notice", encoding="utf-8")
    (party_2025_dir / "通知.pdf").write_text("old notice", encoding="utf-8")
    (managed_root / "党办2026工作计划.docx").write_text("plan", encoding="utf-8")
    (office_dir / "2026通知.pdf").write_text("other", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_DOWNLOADS", str(managed_root))
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {"path_prefix": "党办", "filename_contains": "2026"},
        )

        assert result.status == "COMPLETED"
        assert [file["relative_path"] for file in result.output_json["files"]] == [
            "党办/2026/通知.pdf",
        ]
    finally:
        db.close()
        clear_overrides()


def test_managed_file_list_tool_treats_unknown_root_as_single_configured_subdirectory(monkeypatch, tmp_path):
    """只有一个 env 受管根时，未配置 root_key 应按该根下子目录解析。"""

    downloads_root = tmp_path / "Downloads"
    target_dir = downloads_root / "file_agent_spreadsheet_patch_files"
    target_dir.mkdir(parents=True)
    (downloads_root / "parent.xlsx").write_text("parent", encoding="utf-8")
    (downloads_root / ".DS_Store").write_text("hidden", encoding="utf-8")
    hidden_dir = downloads_root / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "secret.txt").write_text("hidden", encoding="utf-8")
    (target_dir / "README.md").write_text("readme", encoding="utf-8")
    (target_dir / "apply_spreadsheet_patch.ps1").write_text("patch", encoding="utf-8")
    (target_dir / ".DS_Store").write_text("hidden", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_DOWNLOADS", str(downloads_root))
    client, SessionLocal = client_with_database()
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

        result = ToolRegistry(db=db, user_id="user-1").invoke(
            "managed-file-list",
            {"root_key": "file_agent_spreadsheet_patch_files"},
        )

        assert result.status == "COMPLETED"
        assert result.output_json["query"]["root_key"] == "downloads"
        assert result.output_json["query"]["path_prefix"] == "file_agent_spreadsheet_patch_files"
        assert [file["relative_path"] for file in result.output_json["files"]] == [
            "file_agent_spreadsheet_patch_files/README.md",
            "file_agent_spreadsheet_patch_files/apply_spreadsheet_patch.ps1",
        ]
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


def test_managed_file_list_tool_filters_path_classified_categories(monkeypatch, tmp_path):
    """managed-file-list Tool 应能按已分类目录和分类路径筛选。"""

    classified_root_path = tmp_path / "classified-library"
    classified_file_dir = classified_root_path / "奖学金" / "国家励志奖学金"
    classified_file_dir.mkdir(parents=True)
    (classified_file_dir / "a.pdf").write_text("demo", encoding="utf-8")
    (classified_file_dir / ".DS_Store").write_text("hidden", encoding="utf-8")
    plain_root_path = tmp_path / "plain-inbox"
    plain_file_dir = plain_root_path / "奖学金" / "国家励志奖学金"
    plain_file_dir.mkdir(parents=True)
    (plain_file_dir / "b.pdf").write_text("demo", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_CLASSIFIED_LIBRARY", str(classified_root_path))
    monkeypatch.setenv("MANAGED_ROOT_CLASSIFIED_LIBRARY_CLASSIFICATION_MODE", "PATH_AS_CATEGORY")
    monkeypatch.setenv("MANAGED_ROOT_PLAIN_INBOX", str(plain_root_path))
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        classified_root = ManagedRoot(
            root_key="classified_library",
            display_name="已分类文件库",
            container_path=str(classified_root_path),
            classification_mode="PATH_AS_CATEGORY",
        )
        plain_root = ManagedRoot(
            root_key="plain_inbox",
            display_name="普通收件箱",
            container_path=str(plain_root_path),
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
