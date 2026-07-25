"""受管原始目录、工作副本目录和回收站目录完整生命周期测试。"""

from __future__ import annotations

from app.core import config
from app.db.models import (
    AgentRun,
    ChangeItem,
    Document,
    DocumentClassificationSummary,
    DocumentChunk,
    DocumentIndexRun,
    DocumentSearchProfile,
    DocumentSummary,
    DocumentVersion,
    EvidenceSpan,
    FileRenameReviewItem,
    ManagedFile,
    Message,
    TrashEntry,
    ToolInvocation,
    UploadArchiveRecord,
    UploadDuplicateReview,
    User,
    WorkingCopy,
    WorkingCopyPathRecord,
    Workspace,
)
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.file_rename.uploaded_suggestion_service import UploadedRenameSuggestionService
from app.modules.file_lifecycle.risk import inspect_basic_file_risks
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.worker import process_next_filesystem_job
from app.tests.helpers import clear_overrides, client_with_database


def _configure(monkeypatch, tmp_path) -> None:
    """配置测试专用三层目录。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    # 生命周期测试只验证确定性的本地 CPU 文件链路，不能继承 IDE 中启用的外部索引或图谱开关。
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("GRAPH_CLASSIFICATION_ENABLED", "false")
    monkeypatch.setenv("GRAPH_EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("NEO4J_SYNC_ENABLED", "false")
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


def _trash_working_copy(client, headers: dict[str, str], working_copy_id: str, conversation_id: str) -> None:
    """通过真实 OperationPlan 把指定工作副本移入回收站，禁止测试绕过确认链路。"""

    plan_response = client.post(
        "/api/operations/plans",
        headers=headers,
        json={
            "conversation_id": conversation_id,
            "operation_type": "TRASH_WORKING_COPIES",
            "reason": "测试用户明确删除文件",
            "items": [{"working_copy_id": working_copy_id}],
        },
    )
    assert plan_response.status_code == 200
    confirmation = client.post(
        f"/api/operations/plans/{plan_response.json()['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认移入回收站"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "EXECUTED"


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
        # 首次工作副本在 ACTIVE 前必须完成 CPU 原文索引，embedding 默认关闭。
        index_run = db.query(DocumentIndexRun).filter_by(document_version_id=version.id).one()
        assert index_run.status == "COMPLETED"
        assert index_run.embedding_status == "DISABLED"
        assert db.query(DocumentChunk).filter_by(document_version_id=version.id).count() >= 1
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
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "DOCUMENT_INDEX_CREATED").count() == 1
    finally:
        db.close()
        clear_overrides()


def test_existing_working_copy_repairs_missing_search_artifacts(monkeypatch, tmp_path):
    """历史物理副本存在时仍必须补建 Profile、Chunk 和 Evidence，不能幂等短路。"""

    diagnostic_events: list[str] = []

    def capture_log(event: str, **_kwargs) -> None:
        """捕获诊断事件，验证部署现场能定位扫描、解析和索引补建阶段。"""

        diagnostic_events.append(event)

    monkeypatch.setattr(
        "app.modules.file_lifecycle.service.log_event",
        capture_log,
    )
    monkeypatch.setattr(
        "app.modules.managed_files.worker.log_event",
        capture_log,
    )
    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "search-repair-owner")
    upload = _upload(
        client,
        headers,
        filename="干部面谈名单.txt",
        content="面谈人员包括金海燕老师。".encode("utf-8"),
    )
    _drain(SessionLocal)

    db = SessionLocal()
    try:
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        working_copy = db.query(WorkingCopy).filter_by(
            managed_file_id=archive.managed_file_id,
        ).one()
        working_copy_id = working_copy.id
        document = db.get(Document, working_copy.document_id)
        version_id = working_copy.current_version_id

        # 模拟阶段四上线前已经存在物理工作副本、但检索派生数据完全缺失的历史状态。
        chunk_ids = [
            row.id
            for row in db.query(DocumentChunk.id).filter_by(
                document_version_id=version_id,
            )
        ]
        if chunk_ids:
            db.query(EvidenceSpan).filter(EvidenceSpan.chunk_id.in_(chunk_ids)).delete(
                synchronize_session=False
            )
        db.query(DocumentChunk).filter_by(document_version_id=version_id).delete(
            synchronize_session=False
        )
        db.query(DocumentIndexRun).filter_by(document_version_id=version_id).delete(
            synchronize_session=False
        )
        db.query(DocumentSearchProfile).filter_by(
            working_copy_id=working_copy.id,
        ).delete(synchronize_session=False)

        FilesystemJobQueue(db).create_job(
            job_type="SCAN_MANAGED_ROOT",
            queue_name="SCAN",
            root_id=archive.managed_root_id,
            created_by=document.user_id,
            deduplication_key=f"search-artifact-repair-scan:{archive.managed_root_id}",
            payload={"reason": "test-search-artifact-repair"},
        )
        db.commit()
    finally:
        db.close()

    diagnostic_events.clear()
    _drain(SessionLocal)

    db = SessionLocal()
    try:
        assert (
            db.query(DocumentSearchProfile)
            .filter_by(working_copy_id=working_copy_id, status="ACTIVE")
            .count()
            == 1
        )
        assert (
            db.query(DocumentIndexRun)
            .filter_by(document_version_id=version_id, status="COMPLETED")
            .count()
            == 1
        )
        assert db.query(DocumentChunk).filter_by(document_version_id=version_id).count() >= 1
        assert "working_copy.search_repair.queued" in diagnostic_events
        assert "working_copy.search_repair.started" in diagnostic_events
        assert "working_copy.search_repair.index_started" in diagnostic_events
        assert "working_copy.search_repair.profile_started" in diagnostic_events
        assert "working_copy.search_repair.completed" in diagnostic_events
    finally:
        db.close()
        clear_overrides()


def test_each_uploaded_file_is_imported_once_into_shared_working_directory(monkeypatch, tmp_path):
    """多个用户上传不同文件时只能创建一个共享工作区和每文件一份物理副本。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    first_headers = _auth(client, "shared-first-owner")
    second_headers = _auth(client, "shared-second-owner")
    first = _upload(client, first_headers, "first-shared.txt", b"first shared body")
    second = _upload(client, second_headers, "second-shared.txt", b"second shared body")
    _drain(SessionLocal)

    db = SessionLocal()
    try:
        copies = db.query(WorkingCopy).order_by(WorkingCopy.filename.asc()).all()
        shared_workspaces = db.query(Workspace).filter(Workspace.workspace_type == "SYSTEM_SHARED").all()
        assert len(copies) == 2
        assert len(shared_workspaces) == 1
        assert {copy.workspace_id for copy in copies} == {shared_workspaces[0].id}
        assert all(version.storage_path.startswith("shared/upload_archive/") for version in (
            db.get(DocumentVersion, copy.current_version_id) for copy in copies
        ))
        assert first["upload_document_version_id"]
        assert second["upload_document_version_id"]
    finally:
        db.close()


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


