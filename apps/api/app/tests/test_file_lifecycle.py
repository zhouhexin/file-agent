"""受管原始目录、工作副本目录和回收站目录完整生命周期测试。"""

from __future__ import annotations

from app.core import config
from app.db.models import (
    ChangeItem,
    Document,
    DocumentClassificationSummary,
    DocumentSummary,
    DocumentVersion,
    FileRenameReviewItem,
    ManagedFile,
    TrashEntry,
    UploadArchiveRecord,
    UploadDuplicateReview,
    User,
    WorkingCopy,
    WorkingCopyPathRecord,
)
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.file_rename.uploaded_suggestion_service import UploadedRenameSuggestionService
from app.modules.file_lifecycle.risk import inspect_basic_file_risks
from app.modules.managed_files.worker import process_next_filesystem_job
from app.tests.helpers import clear_overrides, client_with_database


def _configure(monkeypatch, tmp_path) -> None:
    """配置测试专用三层目录。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    config.get_settings.cache_clear()


def _auth(client, username: str) -> dict[str, str]:
    """注册测试用户并返回认证头。"""

    client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    token = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _upload(client, headers, filename: str = "2024年度通知.txt", content: bytes = b"annual notice") -> dict:
    """上传一个测试附件。"""

    response = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": (filename, content, "text/plain")},
    )
    assert response.status_code == 202
    return response.json()


def _drain(SessionLocal, maximum: int = 30) -> list[str]:
    """在测试进程中驱动独立 worker 逻辑直至当前队列为空。"""

    job_ids: list[str] = []
    for _ in range(maximum):
        job_id = process_next_filesystem_job(session_factory=SessionLocal, worker_id="lifecycle-test")
        if job_id is None:
            break
        job_ids.append(job_id)
    return job_ids


def test_upload_is_archived_then_imported_by_separate_jobs(monkeypatch, tmp_path):
    """查重、归档和导入必须串联为三个持久化任务并建立完整追溯关系。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "pipeline-owner")
    upload = _upload(client, headers)

    processed = _drain(SessionLocal)

    assert len(processed) == 3
    status = client.get(
        f"/api/uploads/{upload['upload_document_version_id']}/archive-status",
        headers=headers,
    )
    assert status.status_code == 200
    assert status.json()["status"] == "ARCHIVED"
    assert status.json()["managed_file_id"]
    assert status.json()["working_copy_id"]
    db = SessionLocal()
    try:
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        original = db.get(ManagedFile, archive.managed_file_id)
        working_copy = db.get(WorkingCopy, status.json()["working_copy_id"])
        version = db.get(DocumentVersion, working_copy.current_version_id)
        original_path = tmp_path / "originals" / original.relative_path
        working_path = tmp_path / "working" / version.storage_path
        assert original.source_type == "UPLOAD_ARCHIVE"
        assert original.source_upload_version_id == upload["upload_document_version_id"]
        assert original_path.read_bytes() == b"annual notice"
        assert working_path.read_bytes() == b"annual notice"
        assert original_path != working_path
        assert working_copy.managed_file_id == original.id
        assert version.source_managed_file_id == original.id
        assert ".internal" not in version.storage_path
        assert db.query(DocumentSummary).filter_by(document_id=working_copy.document_id).count() == 1
        assert db.query(DocumentClassificationSummary).filter_by(document_id=working_copy.document_id).count() == 1
        initial_path = db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=working_copy.id,
            operation_type="INITIAL_IMPORT",
        ).one()
        assert initial_path.after_filename == working_copy.filename
        source_document = db.get(Document, upload["document_id"])
        resolved = UploadedRenameSuggestionService(
            db=db,
            user_id=source_document.user_id,
        )._resolve_working_copy(source_document=source_document)
        # 上传附件重命名必须先穿过归档关系，最终只得到活动工作副本。
        assert resolved is not None
        assert resolved.id == working_copy.id
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "ORIGINAL_FILE_ARCHIVED").count() == 1
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "WORKING_COPY_IMPORTED").count() == 1
    finally:
        db.close()
        clear_overrides()


