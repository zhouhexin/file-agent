"""上传暂存、异步生命周期和附件删除边界测试。"""

from __future__ import annotations

from app.core import config
from app.db.models import (
    Document,
    DocumentArtifact,
    DocumentVersion,
    FileObject,
    FilesystemJob,
    UploadArchiveRecord,
    UploadDuplicateReview,
)
from app.modules.managed_files.worker import process_next_filesystem_job
from app.modules.files.extraction_repository import FileExtractionRepository
from app.tests.helpers import clear_overrides, client_with_database


def _auth_header(client, username: str = "file-user") -> dict[str, str]:
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


def _configure_storage(monkeypatch, tmp_path) -> None:
    """把三层存储全部隔离到当前测试目录。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    config.get_settings.cache_clear()


def _drain_jobs(SessionLocal, *, maximum: int = 20) -> list[str]:
    """同步驱动测试数据库中的 worker；生产环境仍由独立进程消费。"""

    processed: list[str] = []
    for _ in range(maximum):
        job_id = process_next_filesystem_job(session_factory=SessionLocal, worker_id="test-worker")
        if job_id is None:
            break
        processed.append(job_id)
    return processed


def test_upload_creates_version_review_and_persistent_job(monkeypatch, tmp_path):
    """上传请求只保存暂存和创建任务，不得同步归档或导入。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    response = client.post(
        "/api/files/upload",
        headers=_auth_header(client),
        files={"file": ("student.xlsx", b"student-file-content", "application/vnd.ms-excel")},
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "UPLOADED"
    assert data["ingest_status"] == "DUPLICATE_CHECK_PENDING"
    assert data["deduplicated"] is False
    assert data["upload_document_version_id"]
    assert data["duplicate_review_id"]
    assert data["filesystem_job_id"]

    db = SessionLocal()
    try:
        document = db.get(Document, data["document_id"])
        version = db.get(DocumentVersion, data["upload_document_version_id"])
        review = db.get(UploadDuplicateReview, data["duplicate_review_id"])
        archive = db.query(UploadArchiveRecord).filter_by(upload_document_version_id=version.id).one()
        job = db.get(FilesystemJob, data["filesystem_job_id"])
        file_object = db.query(FileObject).filter_by(document_id=document.id).one()
        assert version.storage_tier == "UPLOAD"
        assert review.status == "CHECKING"
        assert archive.status == "DUPLICATE_CHECK_PENDING"
        assert job.status == "PENDING"
        assert (tmp_path / "uploads" / file_object.storage_path).read_bytes() == b"student-file-content"
        assert not (tmp_path / "originals").exists()
        assert not (tmp_path / "working").exists()
    finally:
        db.close()
        clear_overrides()


def test_same_content_uploads_remain_distinct_until_dialog_decision(monkeypatch, tmp_path):
    """同内容上传不能在请求线程静默复用 Document 或物理暂存文件。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth_header(client, "distinct-upload-owner")
    first = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": ("first.txt", b"same-content", "text/plain")},
    )
    second = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": ("second.txt", b"same-content", "text/plain")},
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["document_id"] != second.json()["document_id"]
    db = SessionLocal()
    try:
        objects = db.query(FileObject).order_by(FileObject.created_at.asc()).all()
        assert len(objects) == 2
        assert objects[0].storage_path != objects[1].storage_path
    finally:
        db.close()
        clear_overrides()


def test_upload_rejects_unsupported_extension_before_creating_document(monkeypatch, tmp_path):
    """上传格式白名单必须在落盘前生效，不能接收可执行脚本。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    response = client.post(
        "/api/files/upload",
        headers=_auth_header(client, "unsupported-upload-owner"),
        files={"file": ("run.sh", b"echo unsafe", "application/x-sh")},
    )

    assert response.status_code == 415
    db = SessionLocal()
    try:
        assert db.query(Document).count() == 0
    finally:
        db.close()
        clear_overrides()


def test_upload_resource_limit_cleans_incoming_file(monkeypatch, tmp_path):
    """部署级资源上限超出时必须清理分块暂存，且不能留下业务记录。"""

    _configure_storage(monkeypatch, tmp_path)
    monkeypatch.setenv("UPLOAD_MAX_FILE_SIZE_MB", "1")
    monkeypatch.setenv("UPLOAD_CHUNK_SIZE_BYTES", str(64 * 1024))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    response = client.post(
        "/api/files/upload",
        headers=_auth_header(client, "large-upload-owner"),
        files={"file": ("large.pdf", b"x" * (1024 * 1024 + 1), "application/pdf")},
    )

    assert response.status_code == 413
    incoming_dir = tmp_path / "uploads" / ".incoming"
    assert list(incoming_dir.glob("*.part")) == []
    db = SessionLocal()
    try:
        assert db.query(Document).count() == 0
    finally:
        db.close()
        clear_overrides()


def test_get_file_content_enforces_owner(monkeypatch, tmp_path):
    """暂存附件读取必须校验所属用户，不能因为内容相同跨用户共享路径。"""

    _configure_storage(monkeypatch, tmp_path)
    client, _ = client_with_database()
    owner = _auth_header(client, "content-owner")
    viewer = _auth_header(client, "content-viewer")
    upload = client.post(
        "/api/files/upload",
        headers=owner,
        files={"file": ("preview.txt", b"preview-content", "text/plain")},
    )
    document_id = upload.json()["document_id"]

    own_response = client.get(f"/api/files/{document_id}/content", headers=owner)
    cross_response = client.get(f"/api/files/{document_id}/content", headers=viewer)

    assert own_response.status_code == 200
    assert own_response.content == b"preview-content"
    assert cross_response.status_code == 404
    clear_overrides()


def test_structured_artifact_download_restricts_owner_and_artifact_type(monkeypatch, tmp_path):
    """通用派生件 ID 不能借结构化下载路由读取其他类型或其他用户文件。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    owner = _auth_header(client, "artifact-owner")
    viewer = _auth_header(client, "artifact-viewer")
    upload = client.post(
        "/api/files/upload",
        headers=owner,
        files={"file": ("source.png", b"image-content", "image/png")},
    )
    document_id = upload.json()["document_id"]
    artifact_dir = tmp_path / "uploads" / "derivatives"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    structured_path = artifact_dir / "result.csv"
    structured_path.write_text("name\nvalue\n", encoding="utf-8")
    preview_path = artifact_dir / "preview.txt"
    preview_path.write_text("internal preview", encoding="utf-8")
    db = SessionLocal()
    try:
        structured = DocumentArtifact(
            id="structured-artifact",
            document_id=document_id,
            artifact_type="STRUCTURED_EXTRACTION_CSV",
            storage_path="derivatives/result.csv",
            content_type="text/csv; charset=utf-8",
            size_bytes=structured_path.stat().st_size,
            sha256="a" * 64,
            source_sha256="b" * 64,
            converter_name="structured-extraction-export",
            converter_version="1",
            converter_config_hash="c" * 64,
        )
        preview = DocumentArtifact(
            id="preview-artifact",
            document_id=document_id,
            artifact_type="PREVIEW_TEXT",
            storage_path="derivatives/preview.txt",
            content_type="text/plain",
            size_bytes=preview_path.stat().st_size,
            sha256="d" * 64,
            source_sha256="b" * 64,
            converter_name="preview",
            converter_version="1",
            converter_config_hash="e" * 64,
        )
        db.add_all([structured, preview])
        db.commit()
        structured_artifact_id = structured.id
        preview_artifact_id = preview.id
    finally:
        db.close()

    own_response = client.get(
        f"/api/files/{document_id}/artifacts/{structured_artifact_id}",
        headers=owner,
    )
    wrong_type_response = client.get(
        f"/api/files/{document_id}/artifacts/{preview_artifact_id}",
        headers=owner,
    )
    cross_user_response = client.get(
        f"/api/files/{document_id}/artifacts/{structured_artifact_id}",
        headers=viewer,
    )

    assert own_response.status_code == 200
    assert own_response.content == structured_path.read_bytes()
    assert wrong_type_response.status_code == 404
    assert cross_user_response.status_code == 404
    clear_overrides()


def test_get_file_preview_returns_extracted_pages_and_enforces_owner(monkeypatch, tmp_path):
    """文件卡正文预览必须读取已解析页面，并拒绝其他用户访问。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    owner = _auth_header(client, "preview-page-owner")
    viewer = _auth_header(client, "preview-page-viewer")
    upload = client.post(
        "/api/files/upload",
        headers=owner,
        files={
            "file": (
                "分类材料.docx",
                b"docx-placeholder",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    document_id = upload.json()["document_id"]
    db = SessionLocal()
    try:
        document = db.get(Document, document_id)
        repository = FileExtractionRepository(db, document.user_id)
        run = repository.create_extraction_run(
            document_id=document_id,
            extractor="test-extractor",
        )
        repository.complete_extraction_run(
            run=run,
            pages=[
                {"page_number": 1, "text": "第一页面正文"},
                {"page_number": 2, "text": "第二页面正文"},
            ],
        )
        db.commit()
    finally:
        db.close()

    own_response = client.get(f"/api/files/{document_id}/preview", headers=owner)
    cross_response = client.get(f"/api/files/{document_id}/preview", headers=viewer)

    assert own_response.status_code == 200
    assert own_response.json()["filename"] == "分类材料.docx"
    assert own_response.json()["sections"] == [
        {"page_number": 1, "sheet_name": None, "text": "第一页面正文"},
        {"page_number": 2, "sheet_name": None, "text": "第二页面正文"},
    ]
    assert cross_response.status_code == 404
    clear_overrides()


def test_get_spreadsheet_preview_returns_persisted_cell_region(monkeypatch, tmp_path):
    """Excel 结构化预览必须分页返回真实坐标，并沿用文档访问控制。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    owner = _auth_header(client, "spreadsheet-preview-owner")
    viewer = _auth_header(client, "spreadsheet-preview-viewer")
    upload = client.post(
        "/api/files/upload",
        headers=owner,
        files={
            "file": (
                "统计表.xlsx",
                b"xlsx-placeholder",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    ).json()
    db = SessionLocal()
    try:
        document = db.get(Document, upload["document_id"])
        repository = FileExtractionRepository(db, document.user_id)
        run = repository.create_extraction_run(document_id=document.id, extractor="excel")
        repository.complete_extraction_run(
            run=run,
            pages=[{
                "page_number": 1,
                "sheet_name": "汇总",
                "text": "姓名\t金额",
                "metadata": {
                    "max_row": 20,
                    "max_column": 3,
                    "merged_ranges": ["A1:C1"],
                    "hidden_rows": [],
                    "hidden_columns": [],
                    "freeze_panes": "B2",
                    "structure_complete": True,
                },
            }],
            elements=[{
                "element_index": 0,
                "label": "table_cell",
                "text": "120.00",
                "page_number": 1,
                "metadata": {
                    "sheet_name": "汇总",
                    "row": 2,
                    "column": 2,
                    "address": "B2",
                    "raw_value": 120,
                    "display_value": "120.00",
                    "value_type": "number",
                    "number_format": "0.00",
                },
            }],
        )
        db.commit()
    finally:
        db.close()

    response = client.get(
        f"/api/files/{upload['document_id']}/spreadsheet-preview?sheet_name=汇总&row_offset=1&row_limit=5",
        headers=owner,
    )
    denied = client.get(
        f"/api/files/{upload['document_id']}/spreadsheet-preview",
        headers=viewer,
    )

    assert response.status_code == 200
    assert response.json()["selected_sheet"]["cells"][0]["address"] == "B2"
    assert response.json()["selected_sheet"]["freeze_panes"] == "B2"
    assert denied.status_code == 404
    clear_overrides()


def test_delete_unsent_upload_cancels_lifecycle_and_cleans_asynchronously(monkeypatch, tmp_path):
    """未发送附件可删除；保留审计记录，但物理暂存由 worker 异步清理。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth_header(client, "delete-owner")
    upload = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": ("delete-me.png", b"image-content", "image/png")},
    )
    document_id = upload.json()["document_id"]
    db = SessionLocal()
    try:
        file_object = db.query(FileObject).filter_by(document_id=document_id).one()
        stored_path = tmp_path / "uploads" / file_object.storage_path
        assert stored_path.exists()
    finally:
        db.close()

    deleted = client.delete(f"/api/files/{document_id}", headers=headers)

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["cleanup_job_id"]
    assert stored_path.exists()
    _drain_jobs(SessionLocal)
    assert not stored_path.exists()
    db = SessionLocal()
    try:
        # Document/版本承担取消审计，不因清理暂存而被物理删除。
        assert db.get(Document, document_id).status == "UPLOAD_CANCELLED"
        review = db.query(UploadDuplicateReview).filter_by(
            upload_document_version_id=upload.json()["upload_document_version_id"]
        ).one()
        assert review.decision == "CANCEL_UPLOAD"
    finally:
        db.close()
        clear_overrides()


def test_shared_active_working_copy_preview_is_readable_by_other_user(
    monkeypatch, tmp_path
):
    """归档后的共享活动工作副本可由任意登录用户预览，上传暂存仍保持私有。"""

    _configure_storage(monkeypatch, tmp_path)
    client, _SessionLocal = client_with_database()
    owner = _auth_header(client, "shared-preview-owner")
    viewer = _auth_header(client, "shared-preview-viewer")
    upload = client.post(
        "/api/files/upload",
        headers=owner,
        files={"file": ("共享通知.txt", "共享正文内容".encode(), "text/plain")},
    )
    assert upload.status_code == 202
    _drain_jobs(_SessionLocal)
    copies = client.get("/api/working-copies", headers=viewer).json()
    shared = next(item for item in copies if item["filename"] == "共享通知.txt")

    preview = client.get(
        f"/api/files/{shared['document_id']}/preview",
        headers=viewer,
    )
    content = client.get(
        f"/api/files/{shared['document_id']}/content",
        headers=viewer,
    )

    assert preview.status_code == 200
    assert "共享正文内容" in "".join(
        section["text"] for section in preview.json()["sections"]
    )
    assert content.status_code == 200
    assert content.content == "共享正文内容".encode()
    clear_overrides()


def test_shared_active_working_copy_cannot_use_private_upload_delete_api(
    monkeypatch, tmp_path
):
    """共享文件可读不代表可绕过 OperationPlan 使用暂存删除接口。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    owner = _auth_header(client, "shared-delete-owner")
    viewer = _auth_header(client, "shared-delete-viewer")
    upload = client.post(
        "/api/files/upload",
        headers=owner,
        files={"file": ("共享删除边界.txt", "受保护正文".encode(), "text/plain")},
    )
    assert upload.status_code == 202
    _drain_jobs(SessionLocal)
    copies = client.get("/api/working-copies", headers=viewer).json()
    shared = next(item for item in copies if item["filename"] == "共享删除边界.txt")

    cross_delete = client.delete(
        f"/api/files/{shared['document_id']}",
        headers=viewer,
    )
    owner_direct_delete = client.delete(
        f"/api/files/{shared['document_id']}",
        headers=owner,
    )
    content = client.get(
        f"/api/files/{shared['document_id']}/content",
        headers=viewer,
    )

    assert cross_delete.status_code == 404
    # 工作副本 Document 仍记录最初创建人，但状态已经不是上传草稿；
    # 所有者也只能得到冲突响应，不能借暂存删除接口改变共享文件。
    assert owner_direct_delete.status_code == 409
    assert content.status_code == 200
    assert content.content == "受保护正文".encode()
    clear_overrides()


def test_trashed_shared_working_copy_content_is_not_readable_by_owner_or_viewer(
    monkeypatch, tmp_path
):
    """共享文件移入回收站后，创建者也不能绕过状态读取原件或正文预览。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    owner = _auth_header(client, "trashed-content-owner")
    viewer = _auth_header(client, "trashed-content-viewer")
    upload = client.post(
        "/api/files/upload",
        headers=owner,
        files={"file": ("已删除共享通知.txt", "不可继续读取".encode(), "text/plain")},
    )
    assert upload.status_code == 202
    _drain_jobs(SessionLocal)
    shared = next(
        item
        for item in client.get("/api/working-copies", headers=viewer).json()
        if item["filename"] == "已删除共享通知.txt"
    )
    context = client.post(
        "/api/conversations/trashed-content-conv/messages",
        headers=owner,
        json={
            "content": "读取这个文件",
            "attachments": [{"document_id": shared["document_id"]}],
        },
    )
    assert context.status_code == 200
    delete_request = client.post(
        "/api/conversations/trashed-content-conv/messages",
        headers=owner,
        json={"content": "删除已删除共享通知", "attachments": []},
    )
    assert delete_request.status_code == 200
    plan_id = delete_request.json()["task_result"]["operation_plan_id"]
    confirmation = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=owner,
        json={"confirmation": "确认移入回收站"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "EXECUTED"

    for headers in (owner, viewer):
        assert (
            client.get(
                f"/api/files/{shared['document_id']}/content",
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/files/{shared['document_id']}/preview",
                headers=headers,
            ).status_code
            == 404
        )
    clear_overrides()


def test_delete_file_after_message_is_rejected(monkeypatch, tmp_path):
    """附件真正进入消息后必须保留引用，不能再作为未发送暂存删除。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth_header(client, "locked-owner")
    upload = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": ("locked.png", b"locked-image", "image/png")},
    )
    document_id = upload.json()["document_id"]
    sent = client.post(
        "/api/conversations/locked-conv/messages",
        headers=headers,
        json={"content": "处理这张图片", "attachments": [{"document_id": document_id}]},
    )

    assert sent.status_code == 200
    deleted = client.delete(f"/api/files/{document_id}", headers=headers)
    assert deleted.status_code == 409
    db = SessionLocal()
    try:
        document = db.get(Document, document_id)
        assert document.status == "USED_IN_MESSAGE"
        assert document.locked_message_id == sent.json()["message"]["id"]
    finally:
        db.close()
        clear_overrides()