def test_duplicate_upload_reports_deleted_match_and_requires_explicit_reupload(monkeypatch, tmp_path):
    """相同内容只命中已删除文件时必须询问是否再次上传，不能自动复活或合并。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "deleted-duplicate-owner")
    first = _upload(client, headers, "已删除通知.txt", b"same deleted content")
    _drain(SessionLocal)
    first_copy = client.get("/api/working-copies", headers=headers).json()[0]
    _trash_working_copy(client, headers, first_copy["id"], "deleted-duplicate-conv")

    second = _upload(client, headers, "再次上传通知.txt", b"same deleted content")
    process_next_filesystem_job(session_factory=SessionLocal, worker_id="deleted-duplicate-test")
    review_response = client.get(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review",
        headers=headers,
    )

    assert review_response.status_code == 200
    review = review_response.json()
    assert review["status"] == "WAITING_CONFIRMATION"
    assert review["allowed_decisions"] == ["CONTINUE_UPLOAD", "CANCEL_UPLOAD"]
    assert review["candidates"][0]["summary"]["file_status"] == "TRASHED"
    assert "此前已删除" in review["candidates"][0]["summary"]["message"]
    assert review["candidates"][0]["existing_working_copy_id"] is None

    decision = client.post(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review/decision",
        headers=headers,
        json={
            "duplicate_review_id": review["id"],
            "decision": "CONTINUE_UPLOAD",
            "selected_existing_working_copy_id": None,
        },
    )
    assert decision.status_code == 202
    _drain(SessionLocal)
    copies = client.get("/api/working-copies", headers=headers).json()
    assert sorted(item["status"] for item in copies) == ["ACTIVE", "TRASHED"]
    # 用户选择再次上传后形成全新工作副本，已删除副本仍保留在回收站。
    assert len({item["id"] for item in copies}) == 2
    assert client.get("/api/trash-entries", headers=headers).json()[0]["status"] == "ACTIVE"
    assert first["document_id"] != second["document_id"]
    clear_overrides()


def test_exact_filename_search_returns_each_same_version_trash_candidate(monkeypatch, tmp_path):
    """完整文件名命中多条同名同版本回收站记录时必须逐条返回并等待单选。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "exact-trash-owner")
    _upload(client, headers, "面谈名单.txt", b"same-version-placeholder")
    _drain(SessionLocal)
    first_copy = client.get("/api/working-copies", headers=headers).json()[0]
    _trash_working_copy(client, headers, first_copy["id"], "exact-trash-conv")

    first_trash_entry_id = ""
    db = SessionLocal()
    try:
        original = db.get(WorkingCopy, first_copy["id"])
        first_entry = db.query(TrashEntry).filter_by(working_copy_id=original.id, status="ACTIVE").one()
        first_trash_entry_id = first_entry.id
        # 构造第二个同名、同 DocumentVersion、同哈希的已删除工作副本，验证查询不能合并。
        duplicate_copy = WorkingCopy(
            working_copy_root_id=original.working_copy_root_id,
            workspace_id=original.workspace_id,
            managed_file_id=original.managed_file_id,
            document_id=original.document_id,
            current_version_id=original.current_version_id,
            relative_path="待确认/duplicate/面谈名单.txt",
            relative_path_hash="d" * 64,
            filename=original.filename,
            extension=original.extension,
            size_bytes=original.size_bytes,
            content_sha256=original.content_sha256,
            imported_source_sha256=original.imported_source_sha256,
            is_primary_import=False,
            status="TRASHED",
            sync_status="SYNCED",
        )
        db.add(duplicate_copy)
        db.flush()
        db.add(
            TrashEntry(
                workspace_id=original.workspace_id,
                working_copy_id=duplicate_copy.id,
                document_version_id=first_entry.document_version_id,
                entry_type="DELETED",
                original_relative_path=duplicate_copy.relative_path,
                trash_relative_path=f"{original.workspace_id}/{duplicate_copy.id}/same/面谈名单.txt",
                status="ACTIVE",
                deleted_by=first_entry.deleted_by,
                deleted_at=first_entry.deleted_at,
                retention_until=first_entry.retention_until,
            )
        )
        db.commit()
    finally:
        db.close()

    broad_search = client.post(
        "/api/conversations/exact-trash-conv/messages",
        headers=headers,
        json={"content": "查找有关面谈的文件", "attachments": []},
    )
    assert broad_search.status_code == 200
    # 主题或关键词检索不能越过 ACTIVE 边界读取回收站。
    assert broad_search.json()["task_result"]["response_type"] != "trash_restore_selection"

    response = client.post(
        "/api/conversations/exact-trash-conv/messages",
        headers=headers,
        json={"content": "查找《面谈名单.txt》", "attachments": []},
    )

    assert response.status_code == 200
    receipt = response.json()["task_result"]
    assert receipt["response_type"] == "trash_restore_selection"
    assert receipt["task_status"] == "needs_attention"
    selection = receipt["trash_restore_result"]
    assert selection["query_type"] == "EXACT_FILENAME"
    assert selection["requires_selection"] is True
    assert len(selection["candidates"]) == 2
    assert [item["display_index"] for item in selection["candidates"]] == [1, 2]
    assert {item["filename"] for item in selection["candidates"]} == {"面谈名单.txt"}
    assert len({item["trash_entry_id"] for item in selection["candidates"]}) == 2
    assert len({item["version_number"] for item in selection["candidates"]}) == 1
    # 回收站候选不能混入普通文件搜索卡，也不能在用户选择前自动创建恢复计划。
    assert receipt["file_search_result"] is None
    assert receipt["operation_plan_id"] is None

    selected = next(
        item for item in selection["candidates"]
        if item["trash_entry_id"] == first_trash_entry_id
    )
    restore_plan = client.post(
        f"/api/trash-entries/{selected['trash_entry_id']}/restore-plan",
        headers=headers,
        json={"conversation_id": "exact-trash-conv"},
    )
    assert restore_plan.status_code == 200
    restored = client.post(
        f"/api/operations/plans/{restore_plan.json()['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认恢复所选文件"},
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "EXECUTED"
    # 单选只恢复被选择项，其余同名同版本候选继续留在回收站。
    copies = client.get("/api/working-copies", headers=headers).json()
    assert sorted(item["status"] for item in copies) == ["ACTIVE", "TRASHED"]

    active_wins = client.post(
        "/api/conversations/exact-trash-conv/messages",
        headers=headers,
        json={"content": "查找《面谈名单.txt》", "attachments": []},
    )
    assert active_wins.status_code == 200
    # 已有同名活动副本时只返回普通活动文件结果，不再混入同名历史删除项。
    assert active_wins.json()["task_result"]["response_type"] == "file_search_results"
    assert active_wins.json()["task_result"]["trash_restore_result"] is None
    clear_overrides()