def test_duplicate_upload_waits_for_dialog_and_can_use_existing(monkeypatch, tmp_path):
    """发现同工作区重复内容时必须暂停归档，由用户选择已有工作副本。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "duplicate-owner")
    first = _upload(client, headers, "first.txt", b"identical body")
    _drain(SessionLocal)
    second = _upload(client, headers, "second.txt", b"identical body")

    process_next_filesystem_job(session_factory=SessionLocal, worker_id="duplicate-test")
    review_response = client.get(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review",
        headers=headers,
    )

    assert review_response.status_code == 200
    review = review_response.json()
    assert review["status"] == "WAITING_CONFIRMATION"
    assert "USE_EXISTING_FILE" in review["allowed_decisions"]
    assert review["candidates"][0]["match_type"] == "EXACT_SHA256"
    existing_copy_id = review["candidates"][0]["existing_working_copy_id"]
    existing_document_id = review["candidates"][0]["existing_document_id"]
    decision = client.post(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review/decision",
        headers=headers,
        json={
            "duplicate_review_id": review["id"],
            "decision": "USE_EXISTING_FILE",
            "selected_existing_working_copy_id": existing_copy_id,
        },
    )
    assert decision.status_code == 202
    assert decision.json()["selected_existing_document_id"] == existing_document_id
    assert decision.json()["archive_status"] == "EXISTING_FILE_SELECTED"
    db = SessionLocal()
    try:
        second_archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=second["upload_document_version_id"]
        ).one()
        assert second_archive.managed_file_id is None
        assert db.query(ManagedFile).count() == 1
        assert db.query(WorkingCopy).count() == 1
    finally:
        db.close()
        clear_overrides()


def test_low_confidence_initial_name_keeps_upload_name_and_returns_pending_receipt(monkeypatch, tmp_path):
    """低置信度首次命名必须保留上传名，并在普通回执中请求自然语言确认。"""

    _configure(monkeypatch, tmp_path)
    original_suggest = UploadedRenameSuggestionService.suggest_for_initial_import

    def force_needs_review(self, *, document):
        """复用真实解析结果，仅把命名质量门禁固定为待确认。"""

        suggestion, extraction = original_suggest(self, document=document)
        return {
            **suggestion,
            "status": "NEEDS_REVIEW",
            "proposed_filename": None,
            "warnings": ["测试固定为低置信度"],
        }, extraction

    monkeypatch.setattr(
        UploadedRenameSuggestionService,
        "suggest_for_initial_import",
        force_needs_review,
    )
    client, SessionLocal = client_with_database()
    headers = _auth(client, "low-confidence-owner")
    upload = client.post(
        "/api/files/upload",
        headers=headers,
        data={"conversation_id": "low-confidence-conv"},
        files={"file": ("原上传名称.txt", b"2026 annual scholarship material", "text/plain")},
    ).json()

    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    history = client.get("/api/conversations/low-confidence-conv", headers=headers).json()
    task_result = history["messages"][-1]["task_result"]

    assert working_copy["filename"] == "原上传名称.txt"
    assert task_result["task_status"] == "needs_attention"
    assert task_result["processed_count"] == 1
    assert task_result["document_results"][0]["working_copy_id"] == working_copy["id"]
    assert task_result["document_results"][0]["filename"] == "原上传名称.txt"
    assert task_result["pending_decisions"][0]["reason"] == "LOW_CONFIDENCE_RENAME"
    db = SessionLocal()
    try:
        review = db.query(FileRenameReviewItem).filter_by(document_id=working_copy["document_id"]).one()
        assert review.status == "NEEDS_REVIEW"
        assert review.review_context_json["reason"] == "LOW_CONFIDENCE_RENAME"
        assert db.get(Document, upload["document_id"]).original_filename == "原上传名称.txt"
    finally:
        db.close()
        clear_overrides()


def test_initial_filename_conflict_waits_for_dialog_without_version_suffix(monkeypatch, tmp_path):
    """不同内容得到同一目标名时必须保留新文件上传名，不能自动追加版本后缀。"""

    _configure(monkeypatch, tmp_path)
    original_suggest = UploadedRenameSuggestionService.suggest_for_initial_import

    def force_same_name(self, *, document):
        """固定两个文件的高置信度目标名，同时保留真实解析链路。"""

        suggestion, extraction = original_suggest(self, document=document)
        return {
            **suggestion,
            "status": "READY",
            "proposed_filename": "2026_统一材料.txt",
            "warnings": [],
            "errors": [],
        }, extraction

    def force_same_category(self, **_kwargs):
        """让两个文件落入同一受控 taxonomy 路径以触发真实路径冲突。"""

        return {
            "status": "COMPLETED",
            "categories": [
                {
                    "name": "奖助学金",
                    "category_id": "student-affairs.scholarship",
                    "category_path": ["学生工作", "奖助学金"],
                    "confidence": 0.95,
                    "status": "SUGGESTED",
                    "source": "rule",
                    "evidence_items": [{"type": "text_quote", "quote": "材料"}],
                }
            ],
            "summary_status": "FULL_TEXT_FALLBACK",
        }

    monkeypatch.setattr(
        UploadedRenameSuggestionService,
        "suggest_for_initial_import",
        force_same_name,
    )
    monkeypatch.setattr(
        "app.modules.file_lifecycle.organizer.DocumentClassificationService.classify",
        force_same_category,
    )
    client, SessionLocal = client_with_database()
    headers = _auth(client, "filename-conflict-owner")
    client.post(
        "/api/files/upload",
        headers=headers,
        data={"conversation_id": "filename-conflict-conv"},
        files={"file": ("第一份.txt", b"first unique material", "text/plain")},
    )
    _drain(SessionLocal)
    client.post(
        "/api/files/upload",
        headers=headers,
        data={"conversation_id": "filename-conflict-conv"},
        files={"file": ("第二份.txt", b"second completely different content", "text/plain")},
    )
    _drain(SessionLocal)

    copies = client.get("/api/working-copies", headers=headers).json()
    assert sorted(item["filename"] for item in copies) == ["2026_统一材料.txt", "第二份.txt"]
    assert not any("第二版" in item["filename"] for item in copies)
    history = client.get("/api/conversations/filename-conflict-conv", headers=headers).json()
    conflict_receipts = [
        message["task_result"]
        for message in history["messages"]
        if message.get("task_result")
        and message["task_result"].get("pending_decisions")
        and message["task_result"]["pending_decisions"][0].get("reason") == "FILENAME_CONFLICT"
    ]
    assert len(conflict_receipts) == 1
    pending = conflict_receipts[0]["pending_decisions"][0]
    assert pending["target_filename"] == "2026_统一材料.txt"
    assert pending["allowed_decisions"] == [
        "KEEP_BOTH",
        "KEEP_EXISTING",
        "REPLACE_EXISTING_WORKING_COPY",
        "DELETE_EXISTING_WORKING_COPY",
    ]
    db = SessionLocal()
    try:
        review = next(
            item
            for item in db.query(FileRenameReviewItem).all()
            if item.review_context_json.get("reason") == "FILENAME_CONFLICT"
        )
        assert review.status == "NEEDS_REVIEW"
    finally:
        db.close()
        clear_overrides()


def test_macro_risk_is_reported_without_claiming_virus_scan(tmp_path):
    """宏格式只做风险提示且绝不执行，病毒扫描状态必须明确为未实现。"""

    macro_file = tmp_path / "含宏表.xlsm"
    macro_file.write_bytes(b"macro-container-placeholder")

    assessment = inspect_basic_file_risks(
        file_path=macro_file,
        filename=macro_file.name,
        content_type="application/vnd.ms-excel.sheet.macroenabled.12",
    )

    assert assessment.status == "WARNING"
    assert assessment.macro_risk is True
    assert assessment.virus_scan_status == "NOT_IMPLEMENTED"
    assert any(item["code"] == "OFFICE_MACRO_RISK" for item in assessment.warnings)


def test_encrypted_pdf_archives_original_but_stops_before_working_copy(monkeypatch, tmp_path):
    """加密文件必须保护不可变原件并进入待复核，系统不得尝试破解或创建工作副本。"""

    import fitz

    _configure(monkeypatch, tmp_path)
    encrypted_path = tmp_path / "encrypted.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "encrypted material")
    document.save(
        encrypted_path,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    document.close()
    encrypted_bytes = encrypted_path.read_bytes()
    client, SessionLocal = client_with_database()
    headers = _auth(client, "encrypted-file-owner")
    upload = client.post(
        "/api/files/upload",
        headers=headers,
        data={"conversation_id": "encrypted-file-conv"},
        files={"file": ("加密材料.pdf", encrypted_bytes, "application/pdf")},
    ).json()

    processed = _drain(SessionLocal)

    assert len(processed) == 2
    status = client.get(
        f"/api/uploads/{upload['upload_document_version_id']}/archive-status",
        headers=headers,
    ).json()
    assert status["status"] == "NEEDS_REVIEW"
    assert status["working_copy_id"] is None
    history = client.get("/api/conversations/encrypted-file-conv", headers=headers).json()
    task_result = history["messages"][-1]["task_result"]
    assert task_result["task_status"] == "needs_attention"
    assert task_result["pending_decisions"][0]["reason"] == "ENCRYPTED_FILE"
    db = SessionLocal()
    try:
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        original = db.get(ManagedFile, archive.managed_file_id)
        assert archive.risk_assessment_json["encrypted"] is True
        assert archive.risk_assessment_json["virus_scan_status"] == "NOT_IMPLEMENTED"
        assert (tmp_path / "originals" / original.relative_path).read_bytes() == encrypted_bytes
        assert db.query(WorkingCopy).count() == 0
    finally:
        db.close()
        clear_overrides()


def test_cross_user_duplicate_candidate_is_sanitized(monkeypatch, tmp_path):
    """跨用户重复候选只能提示存在相同内容，不能暴露文件名、路径或业务 ID。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    first_headers = _auth(client, "private-owner")
    second_headers = _auth(client, "other-uploader")
    _upload(client, first_headers, "机密姓名名单.txt", b"cross user duplicate")
    _drain(SessionLocal)
    second = _upload(client, second_headers, "copy.txt", b"cross user duplicate")
    process_next_filesystem_job(session_factory=SessionLocal, worker_id="cross-user-test")

    review = client.get(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review",
        headers=second_headers,
    ).json()

    candidate = review["candidates"][0]
    assert candidate["match_scope"] == "CROSS_USER"
    assert candidate["existing_working_copy_id"] is None
    assert candidate["existing_document_id"] is None
    assert "filename" not in candidate["summary"]
    assert "relative_path" not in candidate["summary"]
    assert review["allowed_decisions"] == ["CONTINUE_UPLOAD", "CANCEL_UPLOAD"]
    clear_overrides()