def test_duplicate_upload_decision_is_audited_but_hidden_from_chat(monkeypatch, tmp_path):
    """重复上传内部枚举必须保留完整审计，但不能进入普通用户对话历史。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "duplicate-chat-projection-owner")
    _upload(client, headers, "first.txt", b"identical chat projection body")
    _drain(SessionLocal)
    second_response = client.post(
        "/api/files/upload",
        headers=headers,
        data={"conversation_id": "duplicate-chat-projection"},
        files={"file": ("second.txt", b"identical chat projection body", "text/plain")},
    )
    assert second_response.status_code == 202
    second = second_response.json()

    process_next_filesystem_job(session_factory=SessionLocal, worker_id="duplicate-chat-test")
    review_response = client.get(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review",
        headers=headers,
    )
    assert review_response.status_code == 200
    review = review_response.json()
    existing_copy_id = review["candidates"][0]["existing_working_copy_id"]

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
    history = client.get(
        "/api/conversations/duplicate-chat-projection",
        headers=headers,
    )
    assert history.status_code == 200
    assert history.json()["messages"] == []

    db = SessionLocal()
    try:
        audit_messages = (
            db.query(Message)
            .filter(Message.conversation_id == "duplicate-chat-projection")
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
        assert len(audit_messages) == 3
        assert {message.role for message in audit_messages} == {"SYSTEM_AUDIT"}
        assert any("重复上传处理：USE_EXISTING_FILE" in message.content for message in audit_messages)
        assert any("已记录重复上传决策：USE_EXISTING_FILE" in message.content for message in audit_messages)

        decision_item = (
            db.query(ChangeItem)
            .filter(ChangeItem.change_type == "UPLOAD_DUPLICATE_DECISION_RECORDED")
            .one()
        )
        decision_run = (
            db.query(AgentRun)
            .filter(AgentRun.changeset_id == decision_item.changeset_id)
            .one()
        )
        invocation = (
            db.query(ToolInvocation)
            .filter(ToolInvocation.agent_run_id == decision_run.id)
            .one()
        )
        assert invocation.tool_name == "upload-duplicate-decision-record"
        assert invocation.output_json["decision"] == "USE_EXISTING_FILE"

        # 模拟升级前数据库中的旧 role，确保无需清库也不会重新显示截图中的内部文本和已完成卡片。
        for message in audit_messages:
            if message.content.startswith("重复上传处理："):
                message.role = "user"
            else:
                message.role = "assistant"
        db.commit()
    finally:
        db.close()

    legacy_history = client.get(
        "/api/conversations/duplicate-chat-projection",
        headers=headers,
    )
    assert legacy_history.status_code == 200
    assert legacy_history.json()["messages"] == []
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


def test_initial_ready_rename_is_only_suggestion_until_user_requests_rename(monkeypatch, tmp_path):
    """首次导入即使命名建议可用，也必须保留上传名且不得自动改工作副本。"""

    _configure(monkeypatch, tmp_path)
    original_suggest = UploadedRenameSuggestionService.suggest_for_initial_import

    def force_ready_suggestion(self, *, document):
        """固定一个可执行候选，保护“建议不等于用户授权”的边界。"""

        suggestion, extraction = original_suggest(self, document=document)
        return {
            **suggestion,
            "status": "READY",
            "proposed_filename": "2026_研究成果资助汇总表.txt",
            "warnings": [],
            "errors": [],
        }, extraction

    monkeypatch.setattr(
        UploadedRenameSuggestionService,
        "suggest_for_initial_import",
        force_ready_suggestion,
    )
    client, SessionLocal = client_with_database()
    headers = _auth(client, "rename-suggestion-owner")
    upload = client.post(
        "/api/files/upload",
        headers=headers,
        data={"conversation_id": "rename-suggestion-conv"},
        files={
            "file": (
                "2024科研成果资助汇总表.txt",
                b"research funding summary fixture",
                "text/plain",
            )
        },
    )
    assert upload.status_code == 202

    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    history = client.get("/api/conversations/rename-suggestion-conv", headers=headers).json()
    task_result = history["messages"][-1]["task_result"]

    assert working_copy["filename"] == "2024科研成果资助汇总表.txt"
    assert task_result["document_results"][0]["filename"] == "2024科研成果资助汇总表.txt"
    assert task_result["document_results"][0]["rename_suggestion"] == {
        "proposed_filename": "2026_研究成果资助汇总表.txt"
    }
    pending = task_result["pending_decisions"][0]
    assert pending["reason"] == "RENAME_SUGGESTION_AVAILABLE"
    assert pending["proposed_filename"] == "2026_研究成果资助汇总表.txt"
    # 用户仅在后续明确提出改名时，才允许创建待确认计划；此刻仍不得改动工作副本。
    rename_request = client.post(
        "/api/conversations/rename-suggestion-conv/messages",
        headers=headers,
        json={"content": "改名", "attachments": []},
    )
    assert rename_request.status_code == 200
    rename_receipt = rename_request.json()["task_result"]
    assert rename_receipt["response_type"] == "operation_plan"
    assert rename_receipt["operation_plan_id"]
    assert client.get("/api/working-copies", headers=headers).json()[0]["filename"] == "2024科研成果资助汇总表.txt"
    db = SessionLocal()
    try:
        working_document = db.get(Document, working_copy["document_id"])
        original = db.query(ManagedFile).one()
        path_record = db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=working_copy["id"],
            operation_type="INITIAL_IMPORT",
        ).one()
        assert working_document.original_filename == "2024科研成果资助汇总表.txt"
        assert original.filename == "2024科研成果资助汇总表.txt"
        assert path_record.after_filename == "2024科研成果资助汇总表.txt"
        assert db.query(FileRenameReviewItem).filter_by(document_id=working_copy["document_id"]).count() == 0
    finally:
        db.close()
        clear_overrides()


def test_initial_filename_conflict_waits_for_dialog_without_version_suffix(monkeypatch, tmp_path):
    """不同内容使用同一上传名时必须等待对话决策，不能自动追加版本后缀。"""

    _configure(monkeypatch, tmp_path)
    original_suggest = UploadedRenameSuggestionService.suggest_for_initial_import

    def force_same_name(self, *, document):
        """保留建议生成，验证建议不会参与首次物理命名或制造冲突。"""

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
        files={"file": ("同名材料.txt", b"first unique material", "text/plain")},
    )
    _drain(SessionLocal)
    client.post(
        "/api/files/upload",
        headers=headers,
        data={"conversation_id": "filename-conflict-conv"},
        files={"file": ("同名材料.txt", b"second completely different content", "text/plain")},
    )
    _drain(SessionLocal)

    copies = client.get("/api/working-copies", headers=headers).json()
    assert sorted(item["filename"] for item in copies) == ["同名材料.txt", "同名材料.txt"]
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
    assert pending["target_filename"] == "同名材料.txt"
    assert pending["allowed_decisions"] == [
        "KEEP_BOTH",
        "KEEP_EXISTING",
        "REPLACE_EXISTING_WORKING_COPY",
        "DELETE_EXISTING_WORKING_COPY",
    ]

    # 用户通过普通消息选择同时保留时只能生成计划；确认前仍不得分配版本后缀。
    decision_response = client.post(
        "/api/conversations/filename-conflict-conv/messages",
        headers=headers,
        json={"content": "这两个文件同时保留", "attachments": []},
    )
    assert decision_response.status_code == 200
    decision_receipt = decision_response.json()["task_result"]
    assert decision_receipt["response_type"] == "operation_plan"
    assert decision_receipt["operation_plan_id"]
    before_confirmation = client.get("/api/working-copies", headers=headers).json()
    assert not any("第二版" in item["filename"] for item in before_confirmation)
    confirmation = client.post(
        f"/api/operations/plans/{decision_receipt['operation_plan_id']}/confirm",
        headers=headers,
        json={"confirmation": "确认同时保留"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "EXECUTED"
    after_confirmation = client.get("/api/working-copies", headers=headers).json()
    assert sorted(item["filename"] for item in after_confirmation) == [
        "同名材料.txt",
        "同名材料_第二版.txt",
    ]

    # 替换选择必须先把已有工作副本移入可恢复回收站，再提升新副本；原件仍不被覆盖。
    client.post(
        "/api/files/upload",
        headers=headers,
        data={"conversation_id": "filename-conflict-conv"},
        files={"file": ("同名材料.txt", b"third replacement material", "text/plain")},
    )
    _drain(SessionLocal)
    replace_response = client.post(
        "/api/conversations/filename-conflict-conv/messages",
        headers=headers,
        json={"content": "用新文件替换已有文件", "attachments": []},
    )
    replace_plan_id = replace_response.json()["task_result"]["operation_plan_id"]
    replace_confirmation = client.post(
        f"/api/operations/plans/{replace_plan_id}/confirm",
        headers=headers,
        json={"confirmation": "确认替换"},
    )
    assert replace_confirmation.json()["status"] == "EXECUTED"
    final_copies = client.get("/api/working-copies", headers=headers).json()
    assert sorted(item["filename"] for item in final_copies if item["status"] == "ACTIVE") == [
        "同名材料.txt",
        "同名材料_第二版.txt",
    ]
    assert sum(item["status"] == "TRASHED" for item in final_copies) == 1
    assert len(client.get("/api/trash-entries", headers=headers).json()) == 1
    db = SessionLocal()
    try:
        reviews = [
            item
            for item in db.query(FileRenameReviewItem).all()
            if item.review_context_json.get("reason") == "FILENAME_CONFLICT"
        ]
        assert len(reviews) == 2
        assert all(review.status == "EXECUTED" for review in reviews)
    finally:
        db.close()
        clear_overrides()


def test_chat_creates_and_confirms_trash_then_restore_plans(monkeypatch, tmp_path):
    """普通用户必须从对话生成回收站和恢复计划，确认前不得发生物理副作用。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "chat-trash-owner")
    upload = _upload(client, headers, "待删除通知.txt", b"chat trash and restore")
    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]

    context_message = client.post(
        "/api/conversations/chat-trash-conv/messages",
        headers=headers,
        json={
            "content": "读取这个文件",
            "attachments": [{"document_id": upload["document_id"]}],
        },
    )
    assert context_message.status_code == 200

    trash_message = client.post(
        "/api/conversations/chat-trash-conv/messages",
        headers=headers,
        json={
            "content": "删除待删除通知",
            "attachments": [],
        },
    )
    assert trash_message.status_code == 200
    trash_receipt = trash_message.json()["task_result"]
    assert trash_receipt["response_type"] == "operation_plan"
    assert client.get(f"/api/working-copies/{working_copy['id']}", headers=headers).json()["status"] == "ACTIVE"
    trash_confirmation = client.post(
        f"/api/operations/plans/{trash_receipt['operation_plan_id']}/confirm",
        headers=headers,
        json={"confirmation": "确认移入回收站"},
    )
    assert trash_confirmation.json()["status"] == "EXECUTED"
    assert client.get(f"/api/working-copies/{working_copy['id']}", headers=headers).json()["status"] == "TRASHED"
    trashed_history = client.get(
        "/api/conversations/chat-trash-conv",
        headers=headers,
    )
    assert trashed_history.status_code == 200
    historical_attachment = next(
        message["attachments"][0]
        for message in trashed_history.json()["messages"]
        if message["content"] == "读取这个文件"
    )
    # 历史卡片必须保留，但要投影最新回收站状态，禁止继续伪装成可打开文件。
    assert historical_attachment["working_copy_status"] == "TRASHED"
    assert historical_attachment["file_availability"] == "TRASHED"
    assert historical_attachment["availability_message"] == "已删除（在回收站，可恢复）"
    assert historical_attachment["can_open"] is False
    assert historical_attachment["can_restore"] is True

    repeated_delete = client.post(
        "/api/conversations/chat-trash-conv/messages",
        headers=headers,
        json={"content": "删除待删除通知", "attachments": []},
    )
    assert repeated_delete.status_code == 200
    assert "已经在回收站" in repeated_delete.json()["task_result"]["final_response"]

    restore_message = client.post(
        "/api/conversations/chat-trash-conv/messages",
        headers=headers,
        json={"content": "恢复刚才删除的文件", "attachments": []},
    )
    assert restore_message.status_code == 200
    restore_receipt = restore_message.json()["task_result"]
    assert restore_receipt["response_type"] == "operation_plan"
    assert client.get(f"/api/working-copies/{working_copy['id']}", headers=headers).json()["status"] == "TRASHED"
    restore_confirmation = client.post(
        f"/api/operations/plans/{restore_receipt['operation_plan_id']}/confirm",
        headers=headers,
        json={"confirmation": "确认恢复"},
    )
    assert restore_confirmation.json()["status"] == "EXECUTED"
    assert client.get(f"/api/working-copies/{working_copy['id']}", headers=headers).json()["status"] == "ACTIVE"
    restored_history = client.get(
        "/api/conversations/chat-trash-conv",
        headers=headers,
    )
    restored_attachment = next(
        message["attachments"][0]
        for message in restored_history.json()["messages"]
        if message["content"] == "读取这个文件"
    )
    assert restored_attachment["working_copy_status"] == "ACTIVE"
    assert restored_attachment["file_availability"] == "AVAILABLE"
    assert restored_attachment["can_open"] is True
    assert restored_attachment["can_restore"] is False

    # 数据库仍为 ACTIVE 但受控工作目录文件缺失时必须标记异常，不能误报为已删除。
    with SessionLocal() as db:
        restored_copy = db.get(WorkingCopy, working_copy["id"])
        assert restored_copy is not None
        restored_version = db.get(DocumentVersion, restored_copy.current_version_id)
        assert restored_version is not None
        (tmp_path / "working" / restored_version.storage_path).unlink()
    missing_history = client.get(
        "/api/conversations/chat-trash-conv",
        headers=headers,
    )
    missing_attachment = next(
        message["attachments"][0]
        for message in missing_history.json()["messages"]
        if message["content"] == "读取这个文件"
    )
    assert missing_attachment["working_copy_status"] == "ACTIVE"
    assert missing_attachment["file_availability"] == "MISSING"
    assert "工作目录文件不存在" in missing_attachment["availability_message"]
    assert missing_attachment["can_open"] is False
    assert missing_attachment["can_restore"] is False
    clear_overrides()


def test_chat_colloquial_removal_resolves_latest_file_and_only_creates_plan(monkeypatch, tmp_path):
    """常见删除口语必须解析到刚上传文件，但每次都只能创建待确认计划而不能直接移动文件。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "chat-trash-synonym-owner")
    upload = _upload(client, headers, "口语删除测试.txt", b"colloquial trash intent")
    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]

    seed_message = client.post(
        "/api/conversations/chat-trash-synonym-conv/messages",
        headers=headers,
        json={
            "content": "读取这个文件",
            "attachments": [{"document_id": upload["document_id"]}],
        },
    )
    assert seed_message.status_code == 200

    for content in [
        "删除刚刚上传的文件",
        "把刚才上传的附件删掉",
        "这个文件我不要了",
        "把它删了",
    ]:
        response = client.post(
            "/api/conversations/chat-trash-synonym-conv/messages",
            headers=headers,
            json={"content": content, "attachments": []},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["message"]["attachments"] == [{"document_id": upload["document_id"]}]
        assert data["task_result"]["response_type"] == "operation_plan"
        assert data["task_result"]["operation_plan_id"]
        # 多种口语都只能停在确认前，不能因为识别成功就直接产生物理副作用。
        assert client.get(
            f"/api/working-copies/{working_copy['id']}",
            headers=headers,
        ).json()["status"] == "ACTIVE"

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