def test_rename_move_trash_and_restore_only_change_working_copy(monkeypatch, tmp_path):
    """路径操作不新增版本、不改原始文件；删除进入回收站且可恢复。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "operation-owner")
    upload = _upload(client, headers, "04级工程硕士开课通知.doc", b"legacy document")
    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]

    rename_plan = client.post(
        "/api/operations/plans",
        headers=headers,
        json={
            "conversation_id": "working-copy-operations",
            "operation_type": "RENAME_WORKING_COPIES",
            "reason": "规范文件名",
            "items": [
                {
                    "working_copy_id": working_copy["id"],
                    "after": {"filename": "2004级工程硕士开课通知.doc"},
                    "rename_metadata": {"policy_key": "school-file-rename", "year": {"value": "2004"}},
                }
            ],
        },
    )
    assert rename_plan.status_code == 200
    assert rename_plan.json()["items"][0]["rename_metadata"]["year"]["value"] == "2004"
    rename_result = client.post(
        f"/api/operations/plans/{rename_plan.json()['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认重命名"},
    )
    assert rename_result.status_code == 200
    assert rename_result.json()["status"] == "EXECUTED"

    renamed = client.get(f"/api/working-copies/{working_copy['id']}", headers=headers).json()
    assert renamed["filename"] == "2004级工程硕士开课通知.doc"
    assert "2004" in renamed["relative_path"]
    versions = client.get(
        f"/api/working-copies/{working_copy['id']}/versions",
        headers=headers,
    ).json()
    assert len(versions) == 1

    trash_plan = client.post(
        "/api/operations/plans",
        headers=headers,
        json={
            "conversation_id": "working-copy-operations",
            "operation_type": "TRASH_WORKING_COPIES",
            "reason": "用户请求删除工作副本",
            "items": [{"working_copy_id": working_copy["id"]}],
        },
    ).json()
    trash_result = client.post(
        f"/api/operations/plans/{trash_plan['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认移入回收站"},
    )
    assert trash_result.json()["status"] == "EXECUTED"
    trash_entries = client.get("/api/trash-entries", headers=headers).json()
    assert len(trash_entries) == 1

    restore_plan = client.post(
        f"/api/trash-entries/{trash_entries[0]['id']}/restore-plan",
        headers=headers,
        json={"conversation_id": "working-copy-operations"},
    ).json()
    restored = client.post(
        f"/api/operations/plans/{restore_plan['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认恢复"},
    )
    assert restored.json()["status"] == "EXECUTED"

    db = SessionLocal()
    try:
        copy = db.get(WorkingCopy, working_copy["id"])
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        original = db.get(ManagedFile, archive.managed_file_id)
        records = db.query(WorkingCopyPathRecord).filter_by(working_copy_id=copy.id).all()
        work_document = db.get(Document, copy.document_id)
        assert copy.status == "ACTIVE"
        assert work_document.original_filename == "2004级工程硕士开课通知.doc"
        assert db.query(DocumentVersion).filter_by(document_id=copy.document_id).count() == 1
        assert len(records) == 2
        rename_record = sorted(records, key=lambda item: item.sequence_number)[-1]
        assert rename_record.status == "COMPLETED"
        assert rename_record.after_filename == "2004级工程硕士开课通知.doc"
        assert (tmp_path / "originals" / original.relative_path).read_bytes() == b"legacy document"
        assert db.query(TrashEntry).filter_by(working_copy_id=copy.id, status="RESTORED").count() == 1
    finally:
        db.close()
        clear_overrides()


def test_confirmed_file_action_tool_executes_persisted_working_copy_plan(monkeypatch, tmp_path):
    """Agent Tool 必须执行真实工作副本计划，并返回可追踪 ChangeSet。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "tool-operation-owner")
    _upload(client, headers, "待整理通知.txt", b"controlled rename body")
    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    plan = client.post(
        "/api/operations/plans",
        headers=headers,
        json={
            "conversation_id": "tool-confirmed-operation",
            "operation_type": "RENAME_WORKING_COPIES",
            "reason": "验证 Tool 真实执行入口",
            "items": [
                {
                    "working_copy_id": working_copy["id"],
                    "after": {"filename": "已整理通知.txt"},
                }
            ],
        },
    ).json()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "tool-operation-owner").one()
        invocation = ToolRegistry(db=db, user_id=user.id).invoke(
            "confirmed-file-action",
            {
                "operation_plan_id": plan["id"],
                "confirmation_text": "确认重命名工作副本",
            },
        )

        assert invocation.status == "COMPLETED"
        assert invocation.output_json["status"] == "EXECUTED"
        assert invocation.changeset_id
    finally:
        db.close()

    renamed = client.get(f"/api/working-copies/{working_copy['id']}", headers=headers).json()
    assert renamed["filename"] == "已整理通知.txt"
    clear_overrides()
