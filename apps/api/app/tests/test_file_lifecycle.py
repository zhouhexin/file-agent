"""受管原始目录、工作副本目录和回收站目录完整生命周期测试。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.core import config
from app.db.models import (
    AgentRun,
    ChangeItem,
    Document,
    DocumentClassificationSummary,
    DocumentCategory,
    DocumentCategorySuggestion,
    DocumentChunk,
    DocumentClassificationRun,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentOrganizationDecision,
    DocumentSearchProfile,
    DocumentSummary,
    DocumentVersion,
    EvidenceSpan,
    FileObject,
    FileRenameReviewItem,
    ManagedFile,
    ManagedRoot,
    Message,
    OperationConfirmation,
    OperationPlan,
    TrashEntry,
    ToolInvocation,
    UploadArchiveRecord,
    UploadDuplicateReview,
    User,
    WorkingCopy,
    WorkingCopyPathRecord,
    WorkingCopyRoot,
    Workspace,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.file_rename.uploaded_suggestion_service import UploadedRenameSuggestionService
from app.modules.file_lifecycle.organizer import (
    InitialOrganizationDecision,
    rename_metadata_for_initial_organization,
)
from app.modules.file_lifecycle.risk import inspect_basic_file_risks
from app.modules.file_lifecycle.layout_repair import WorkingCopyLayoutRepairService
from app.modules.file_lifecycle.service import (
    FileLifecycleJobProcessor,
    working_copy_search_artifact_status,
)
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.files.extraction_repository import FileExtractionRepository
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
    # 普通生命周期用例必须隔离开发机 .env 中可能启用的首次自动落位；
    # 专项用例会在调用本辅助函数后显式开启并重新加载配置。
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "false")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "false")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "true")
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


def _start_upload(client, headers, upload: dict) -> None:
    """模拟用户点击发送，启动暂存附件处理。"""

    response = client.post(
        f"/api/uploads/{upload['upload_document_version_id']}/process",
        headers=headers,
    )
    assert response.status_code == 202


def _upload(client, headers, filename: str = "2024年度通知.txt", content: bytes = b"annual notice") -> dict:
    """上传测试附件并模拟点击发送。"""

    response = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": (filename, content, "text/plain")},
    )
    assert response.status_code == 202
    upload = response.json()
    _start_upload(client, headers, upload)
    return upload


def _drain(SessionLocal, maximum: int = 30) -> list[str]:
    """在测试进程中驱动独立 worker 逻辑直至当前队列为空。"""

    job_ids: list[str] = []
    for _ in range(maximum):
        job_id = process_next_filesystem_job(session_factory=SessionLocal, worker_id="lifecycle-test")
        if job_id is None:
            break
        job_ids.append(job_id)
    return job_ids


@pytest.mark.parametrize(
    ("year_filename", "expected_filename"),
    [
        (
            "2022_西安理工疫控组发〔2022〕2号_疫情防控工作会议纪要.pdf",
            "20220115_西安理工疫控组发〔2022〕2号_疫情防控工作会议纪要.pdf",
        ),
        (
            "2022_西安理工大学新冠肺炎疫情防控工作会议纪要.pdf",
            "20220115_西安理工大学新冠肺炎疫情防控工作会议纪要.pdf",
        ),
    ],
)
def test_initial_organization_year_collision_uses_full_date(
    year_filename: str,
    expected_filename: str,
):
    """受管目录首次落位发生年份名称冲突时，应保留文号和标题并升级完整日期。"""

    rename_metadata = rename_metadata_for_initial_organization(
        {
            "document_date": {"value": "20220115"},
            "year": {"value": "2022"},
            "document_number": {"value": None},
            "title": {"value": "疫情防控工作会议纪要"},
            "proposed_filename": year_filename,
        }
    )
    decision = InitialOrganizationDecision(
        filename="原文件.pdf",
        extraction_result={"status": "COMPLETED"},
        categories=[],
        primary_category=None,
        document_summary_id=None,
        classification_summary_id=None,
        summary_status="REUSED",
        rename_status="READY",
        rename_metadata=rename_metadata,
        summary_metadata={},
    )

    assert FileLifecycleJobProcessor._full_date_collision_filename(
        decision=decision,
        filename=year_filename,
    ) == expected_filename


def test_managed_source_image_date_overrides_scoped_other_fallback():
    """学院/其他只是拒识兜底，受管图片仍应按源文件修改日期归档。"""

    assert FileLifecycleJobProcessor._managed_source_image_date_fallback_needed(
        categories=[
            {
                "category_id": "college.other",
                "category_path": ["学院", "其他"],
                "source": "rule_fallback",
            }
        ],
        policy_result=type("PolicyResult", (), {"accepted": True})(),
    )


def test_managed_source_container_path_excludes_uploaded_archives():
    """仅外部受管源保留原父目录，上传归档不能带入 uploads 容器。"""

    assert FileLifecycleJobProcessor._managed_source_container_path(
        ManagedFile(relative_path="外来应聘/2026/张三/个人简历.pdf")
    ) == Path("外来应聘/2026/张三")
    assert FileLifecycleJobProcessor._managed_source_container_path(
        ManagedFile(
            relative_path="uploads/2026/09/个人简历.pdf",
            source_upload_version_id="upload-version",
        )
    ) == Path()


def test_personal_resume_initial_organization_template_is_explicit():
    """只有简历专用模板才能启用首次落位的自动版本后缀。"""

    decision = InitialOrganizationDecision(
        filename="王青龙.doc",
        extraction_result={"status": "COMPLETED"},
        categories=[],
        primary_category=None,
        document_summary_id=None,
        classification_summary_id=None,
        summary_status="REUSED",
        rename_status="READY",
        rename_metadata={
            "template_key": "personal_resume",
            "proposed_filename": "王青龙_个人简历.doc",
        },
        summary_metadata={},
    )

    assert FileLifecycleJobProcessor._is_personal_resume_rename(decision) is True
    decision.rename_metadata["template_key"] = "title_only"
    assert FileLifecycleJobProcessor._is_personal_resume_rename(decision) is False


def test_personal_resume_initial_collision_uses_second_version(monkeypatch, tmp_path):
    """同一最终目录已有个人简历时自动分配第二版，不覆盖也不进入待复核。"""

    _configure(monkeypatch, tmp_path)
    _client, session_factory = client_with_database()
    db = session_factory()
    try:
        processor = FileLifecycleJobProcessor(db)
        working_root = WorkingCopyRoot(
            id="resume-working-root",
            relative_storage_path="shared/resume-test",
        )
        working_copy = WorkingCopy(id="pending-resume-copy")
        version = DocumentVersion(
            storage_path="staging/pending-resume.doc",
            sha256="a" * 64,
        )
        target_parent = Path("学院/人事师资/师资招聘/2015/王青龙")
        base_path = processor.storage.working_copy_path(
            f"{working_root.relative_storage_path}/"
            f"{target_parent.as_posix()}/王青龙_个人简历.doc"
        )
        base_path.parent.mkdir(parents=True, exist_ok=True)
        base_path.write_bytes(b"existing resume")

        relative_path = processor._available_initial_resume_relative_path(
            working_copy=working_copy,
            working_root=working_root,
            version=version,
            target_parent=target_parent,
            target_filename="王青龙_个人简历.doc",
        )

        assert relative_path.endswith("/王青龙_个人简历_第二版.doc")
        assert base_path.read_bytes() == b"existing resume"
    finally:
        db.close()
        clear_overrides()


def _png_bytes(color: str) -> bytes:
    """生成可由后端真实容器校验识别的测试 PNG，不能只伪造 MIME。"""

    buffer = BytesIO()
    Image.new("RGB", (32, 24), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


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
    """查重、归档、快速导入和后台分析必须串联为四个持久化任务。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "pipeline-owner")
    upload = _upload(client, headers)

    processed = _drain(SessionLocal)

    assert len(processed) == 4
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
        # 快速导入先创建 ACTIVE 工作副本，随后由独立 ANALYSIS 任务补齐 CPU 原文索引。
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
        archive_messages = db.query(Message).filter(
            Message.content.like("%的原件已归档，正在创建工作副本。")
        ).all()
        assert archive_messages
        assert all(message.role == "SYSTEM_AUDIT" for message in archive_messages)
    finally:
        db.close()


def test_default_upload_is_classified_then_first_published_to_taxonomy_path(monkeypatch, tmp_path):
    """高可靠新上传必须从隐藏 ORGANIZING 一次发布到 taxonomy 主分类目录。"""

    _configure(monkeypatch, tmp_path)
    monkeypatch.delenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", raising=False)
    monkeypatch.delenv("AUTO_INITIAL_PLACEMENT_ENABLED", raising=False)
    monkeypatch.delenv("AUTO_CLASSIFICATION_SHADOW_MODE", raising=False)
    monkeypatch.setenv("AUTO_CLASSIFICATION_FALLBACK_MARGIN", "0.01")
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    headers = _auth(client, "auto-placement-owner")
    filename = "学校会议纪要研究决定议题.md"
    content = "学校会议纪要。会议围绕议题进行研究，研究决定通过有关事项。".encode()
    upload = _upload(client, headers, filename=filename, content=content)

    processed = [
        process_next_filesystem_job(
            session_factory=SessionLocal,
            worker_id="lifecycle-test",
        )
        for _ in range(3)
    ]
    db = SessionLocal()
    try:
        organizing = db.query(WorkingCopy).one()
        assert organizing.status == "ORGANIZING"
        assert ".internal" in organizing.relative_path
        assert all(
            item["id"] != organizing.id
            for item in client.get("/api/working-copies", headers=headers).json()
        )
        assert client.get(
            f"/api/working-copies/{organizing.id}", headers=headers
        ).status_code == 404
    finally:
        db.close()

    processed.extend(_drain(SessionLocal))

    assert len(processed) == 4
    status = client.get(
        f"/api/uploads/{upload['upload_document_version_id']}/archive-status",
        headers=headers,
    ).json()
    assert status["processing_status"] == "COMPLETED"
    assert status["rename_status"] == "COMPLETED"
    assert status["classification_status"] == "COMPLETED"
    assert status["organization_status"] == "AUTO_ORGANIZED"
    assert status["categories"][0]["category_path"] == [
        "学校",
        "行政综合管理类",
        "会议纪要",
    ]
    assert status["categories"][0]["evidence"]
    assert status["review_reasons"] == []
    db = SessionLocal()
    try:
        working_copy = db.get(WorkingCopy, status["working_copy_id"])
        version = db.get(DocumentVersion, working_copy.current_version_id)
        relation = db.query(DocumentCategory).filter_by(working_copy_id=working_copy.id).one()
        decision = db.query(DocumentOrganizationDecision).filter_by(
            working_copy_id=working_copy.id
        ).one()
        path_record = db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=working_copy.id
        ).one()
        managed_file = db.get(ManagedFile, working_copy.managed_file_id)

        assert working_copy.status == "ACTIVE"
        assert Path(working_copy.relative_path).parent.as_posix() == "学校/行政综合管理类/会议纪要"
        assert working_copy.filename != filename
        assert db.get(Document, working_copy.document_id).original_filename == filename
        assert version.storage_path.endswith(working_copy.relative_path)
        assert relation.status == "AUTO_APPLIED"
        assert relation.relation_role == "PRIMARY"
        assert relation.source == "auto_placement_policy"
        assert decision.decision == "AUTO_ORGANIZED"
        assert decision.feature_snapshot_json["shadow_only"] is False
        assert path_record.operation_type == "INITIAL_AUTO_PLACEMENT"
        assert (tmp_path / "working" / version.storage_path).read_bytes() == content
        # taxonomy 只决定工作副本首次发布位置，不得重写内部保护原件路径。
        assert managed_file.relative_path.startswith("uploads/")
        assert "学校/行政综合管理类/会议纪要" not in managed_file.relative_path
        assert (tmp_path / "originals" / managed_file.relative_path).read_bytes() == content
    finally:
        db.close()


def test_txt_upload_keeps_original_filename_during_initial_organization(monkeypatch, tmp_path):
    """TXT 上传仍执行解析、分类和落位，但首次整理不得自动重命名。"""

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "false")
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    headers = _auth(client, "txt-upload-owner")
    original_filename = "2024科研成果资助汇总表.txt"
    upload = _upload(
        client,
        headers,
        filename=original_filename,
        content="2026年科研成果资助汇总表，学校科研处通知。".encode(),
    )

    _drain(SessionLocal)

    status = client.get(
        f"/api/uploads/{upload['upload_document_version_id']}/archive-status",
        headers=headers,
    ).json()
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    assert status["rename_status"] == "NO_CHANGE"
    assert status["renamed_filename"] == original_filename
    assert working_copy["filename"] == original_filename
    assert status["classification_status"] == "COMPLETED"
    clear_overrides()


def test_rejected_auto_classification_publishes_active_neutral_copy(monkeypatch, tmp_path):
    """无法可靠分类的安全文件仍应可用，只把主分类状态留给人工复核。"""

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "false")
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    headers = _auth(client, "review-placement-owner")
    upload = _upload(
        client,
        headers,
        filename="普通材料.txt",
        content="这是一份没有明确业务主题的普通材料。".encode(),
    )

    _drain(SessionLocal)

    status = client.get(
        f"/api/uploads/{upload['upload_document_version_id']}/archive-status",
        headers=headers,
    ).json()
    assert status["processing_status"] == "NEEDS_REVIEW"
    assert status["organization_status"] == "NEEDS_REVIEW"
    assert status["categories"]
    assert "只能确定为其他分类，需要人工确认。" in status["review_reasons"]
    db = SessionLocal()
    try:
        working_copy = db.get(WorkingCopy, status["working_copy_id"])
        decision = db.query(DocumentOrganizationDecision).filter_by(
            working_copy_id=working_copy.id
        ).one()
        assert working_copy.status == "ACTIVE"
        assert working_copy.relative_path.endswith("普通材料.txt")
        assert decision.decision == "NEEDS_REVIEW"
        assert "OTHER_CATEGORY" in decision.reason_codes_json
        assert db.query(DocumentCategory).filter_by(working_copy_id=working_copy.id).count() == 0
        # 待复核不等于不可用，普通详情入口必须继续返回该活动副本。
        response = client.get(f"/api/working-copies/{working_copy.id}", headers=headers)
        assert response.status_code == 200
    finally:
        db.close()
        clear_overrides()


def test_rename_and_classify_only_returns_uploaded_file_cards(monkeypatch, tmp_path):
    """组合任务不能把重命名内部解析的工作副本再次展示为额外文件。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "rename-classify-card-owner")
    first = _upload(
        client,
        headers,
        filename="公文版头通知.txt",
        content="2023年关于调整更新部分机构公文版头的通知\n请有关单位执行。".encode("utf-8"),
    )
    second = _upload(
        client,
        headers,
        filename="印章刻制申请表.txt",
        content="2025年计算机学院印章刻制申请表\n申请刻制学院业务印章。".encode("utf-8"),
    )
    _drain(SessionLocal)

    response = client.post(
        "/api/conversations/lifecycle-rename-classify/messages",
        headers=headers,
        json={
            "content": "对刚刚上传文件进行重命名和分类",
            "attachments": [
                {"document_id": first["document_id"]},
                {"document_id": second["document_id"]},
            ],
        },
    )

    assert response.status_code == 200
    task_result = response.json()["task_result"]
    document_results = task_result["document_results"]
    assert len(document_results) == 2
    assert all(item["filename"] not in {item["document_id"], ""} for item in document_results)

    with SessionLocal() as db:
        working_document_ids = {
            item.document_id
            for item in db.query(WorkingCopy).filter(WorkingCopy.status == "ACTIVE").all()
        }
    # Agent 执行前已把上传 Document 统一映射到活动工作副本；逐文件
    # 回执因此必须使用工作副本 Document ID，不再使用上传暂存 ID。
    assert {item["document_id"] for item in document_results} == working_document_ids
    clear_overrides()


def test_deferred_upload_rename_plan_executes_after_background_import(monkeypatch, tmp_path):
    """上传后立即生成的重命名计划应在后台导入完成后用同一个计划执行。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "deferred-upload-rename-owner")
    upload = _upload(
        client,
        headers,
        filename="扫描通知.md",
        content="2026年关于开展奖学金评审工作的通知\n请各学院按时报送。".encode("utf-8"),
    )

    message_response = client.post(
        "/api/conversations/deferred-upload-rename/messages",
        headers=headers,
        json={
            "content": "对刚刚上传文件进行重命名和分类",
            "attachments": [{"document_id": upload["document_id"]}],
        },
    )

    assert message_response.status_code == 200
    task_result = message_response.json()["task_result"]
    plan_id = task_result["operation_plan_id"]
    assert task_result["response_type"] == "operation_plan"
    assert task_result["document_results"][0]["document_id"] == upload["document_id"]
    assert plan_id
    plan_before_import = client.get(
        f"/api/operations/plans/{plan_id}",
        headers=headers,
    ).json()
    assert plan_before_import["operation_type"] == "RENAME_PENDING_UPLOADS"
    proposed_filename = plan_before_import["items"][0]["after"]["filename"]

    early_confirmation = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=headers,
        json={"confirmation": "确认执行"},
    )
    assert early_confirmation.status_code == 409
    assert "后台归档或导入工作副本" in early_confirmation.json()["error"]["message"]
    assert client.get(
        f"/api/operations/plans/{plan_id}",
        headers=headers,
    ).json()["status"] == "WAITING_CONFIRMATION"

    _drain(SessionLocal)
    confirmation = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=headers,
        json={"confirmation": "确认执行"},
    )

    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "EXECUTED"
    with SessionLocal() as db:
        working_copy = db.query(WorkingCopy).filter(WorkingCopy.status == "ACTIVE").one()
        assert working_copy.filename == proposed_filename
        assert working_copy.last_operation_plan_id == plan_id
    clear_overrides()


def test_uploaded_low_confidence_name_can_be_corrected_in_same_conversation(monkeypatch, tmp_path):
    """上传命名证据不足时应保留待确认上下文，并接受带真实文件名的后续更正。"""

    _configure(monkeypatch, tmp_path)

    def needs_review_suggestion(self, *, document):
        """稳定模拟无法从正文确认标题的上传文件。"""

        return (
            {
                "document_id": document.id,
                "filename": document.original_filename,
                "source_sha256": document.sha256,
                "status": "NEEDS_REVIEW",
                "proposed_filename": None,
                "warnings": ["正文标题缺失或存在歧义，等待用户确认。"],
                "errors": [],
            },
            None,
        )

    monkeypatch.setattr(
        UploadedRenameSuggestionService,
        "_suggest_one",
        needs_review_suggestion,
    )
    client, SessionLocal = client_with_database()
    headers = _auth(client, "uploaded-review-resolution-owner")
    upload = _upload(
        client,
        headers,
        filename="西安理工大学用印申请单.docx",
        content=b"deterministic-low-confidence-content",
    )
    url = "/api/conversations/uploaded-review-resolution/messages"
    initial = client.post(
        url,
        headers=headers,
        json={
            "content": "对上传文件进行重命名并且分类",
            "attachments": [{"document_id": upload["document_id"]}],
        },
    )

    assert initial.status_code == 200
    initial_result = initial.json()["task_result"]
    assert initial_result["response_type"] == "rename_plan"
    assert initial_result["rename_plan_result"]["needs_review_count"] == 1
    assert "西安理工大学用印申请单.docx" in initial_result["final_response"]
    assert "请勿原样发送" in initial_result["final_response"]

    placeholder = client.post(
        url,
        headers=headers,
        json={
            "content": "文件原文件名更正为新文件名",
            "attachments": [],
        },
    )
    assert placeholder.status_code == 200
    placeholder_result = placeholder.json()["task_result"]
    assert placeholder_result["operation_plan_id"] is None
    assert "占位词" in placeholder_result["final_response"]
    assert "西安理工大学用印申请单.docx" in placeholder_result["final_response"]
    assert "旧待复核项已失效" not in placeholder_result["final_response"]

    corrected = client.post(
        url,
        headers=headers,
        json={
            "content": "文件“西安理工大学用印申请单.docx”更正为“2026_用印申请单.docx”",
            "attachments": [],
        },
    )
    assert corrected.status_code == 200
    corrected_result = corrected.json()["task_result"]
    plan_id = corrected_result["operation_plan_id"]
    assert corrected_result["response_type"] == "operation_plan"
    assert plan_id
    plan = client.get(f"/api/operations/plans/{plan_id}", headers=headers).json()
    assert plan["operation_type"] == "RENAME_PENDING_UPLOADS"
    assert plan["items"][0]["before"]["filename"] == "西安理工大学用印申请单.docx"
    assert plan["items"][0]["after"]["filename"] == "2026_用印申请单.docx"

    _drain(SessionLocal)
    confirmation = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=headers,
        json={"confirmation": "确认执行"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "EXECUTED"
    with SessionLocal() as db:
        working_copy = db.query(WorkingCopy).filter(WorkingCopy.status == "ACTIVE").one()
        assert working_copy.filename == "2026_用印申请单.docx"
    clear_overrides()


def test_explicit_re_rename_uses_current_working_copy_name(monkeypatch, tmp_path):
    """工作副本改名后再说“重新命名为”时，不得按当前名称反查受管原件。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "explicit-current-working-copy-rename-owner")
    original_name = "计算机科学与工程学院下载监控视频申请20250127.docx"
    current_name = "2025_计算机科学与工程学院下载监控视频申请.docx"
    target_name = "2025_计算机科学与工程学院下载监控视频申请1.docx"
    uploaded = _upload(
        client,
        headers,
        filename=original_name,
        content=b"explicit-current-working-copy-rename-content",
    )
    _drain(SessionLocal)
    working_copy = next(
        item
        for item in client.get("/api/working-copies", headers=headers).json()
        if item["filename"] == original_name
    )
    first_plan = client.post(
        "/api/operations/plans",
        headers=headers,
        json={
            "conversation_id": "explicit-current-working-copy-rename-setup",
            "operation_type": "RENAME_WORKING_COPIES",
            "reason": "构造工作副本名称已不同于上传原件的回归场景",
            "items": [
                {
                    "working_copy_id": working_copy["id"],
                    "after": {"filename": current_name},
                }
            ],
        },
    )
    assert first_plan.status_code == 200
    first_confirmation = client.post(
        f"/api/operations/plans/{first_plan.json()['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认第一次重命名"},
    )
    assert first_confirmation.status_code == 200
    assert first_confirmation.json()["status"] == "EXECUTED"

    response = client.post(
        "/api/conversations/explicit-current-working-copy-rename/messages",
        headers=headers,
        json={
            "content": f"将 {current_name} 重新命名为 {target_name}",
            "attachments": [],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    task_result = payload["task_result"]
    assert task_result["response_type"] == "operation_plan"
    plan_id = task_result["operation_plan_id"]
    assert plan_id
    with SessionLocal() as db:
        run = db.get(AgentRun, task_result["task_id"])
        assert run is not None
        assert run.intent == "RESOLVE_RENAME_REVIEW"
        invocations = (
            db.query(ToolInvocation)
            .filter(ToolInvocation.agent_run_id == run.id)
            .order_by(ToolInvocation.created_at.asc())
            .all()
        )
        assert [item.tool_name for item in invocations] == [
            "resolve-rename-reviews"
        ]
    plan = client.get(f"/api/operations/plans/{plan_id}", headers=headers).json()
    assert plan["items"][0]["before"]["filename"] == current_name
    assert plan["items"][0]["after"]["filename"] == target_name

    # 显式请求只生成受控计划；确认前工作副本和上传原件都不能被提前修改。
    with SessionLocal() as db:
        working_copy = db.query(WorkingCopy).filter(WorkingCopy.status == "ACTIVE").one()
        assert working_copy.filename == current_name
        upload_document = db.get(Document, uploaded["document_id"])
        assert upload_document is not None
        assert upload_document.original_filename == original_name

    confirmation = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=headers,
        json={"confirmation": "确认重命名"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "EXECUTED"
    with SessionLocal() as db:
        working_copy = db.query(WorkingCopy).filter(WorkingCopy.status == "ACTIVE").one()
        assert working_copy.filename == target_name
        upload_document = db.get(Document, uploaded["document_id"])
        assert upload_document is not None
        assert upload_document.original_filename == original_name
    clear_overrides()


def test_explicit_working_copy_rename_detects_shared_filename_conflict(
    monkeypatch,
    tmp_path,
):
    """显式改名命中共享同名文件时必须先进入选择闭环，不能直接生成重复名称。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "explicit-shared-rename-conflict-owner")
    _upload(
        client,
        headers,
        filename="述职报告-赵明华-计算机学院.docx",
        content=b"existing-target-content",
    )
    _upload(
        client,
        headers,
        filename="述职报告-赵明华-计算机学院new.docx",
        content=b"pending-rename-content",
    )
    _drain(SessionLocal)

    response = client.post(
        "/api/conversations/explicit-shared-rename-conflict/messages",
        headers=headers,
        json={
            "content": (
                "把“述职报告-赵明华-计算机学院new.docx”"
                "重命名为“述职报告-赵明华-计算机学院.docx”"
            ),
            "attachments": [],
        },
    )

    assert response.status_code == 200
    task_result = response.json()["task_result"]
    assert task_result["response_type"] == "filename_conflict"
    assert task_result["operation_plan_id"] is None
    assert task_result["filename_conflict_result"]["filename"] == (
        "述职报告-赵明华-计算机学院.docx"
    )
    assert task_result["filename_conflict_result"]["allowed_decisions"] == [
        "REPLACE_EXISTING_WORKING_COPY",
        "KEEP_BOTH",
        "CANCEL",
    ]
    with SessionLocal() as db:
        active_names = [
            item.filename
            for item in db.query(WorkingCopy)
            .filter(WorkingCopy.status == "ACTIVE")
            .all()
        ]
        assert active_names.count("述职报告-赵明华-计算机学院.docx") == 1
        review = (
            db.query(FileRenameReviewItem)
            .filter(FileRenameReviewItem.status == "NEEDS_REVIEW")
            .all()
        )
        review = next(
            item
            for item in review
            if item.review_context_json.get("reason") == "FILENAME_CONFLICT"
        )
        assert review.original_filename == "述职报告-赵明华-计算机学院new.docx"
        assert review.review_context_json["target_filename"] == (
            "述职报告-赵明华-计算机学院.docx"
        )
    keep_both = client.post(
        "/api/conversations/explicit-shared-rename-conflict/messages",
        headers=headers,
        json={"content": "两个文件同时保留", "attachments": []},
    )
    assert keep_both.status_code == 200
    keep_both_receipt = keep_both.json()["task_result"]
    assert keep_both_receipt["response_type"] == "operation_plan"
    confirmation = client.post(
        f"/api/operations/plans/{keep_both_receipt['operation_plan_id']}/confirm",
        headers=headers,
        json={"confirmation": "确认同时保留"},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "EXECUTED"
    with SessionLocal() as db:
        active_names = sorted(
            item.filename
            for item in db.query(WorkingCopy)
            .filter(WorkingCopy.status == "ACTIVE")
            .all()
        )
        assert active_names == [
            "述职报告-赵明华-计算机学院.docx",
            "述职报告-赵明华-计算机学院_第二版.docx",
        ]
    clear_overrides()


def test_confirmed_rename_rechecks_shared_filename_conflict_before_execution(
    monkeypatch,
    tmp_path,
):
    """确认执行前新增同名文件时必须令计划失败，防止并发窗口产生重复名称。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "rename-execution-conflict-owner")
    _upload(
        client,
        headers,
        filename="待改名文件.docx",
        content=b"rename-source-content",
    )
    _drain(SessionLocal)
    source_copy = next(
        item
        for item in client.get("/api/working-copies", headers=headers).json()
        if item["filename"] == "待改名文件.docx"
    )
    plan_response = client.post(
        "/api/operations/plans",
        headers=headers,
        json={
            "conversation_id": "rename-execution-conflict",
            "operation_type": "RENAME_WORKING_COPIES",
            "reason": "并发冲突回归测试",
            "items": [
                {
                    "working_copy_id": source_copy["id"],
                    "after": {"filename": "并发目标.docx"},
                }
            ],
        },
    )
    assert plan_response.status_code == 200

    _upload(
        client,
        headers,
        filename="并发目标.docx",
        content=b"concurrent-target-content",
    )
    _drain(SessionLocal)
    confirmation = client.post(
        f"/api/operations/plans/{plan_response.json()['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认重命名"},
    )

    assert confirmation.status_code == 200
    assert confirmation.json()["status"] == "FAILED"
    assert confirmation.json()["result"]["failed_count"] == 1
    assert "共享工作目录已存在同名文件" in (
        confirmation.json()["result"]["items"][0]["error_message"]
    )
    with SessionLocal() as db:
        active = (
            db.query(WorkingCopy)
            .filter(WorkingCopy.status == "ACTIVE")
            .all()
        )
        assert [item.filename for item in active].count("并发目标.docx") == 1
        assert db.get(WorkingCopy, source_copy["id"]).filename == "待改名文件.docx"
    clear_overrides()


def test_missing_rename_source_returns_similar_file_selection_before_plan(
    monkeypatch,
    tmp_path,
):
    """原文件名未精确命中时必须先让用户选择相似文件，禁止直接改错文件。"""

    _configure(monkeypatch, tmp_path)

    def needs_review_suggestion(self, *, document):
        """稳定生成一个需要用户确认名称的上传文件。"""

        return (
            {
                "document_id": document.id,
                "filename": document.original_filename,
                "source_sha256": document.sha256,
                "status": "NEEDS_REVIEW",
                "proposed_filename": None,
                "warnings": ["正文标题缺失或存在歧义，等待用户确认。"],
                "errors": [],
            },
            None,
        )

    monkeypatch.setattr(
        UploadedRenameSuggestionService,
        "_suggest_one",
        needs_review_suggestion,
    )
    client, _ = client_with_database()
    headers = _auth(client, "uploaded-rename-similar-owner")
    upload = _upload(
        client,
        headers,
        filename="西安理工大学用印申请单.docx",
        content=b"rename-similar-selection-content",
    )
    url = "/api/conversations/uploaded-rename-similar/messages"
    initial = client.post(
        url,
        headers=headers,
        json={
            "content": "对上传文件进行重命名并且分类",
            "attachments": [{"document_id": upload["document_id"]}],
        },
    )
    assert initial.status_code == 200

    placeholder_source = client.post(
        url,
        headers=headers,
        json={
            "content": (
                "文件“原文件名.docx”"
                "更正为“2026_用印申请单.docx”"
            ),
            "attachments": [],
        },
    )
    assert placeholder_source.status_code == 200
    placeholder_source_result = placeholder_source.json()["task_result"]
    assert placeholder_source_result["response_type"] == "file_selection"
    assert placeholder_source_result["operation_plan_id"] is None
    assert [
        item["filename"]
        for item in placeholder_source_result["file_selection_result"]["choices"]
    ] == ["西安理工大学用印申请单.docx"]

    # 新一轮更具体的近似文件名会替代旧选择卡，并继续要求用户明确选择。
    selection_response = client.post(
        url,
        headers=headers,
        json={
            "content": (
                "把 “西安理工大学用印申请.docx”"
                " 重命名为 “2026_用印申请单.docx”"
            ),
            "attachments": [],
        },
    )

    assert selection_response.status_code == 200
    selection_result = selection_response.json()["task_result"]
    assert selection_result["response_type"] == "file_selection"
    assert selection_result["operation_plan_id"] is None
    choices = selection_result["file_selection_result"]["choices"]
    assert [item["filename"] for item in choices] == [
        "西安理工大学用印申请单.docx"
    ]

    resolved = client.post(
        (
            "/api/file-search/clarifications/"
            f"{selection_result['file_selection_result']['clarification_id']}/resolve"
        ),
        headers=headers,
        json={"option_id": choices[0]["option_id"], "custom_phrase": None},
    )

    assert resolved.status_code == 200
    resolved_result = resolved.json()["task_result"]
    assert resolved_result["response_type"] == "operation_plan"
    assert resolved_result["operation_plan_id"]
    plan = client.get(
        f"/api/operations/plans/{resolved_result['operation_plan_id']}",
        headers=headers,
    ).json()
    assert plan["items"][0]["before"]["filename"] == "西安理工大学用印申请单.docx"
    assert plan["items"][0]["after"]["filename"] == "2026_用印申请单.docx"
    clear_overrides()


def test_missing_rename_source_without_similar_file_requests_reattachment(
    monkeypatch,
    tmp_path,
):
    """扩大当前会话候选后仍无相似文件时，必须提示重新附加而不是返回笼统空结果。"""

    _configure(monkeypatch, tmp_path)

    def needs_review_suggestion(self, *, document):
        """稳定生成一个与用户所报文件名无关的待确认文件。"""

        return (
            {
                "document_id": document.id,
                "filename": document.original_filename,
                "source_sha256": document.sha256,
                "status": "NEEDS_REVIEW",
                "proposed_filename": None,
                "warnings": ["正文标题缺失或存在歧义，等待用户确认。"],
                "errors": [],
            },
            None,
        )

    monkeypatch.setattr(
        UploadedRenameSuggestionService,
        "_suggest_one",
        needs_review_suggestion,
    )
    client, _ = client_with_database()
    headers = _auth(client, "uploaded-rename-no-similar-owner")
    upload = _upload(
        client,
        headers,
        filename="西安理工大学用印申请单.docx",
        content=b"rename-no-similar-content",
    )
    url = "/api/conversations/uploaded-rename-no-similar/messages"
    client.post(
        url,
        headers=headers,
        json={
            "content": "对上传文件进行重命名并且分类",
            "attachments": [{"document_id": upload["document_id"]}],
        },
    )

    response = client.post(
        url,
        headers=headers,
        json={
            "content": (
                "文件“火星探测器发射记录.pdf”"
                "更正为“新火星记录.pdf”"
            ),
            "attachments": [],
        },
    )

    assert response.status_code == 200
    result = response.json()["task_result"]
    assert result["operation_plan_id"] is None
    assert result["file_selection_result"] is None
    assert "没有可供确认的相似文件" in result["final_response"]
    assert "重新附加" in result["final_response"]
    clear_overrides()


def test_rename_similarity_expands_to_user_active_shared_working_copies(
    monkeypatch,
    tmp_path,
):
    """当前会话无附件时也应从用户可见的活动共享工作副本返回相似候选。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "active-working-copy-rename-owner")
    _upload(
        client,
        headers,
        filename="2020年度个人述职报告.txt",
        content=b"active shared working copy rename candidate",
    )
    _drain(SessionLocal)

    response = client.post(
        "/api/conversations/active-working-copy-rename/messages",
        headers=headers,
        json={
            "content": (
                "文件“2020个人述职报告.txt”"
                "更正为“2020年度述职报告.txt”"
            ),
            "attachments": [],
        },
    )

    assert response.status_code == 200
    result = response.json()["task_result"]
    assert result["response_type"] == "file_selection"
    assert result["operation_plan_id"] is None
    choices = result["file_selection_result"]["choices"]
    assert [item["filename"] for item in choices] == [
        "2020年度个人述职报告.txt"
    ]
    assert choices[0]["working_copy_id"]
    clear_overrides()


def test_trashed_upload_source_cannot_read_historical_content(monkeypatch, tmp_path):
    """上传来源 ID 映射到已删除工作副本时，也不能复用历史正文或回收站文件。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "trashed-source-reader")
    upload = _upload(
        client,
        headers,
        filename="资助汇总表.xlsx",
        content=b"placeholder workbook bytes",
    )
    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    _trash_working_copy(client, headers, working_copy["id"], "trashed-source-read-conv")

    content_response = client.get(
        f"/api/files/{upload['document_id']}/content",
        headers=headers,
    )
    preview_response = client.get(
        f"/api/files/{upload['document_id']}/preview",
        headers=headers,
    )
    assert content_response.status_code == 410
    assert "已删除" in content_response.json()["error"]["message"]
    assert preview_response.status_code == 410
    assert "已删除" in preview_response.json()["error"]["message"]

    spreadsheet_request = client.post(
        "/api/conversations/trashed-source-read-conv/messages",
        headers=headers,
        json={
            "content": "汇总资助汇总表.xlsx中的资助金额",
            "attachments": [{"document_id": upload["document_id"]}],
        },
    )
    assert spreadsheet_request.status_code == 200
    receipt = spreadsheet_request.json()["task_result"]
    assert receipt["response_type"] == "trash_restore_selection"
    assert receipt["trash_restore_result"]["candidates"]

    with SessionLocal() as db:
        source_document = db.get(Document, upload["document_id"])
        assert source_document is not None
        resolved = FileExtractionRepository(
            db,
            source_document.user_id,
        ).resolve_original_file(upload["document_id"])
        assert resolved["ok"] is False
        assert resolved["error"]["code"] == "FILE_TRASHED"
    clear_overrides()


def test_uploaded_document_resolves_immutable_archive_after_staging_cleanup(
    monkeypatch,
    tmp_path,
):
    """暂存文件清理后，上传附件 ID 仍应沿审计血缘读取不可变归档原件。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "archived-original-reader")
    payload = b"immutable archived image bytes"
    upload = _upload(
        client,
        headers,
        filename="资助登记.txt",
        content=payload,
    )
    _drain(SessionLocal)

    with SessionLocal() as db:
        document = db.get(Document, upload["document_id"])
        assert document is not None
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        assert archive.status == "ARCHIVED"
        archived_path = Path(tmp_path / "originals" / str(archive.archive_relative_path))
        assert archived_path.read_bytes() == payload

        storage = FileLifecycleStorageService()
        for file_object in db.query(FileObject).filter_by(document_id=document.id).all():
            staging_path = storage.file_object_path(file_object)
            staging_path.resolve().relative_to((tmp_path / "uploads").resolve())
            staging_path.unlink(missing_ok=True)

        resolved = FileExtractionRepository(db, document.user_id).resolve_original_file(document.id)
        assert resolved["ok"] is True
        assert resolved["source_tier"] == "UPLOAD_ARCHIVE"
        assert Path(resolved["file_path"]).read_bytes() == payload
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


def test_failed_search_repair_is_not_requeued_on_every_scan(monkeypatch, tmp_path):
    """当前版本确定性解析失败后，自动扫描不得无限重复创建同一修复任务。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "search-repair-failure-owner")
    upload = _upload(
        client,
        headers,
        filename="历史材料.txt",
        content="历史工作副本检索修复测试正文。".encode("utf-8"),
    )
    _drain(SessionLocal)

    with SessionLocal() as db:
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        working_copy = db.query(WorkingCopy).filter_by(
            managed_file_id=archive.managed_file_id
        ).one()
        version_id = working_copy.current_version_id
        extraction_runs = db.query(DocumentExtractionRun).filter_by(
            document_id=working_copy.document_id
        ).all()
        assert extraction_runs
        for run in extraction_runs:
            run.status = "FAILED"
            run.error_message = "确定性解析失败，等待显式重处理"
        chunk_ids = [
            value
            for (value,) in db.query(DocumentChunk.id).filter_by(
                document_version_id=version_id
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
        db.commit()
        db.refresh(working_copy)
        status = working_copy_search_artifact_status(db, working_copy)
        assert status["profile_ready"] is True
        assert status["index_ready"] is False
        assert status["repair_blocked"] is True
        extraction_count = db.query(DocumentExtractionRun).filter_by(
            document_id=working_copy.document_id
        ).count()
        root_id = archive.managed_root_id
        user_id = db.get(Document, working_copy.document_id).user_id

        for suffix in ("first", "second"):
            FilesystemJobQueue(db).create_job(
                job_type="SCAN_MANAGED_ROOT",
                queue_name="SCAN",
                root_id=root_id,
                created_by=user_id,
                deduplication_key=f"blocked-search-repair-scan:{suffix}:{root_id}",
                payload={"reason": "test-blocked-search-repair"},
            )
            db.commit()
            processed = _drain(SessionLocal)
            # 每轮只处理扫描本身；不能继续派生 IMPORT_WORKING_COPIES 修复任务。
            assert len(processed) == 1

        assert db.query(DocumentExtractionRun).filter_by(
            document_id=working_copy.document_id
        ).count() == extraction_count
        assert db.query(DocumentIndexRun).filter_by(
            document_version_id=version_id
        ).count() == 0
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
    selected_status = client.get(
        f"/api/uploads/{second['upload_document_version_id']}/archive-status",
        headers=headers,
    ).json()
    assert selected_status["processing_status"] in {"COMPLETED", "NEEDS_REVIEW"}
    assert selected_status["working_copy_id"] == existing_copy_id
    assert selected_status["renamed_filename"] == "first.txt"
    assert selected_status["categories"]
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


def test_duplicate_upload_compares_current_managed_file_without_working_copy(monkeypatch, tmp_path):
    """尚未物化工作副本的当前受管文件参与查重和受控预览，但不能直接选择使用。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "managed-source-duplicate-owner")
    content = b"current managed source"
    source_root = tmp_path / "managed-current"
    source_root.mkdir()
    (source_root / "current.txt").write_bytes(content)
    db = SessionLocal()
    try:
        root = ManagedRoot(
            root_key="current_source",
            display_name="当前受管文件",
            container_path=str(source_root),
            enabled=True,
            read_only=True,
        )
        db.add(root)
        db.flush()
        db.add(ManagedFile(
            root_id=root.id,
            relative_path="current.txt",
            filename="current.txt",
            extension=".txt",
            size_bytes=len(content),
            fingerprint=hashlib.sha256(content).hexdigest(),
            content_sha256=hashlib.sha256(content).hexdigest(),
            status="ACTIVE",
        ))
        db.commit()
    finally:
        db.close()

    uploaded = _upload(client, headers, "copy.txt", content)
    process_next_filesystem_job(session_factory=SessionLocal, worker_id="managed-source-duplicate-test")
    review = client.get(
        f"/api/uploads/{uploaded['upload_document_version_id']}/duplicate-review",
        headers=headers,
    ).json()

    assert review["status"] == "WAITING_CONFIRMATION"
    assert review["allowed_decisions"] == ["CONTINUE_UPLOAD", "CANCEL_UPLOAD"]
    assert review["candidates"][0]["existing_working_copy_id"] is None
    assert review["candidates"][0]["summary"]["managed_root_key"] == "current_source"
    assert review["candidates"][0]["summary"]["managed_relative_path"] == "current.txt"
    preview = client.get(
        "/api/managed-files/preview",
        headers=headers,
        params={"root_key": "current_source", "relative_path": "current.txt"},
    )
    assert preview.status_code == 200
    assert preview.content == content
    clear_overrides()


@pytest.mark.parametrize("decision", ["USE_EXISTING_FILE", "CANCEL_UPLOAD"])
def test_duplicate_upload_cannot_be_cleaned_after_message_lock(monkeypatch, tmp_path, decision):
    """临时上传进入消息后不得再被替换或取消，否则运行中的 Agent 会失去原文件。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "duplicate-locked-owner")
    _upload(client, headers, "first.txt", b"identical locked body")
    _drain(SessionLocal)
    second = _upload(client, headers, "second.txt", b"identical locked body")
    process_next_filesystem_job(session_factory=SessionLocal, worker_id="duplicate-locked-test")
    review = client.get(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review",
        headers=headers,
    ).json()

    db = SessionLocal()
    try:
        document = db.get(Document, second["document_id"])
        document.status = "USED_IN_MESSAGE"
        document.locked_message_id = "message-in-flight"
        document.locked_conversation_id = "conversation-in-flight"
        db.commit()
    finally:
        db.close()

    decision_payload = {
        "duplicate_review_id": review["id"],
        "decision": decision,
    }
    if decision == "USE_EXISTING_FILE":
        decision_payload["selected_existing_working_copy_id"] = review["candidates"][0][
            "existing_working_copy_id"
        ]
    response = client.post(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review/decision",
        headers=headers,
        json=decision_payload,
    )

    assert response.status_code == 409
    db = SessionLocal()
    try:
        persisted_review = db.get(UploadDuplicateReview, review["id"])
        upload_file = db.query(FileObject).filter_by(document_id=second["document_id"]).one()
        assert persisted_review.status == "WAITING_CONFIRMATION"
        assert (tmp_path / "uploads" / upload_file.storage_path).exists()
    finally:
        db.close()
        clear_overrides()


def test_duplicate_upload_ignores_deleted_match_and_creates_new_copy(monkeypatch, tmp_path):
    """相同内容只存在于回收站时不参与查重，直接按新文件继续上传。"""

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
    assert review["status"] == "RESOLVED"
    assert review["allowed_decisions"] == ["CONTINUE_UPLOAD", "CANCEL_UPLOAD"]
    assert review["decision"] == "CONTINUE_UPLOAD"
    assert review["candidates"] == []

    _drain(SessionLocal)
    copies = client.get("/api/working-copies", headers=headers).json()
    assert sorted(item["status"] for item in copies) == ["ACTIVE", "TRASHED"]
    # 新上传形成全新工作副本，已删除副本仍保留在回收站且没有被自动恢复。
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
    _start_upload(client, headers, second)

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


def test_low_confidence_initial_name_keeps_upload_name_and_audit_without_chat_notice(monkeypatch, tmp_path):
    """低置信度命名保留原名和复核审计，但未请求改名时不进入聊天。"""

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "false")
    config.get_settings.cache_clear()
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

    _start_upload(client, headers, upload)

    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    history = client.get("/api/conversations/low-confidence-conv", headers=headers).json()

    assert working_copy["filename"] == "原上传名称.txt"
    assert history["messages"] == []
    db = SessionLocal()
    try:
        review = db.query(FileRenameReviewItem).filter_by(document_id=working_copy["document_id"]).one()
        assert review.status == "NEEDS_REVIEW"
        assert review.review_context_json["reason"] == "LOW_CONFIDENCE_RENAME"
        assert db.get(Document, upload["document_id"]).original_filename == "原上传名称.txt"
        background_run = (
            db.query(AgentRun)
            .filter(AgentRun.conversation_id == "low-confidence-conv")
            .order_by(AgentRun.created_at.desc())
            .first()
        )
        assert background_run is not None
        pending_decision = background_run.graph_state_json["document_results"][0][
            "pending_decision"
        ]
        assert pending_decision["reason"] == "LOW_CONFIDENCE_RENAME"
        assert db.get(Message, background_run.message_id).role == "SYSTEM_AUDIT"
    finally:
        db.close()
        clear_overrides()


def test_initial_ready_rename_is_applied_to_working_copy_on_upload(monkeypatch, tmp_path):
    """首次导入应自动应用可信标准名称，同时永久保留 Document 原始文件名。"""

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "false")
    config.get_settings.cache_clear()
    original_suggest = UploadedRenameSuggestionService.suggest_for_initial_import

    def force_ready_suggestion(self, *, document):
        """固定一个可执行候选，保护“建议不等于用户授权”的边界。"""

        suggestion, extraction = original_suggest(self, document=document)
        return {
            **suggestion,
            "status": "READY",
            "proposed_filename": "2026_研究成果资助汇总表.md",
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
                "2024科研成果资助汇总表.md",
                b"research funding summary fixture",
                "text/plain",
            )
        },
    )
    assert upload.status_code == 202
    upload_payload = upload.json()
    _start_upload(client, headers, upload_payload)

    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    history = client.get("/api/conversations/rename-suggestion-conv", headers=headers).json()
    archive_status = client.get(
        f"/api/uploads/{upload_payload['upload_document_version_id']}/archive-status",
        headers=headers,
    ).json()

    assert working_copy["filename"] == "2026_研究成果资助汇总表.md"
    assert archive_status["original_filename"] == "2024科研成果资助汇总表.md"
    assert archive_status["renamed_filename"] == "2026_研究成果资助汇总表.md"
    assert archive_status["rename_status"] == "COMPLETED"
    assert history["messages"] == []
    db = SessionLocal()
    try:
        background_run = (
            db.query(AgentRun)
            .filter(AgentRun.conversation_id == "rename-suggestion-conv")
            .order_by(AgentRun.created_at.desc())
            .first()
        )
        assert background_run is not None
        audit_result = background_run.graph_state_json["document_results"][0]
        assert audit_result["rename_suggestion"] is None
        assert audit_result["original_filename"] == "2024科研成果资助汇总表.md"
        assert audit_result["renamed_filename"] == "2026_研究成果资助汇总表.md"
        assert audit_result["rename_status"] == "COMPLETED"
        assert audit_result["pending_decision"] is None
        audit_message = db.get(Message, background_run.message_id)
        assert audit_message.role == "SYSTEM_AUDIT"

    finally:
        db.close()


    db = SessionLocal()
    try:
        working_document = db.get(Document, working_copy["document_id"])
        original = db.query(ManagedFile).one()
        path_record = db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=working_copy["id"],
        ).one()
        assert working_document.original_filename == "2024科研成果资助汇总表.md"
        assert original.filename == "2024科研成果资助汇总表.md"
        assert path_record.after_filename == "2026_研究成果资助汇总表.md"
        assert db.query(FileRenameReviewItem).filter_by(document_id=working_copy["document_id"]).count() == 0
    finally:
        db.close()
        clear_overrides()


def test_archive_job_resumes_from_retry_wait(monkeypatch, tmp_path):
    """A queued archive retry must resume instead of failing on its own retry state."""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "archive-retry-owner")
    upload = _upload(client, headers, "retry.txt", b"retryable upload")

    assert process_next_filesystem_job(
        session_factory=SessionLocal,
        worker_id="archive-retry-duplicate-check",
        queue_names={"DUPLICATE_CHECK"},
    ) is not None
    with SessionLocal() as db:
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        archive.status = "RETRY_WAIT"
        db.commit()

    assert process_next_filesystem_job(
        session_factory=SessionLocal,
        worker_id="archive-retry-worker",
        queue_names={"ARCHIVE"},
    ) is not None
    with SessionLocal() as db:
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        assert archive.status == "ARCHIVED"
        assert archive.managed_file_id
    clear_overrides()


def test_archive_status_does_not_publish_staged_name_as_rename_result(monkeypatch, tmp_path):
    """首次整理完成前，暂存工作副本名不能冒充最终重命名结果。"""

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "false")
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    headers = _auth(client, "rename-processing-owner")
    upload = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": ("原始名称.txt", b"2026 scholarship notice", "text/plain")},
    ).json()
    _start_upload(client, headers, upload)

    for queue_name in ("DUPLICATE_CHECK", "ARCHIVE", "IMPORT"):
        assert process_next_filesystem_job(
            session_factory=SessionLocal,
            worker_id=f"rename-processing-{queue_name.lower()}",
            queue_names={queue_name},
        ) is not None

    status = client.get(
        f"/api/uploads/{upload['upload_document_version_id']}/archive-status",
        headers=headers,
    ).json()
    assert status["working_copy_status"] == "ORGANIZING"
    assert status["processing_status"] == "PROCESSING"
    assert status["rename_status"] == "PROCESSING"
    assert status["renamed_filename"] is None
    clear_overrides()


def test_single_and_multiple_uploaded_images_share_college_upload_date_directory(
    monkeypatch,
    tmp_path,
):
    """单张与多张图片均跳过学院识别，按中国本地上传日归入同一学院日期目录。"""

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "false")
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    headers = _auth(client, "image-date-placement-owner")

    uploads = []
    for color in ("red", "blue"):
        response = client.post(
            "/api/files/upload",
            headers=headers,
            files={"file": ("现场照片.png", _png_bytes(color), "image/png")},
        )
        assert response.status_code == 202
        uploads.append(response.json())

    with SessionLocal() as db:
        for upload in uploads:
            version = db.get(
                DocumentVersion,
                upload["upload_document_version_id"],
            )
            # UTC 16:30 在中国时区已经是次日，保护日期目录的时区边界。
            version.created_at = datetime(
                2026,
                9,
                2,
                16,
                30,
                tzinfo=timezone.utc,
            )
        db.commit()

    for upload in uploads:
        _start_upload(client, headers, upload)
    assert len(_drain(SessionLocal)) == 8

    statuses = [
        client.get(
            f"/api/uploads/{upload['upload_document_version_id']}/archive-status",
            headers=headers,
        ).json()
        for upload in uploads
    ]
    assert all(item["processing_status"] == "COMPLETED" for item in statuses)
    assert all(item["organization_status"] == "AUTO_ORGANIZED" for item in statuses)
    assert all(
        item["categories"][0]["category_path"] == ["学院", "2026-09-03"]
        for item in statuses
    )

    with SessionLocal() as db:
        working_copies = [
            db.get(WorkingCopy, item["working_copy_id"])
            for item in statuses
        ]
        assert all(
            Path(item.relative_path).parent.as_posix() == "学院/2026-09-03"
            for item in working_copies
        )
        # 文件夹中出现同名图片时必须全部保留，不得覆盖或退回中性目录。
        assert len({item.filename for item in working_copies}) == 2
        relations = (
            db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id.in_(
                    [item.id for item in working_copies]
                )
            )
            .all()
        )
        assert len(relations) == 2
        assert all(item.category_id == "college" for item in relations)
        assert all(
            item.category_path_json == ["学院", "2026-09-03"]
            for item in relations
        )
        assert all(item.source == "image_upload_date_policy" for item in relations)
        for upload in uploads:
            source_document = db.get(Document, upload["document_id"])
            assert source_document.original_filename == "现场照片.png"
    clear_overrides()


def test_same_filename_upload_prompts_before_both_files_are_normally_imported(monkeypatch, tmp_path):
    """同名上传只在上传时提示；用户继续后两个文件都必须正常导入。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "same-filename-owner")
    _upload(client, headers, "同名材料.txt", b"first unique material")
    _drain(SessionLocal)

    second = _upload(client, headers, "同名材料.txt", b"second completely different content")
    process_next_filesystem_job(
        session_factory=SessionLocal,
        worker_id="same-filename-duplicate-check",
        queue_names={"DUPLICATE_CHECK"},
    )
    review_response = client.get(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review",
        headers=headers,
    )
    assert review_response.status_code == 200
    review = review_response.json()
    assert review["status"] == "WAITING_CONFIRMATION"
    assert any(item["match_type"] == "SAME_FILENAME" for item in review["candidates"])

    decision = client.post(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review/decision",
        headers=headers,
        json={
            "duplicate_review_id": review["id"],
            "decision": "CONTINUE_UPLOAD",
        },
    )
    assert decision.status_code == 202
    _drain(SessionLocal)

    copies = client.get("/api/working-copies", headers=headers).json()
    same_name_copies = [item for item in copies if item["filename"] == "同名材料.txt"]
    assert len(same_name_copies) == 2
    assert all(
        not item["relative_path"].startswith(("待整理/", "待确认/"))
        for item in same_name_copies
    )
    clear_overrides()


@pytest.mark.parametrize("legacy_directory", ["待整理", "待确认"])
def test_layout_repair_restores_legacy_pending_file_to_shared_source_path(
    monkeypatch,
    tmp_path,
    legacy_directory,
):
    """历史系统暂存文件必须迁回共享根和原始相对路径，并追加路径审计。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "layout-repair-owner")
    _upload(client, headers, "历史材料.txt", b"legacy pending body")
    _drain(SessionLocal)

    db = SessionLocal()
    try:
        working_copy = db.query(WorkingCopy).one()
        working_root = db.get(WorkingCopyRoot, working_copy.working_copy_root_id)
        managed_file = db.get(ManagedFile, working_copy.managed_file_id)
        version = db.get(DocumentVersion, working_copy.current_version_id)
        file_object = db.query(FileObject).filter_by(document_id=working_copy.document_id).one()
        original_physical = tmp_path / "working" / version.storage_path
        legacy_relative = f"{legacy_directory}/{managed_file.id}/{working_copy.filename}"
        legacy_physical = tmp_path / "working" / legacy_relative
        legacy_physical.parent.mkdir(parents=True, exist_ok=True)
        original_physical.replace(legacy_physical)
        working_root.relative_storage_path = ""
        working_copy.relative_path = legacy_relative
        working_copy.relative_path_hash = hashlib.sha256(legacy_relative.encode("utf-8")).hexdigest()
        version.storage_path = legacy_relative
        file_object.storage_path = legacy_relative
        initial_record = db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=working_copy.id,
            operation_type="INITIAL_IMPORT",
        ).one()
        initial_record.after_relative_path = legacy_relative
        db.commit()

        result = WorkingCopyLayoutRepairService(db).repair_managed_root(
            managed_root_id=managed_file.root_id,
        )
        db.commit()
        db.refresh(working_root)
        db.refresh(working_copy)
        db.refresh(version)

        expected_storage = f"shared/upload_archive/{managed_file.relative_path}"
        assert result["legacy_paths"] == 1
        assert working_root.relative_storage_path == "shared/upload_archive"
        assert working_copy.relative_path == managed_file.relative_path
        assert version.storage_path == expected_storage
        assert (tmp_path / "working" / expected_storage).read_bytes() == b"legacy pending body"
        assert not legacy_physical.exists()
        repair_record = db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=working_copy.id,
            operation_type="SYSTEM_LAYOUT_REPAIR",
        ).one()
        assert repair_record.before_relative_path == legacy_relative
        assert repair_record.after_relative_path == expected_storage
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
    trashed_download = client.get(
        f"/api/working-copies/{working_copy['id']}/download",
        headers=headers,
    )
    assert trashed_download.status_code == 410
    assert trashed_download.json()["error"]["message"] == "文件已删除，请先恢复。"
    with SessionLocal() as db:
        operation_messages = db.query(Message).filter(
            Message.content.like("工作副本操作完成：%")
        ).all()
        assert operation_messages
        assert all(message.role == "SYSTEM_AUDIT" for message in operation_messages)
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

    _start_upload(client, headers, upload)

    processed = _drain(SessionLocal)

    assert len(processed) == 2
    status = client.get(
        f"/api/uploads/{upload['upload_document_version_id']}/archive-status",
        headers=headers,
    ).json()
    assert status["status"] == "NEEDS_REVIEW"
    assert status["processing_status"] == "NEEDS_REVIEW"
    assert status["rename_status"] == "NEEDS_REVIEW"
    assert status["classification_status"] == "NEEDS_REVIEW"
    assert status["organization_status"] == "NEEDS_REVIEW"
    assert status["review_reasons"]
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


def test_cross_user_duplicate_can_use_shared_existing_working_copy(monkeypatch, tmp_path):
    """重复内容命中共享活动工作副本时，任意登录用户都能选择现有文件。"""

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
    assert candidate["match_scope"] == "SAME_WORKSPACE"
    assert candidate["existing_working_copy_id"]
    assert candidate["existing_document_id"]
    assert candidate["summary"]["filename"] == "机密姓名名单.txt"
    assert candidate["summary"]["relative_path"]
    assert review["allowed_decisions"] == [
        "CONTINUE_UPLOAD",
        "USE_EXISTING_FILE",
        "CANCEL_UPLOAD",
    ]

    decision = client.post(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review/decision",
        headers=second_headers,
        json={
            "duplicate_review_id": review["id"],
            "decision": "USE_EXISTING_FILE",
            "selected_existing_working_copy_id": candidate["existing_working_copy_id"],
        },
    )
    assert decision.status_code == 202
    assert decision.json()["selected_existing_document_id"] == candidate["existing_document_id"]
    assert decision.json()["archive_status"] == "EXISTING_FILE_SELECTED"
    assert client.get(
        f"/api/files/{candidate['existing_document_id']}/content",
        headers=second_headers,
    ).content == b"cross user duplicate"
    message = client.post(
        "/api/conversations/shared-existing-duplicate/messages",
        headers=second_headers,
        json={
            "content": "请读取这个现有文件",
            "attachments": [{"document_id": candidate["existing_document_id"]}],
        },
    )
    assert message.status_code == 200
    history = client.get(
        "/api/conversations/shared-existing-duplicate",
        headers=second_headers,
    ).json()
    assert history["messages"][0]["attachments"][0]["document_id"] == candidate["existing_document_id"]
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
        assert work_document.original_filename == "04级工程硕士开课通知.doc"
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


def test_explicit_category_organization_moves_shared_file_without_second_confirmation(
    monkeypatch,
    tmp_path,
):
    """按分类整理指令直接授权受控移动，同时保留计划、确认和版本审计。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "category-move-owner")
    _upload(client, headers, "教师考核材料.txt", b"teacher assessment")
    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    original_path = working_copy["relative_path"]

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "category-move-owner").one()
        taxonomy = load_default_taxonomy()
        run = AgentRun(
            id="33333333-3333-4333-8333-333333333333",
            conversation_id="category-move-conversation",
            message_id="category-move-message",
            user_id=user.id,
        )
        db.add_all(
            [
                run,
                DocumentCategory(
                    working_copy_id=working_copy["id"],
                    document_id=working_copy["document_id"],
                    document_version_id=working_copy["current_version_id"],
                    category_id="school.hr.appointment-assessment",
                    category_path_json=["学校", "人事师资", "考核聘任"],
                    relation_role="PRIMARY",
                    status="CONFIRMED",
                    taxonomy_key=taxonomy.key,
                    taxonomy_version=taxonomy.version,
                    classifier_version="test",
                ),
            ]
        )
        db.commit()
        before_version_count = (
            db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == working_copy["document_id"])
            .count()
        )
        executed = ToolRegistry(db=db, user_id=user.id).invoke(
            "working-copy-action-plan-create",
            {
                "action": "MOVE_BY_CONFIRMED_CATEGORY",
                "message": "按确认分类整理这个文件",
                "document_ids": [working_copy["document_id"]],
                "conversation_id": run.conversation_id,
                "agent_run_id": run.id,
            },
        )
        moved = db.get(WorkingCopy, working_copy["id"])
        assert executed.output_json["status"] == "EXECUTED"
        assert executed.output_json["file_position_changed"] is True
        assert executed.output_json["operation_plan_id"]
        assert moved.relative_path != original_path
        assert moved.relative_path == "学校/人事师资/考核聘任/教师考核材料.txt"
        assert (
            db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == working_copy["document_id"])
            .count()
            == before_version_count
        )
        assert (
            db.query(WorkingCopyPathRecord)
            .filter(
                WorkingCopyPathRecord.working_copy_id == working_copy["id"],
                WorkingCopyPathRecord.operation_type == "MOVE",
                WorkingCopyPathRecord.status == "COMPLETED",
            )
            .count()
            == 1
        )
    finally:
        db.close()
        clear_overrides()


def test_auto_reclassification_change_moves_without_second_confirmation(
    monkeypatch,
    tmp_path,
):
    """重新分类达标且主分类变化时，当前指令直接授权关系更新和受控移动。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "auto-reclassification-move-owner")
    _upload(
        client,
        headers,
        "工资津贴发放情况自查通知.txt",
        b"salary allowance audit notice",
    )
    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    original_path = working_copy["relative_path"]

    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "false")
    config.get_settings.cache_clear()

    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.username == "auto-reclassification-move-owner")
            .one()
        )
        taxonomy = load_default_taxonomy()
        run = AgentRun(
            id="44444444-4444-4444-8444-444444444444",
            conversation_id="auto-reclassification-conversation",
            message_id="auto-reclassification-message",
            user_id=user.id,
        )
        classification_run = DocumentClassificationRun(
            id="55555555-5555-4555-8555-555555555555",
            document_id=working_copy["document_id"],
            agent_run_id=run.id,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            classifier_version="test-auto-reclassification",
            source="rule",
            status="COMPLETED",
        )
        suggestion = DocumentCategorySuggestion(
            id="66666666-6666-4666-8666-666666666666",
            classification_run_id=classification_run.id,
            document_id=working_copy["document_id"],
            document_version_id=working_copy["current_version_id"],
            category_id="school.hr.salary-social-security",
            category_name="学校/人事师资/劳资社保",
            category_path_json=["学校", "人事师资", "劳资社保"],
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            confidence=0.9,
            status="SUGGESTED",
            evidence_json=[
                {
                    "type": "text_quote",
                    "page_number": 1,
                    "sheet_name": None,
                    "quote": "开展工资津贴补贴发放情况自查",
                    "signals": ["工资", "津贴", "补贴"],
                    "source": "document_pages",
                }
            ],
            candidate_scores_json={
                "rule": 0.9,
                "matched_content_signals": ["工资", "津贴", "补贴"],
                "negative_signals": [],
            },
            source="rule",
            rank=1,
        )
        old_relation = DocumentCategory(
            working_copy_id=working_copy["id"],
            document_id=working_copy["document_id"],
            document_version_id=working_copy["current_version_id"],
            category_id="school.audit",
            category_path_json=["学校", "审计"],
            relation_role="PRIMARY",
            status="AUTO_APPLIED",
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            classifier_version="previous-classifier",
            source="auto_placement_policy",
        )
        db.add_all([run, classification_run, suggestion, old_relation])
        db.commit()

        executed = ToolRegistry(db=db, user_id=user.id).invoke(
            "working-copy-action-plan-create",
            {
                "action": "MOVE_AFTER_AUTO_RECLASSIFICATION",
                "message": "重新分类这个文件，并在分类变化时整理位置",
                "document_ids": [working_copy["document_id"]],
                "conversation_id": run.conversation_id,
                "agent_run_id": run.id,
            },
        )

        assert executed.output_json["status"] == "EXECUTED"
        assert executed.output_json["file_position_changed"] is True
        current_copy = db.get(WorkingCopy, working_copy["id"])
        assert current_copy.relative_path != original_path
        db.refresh(old_relation)
        assert old_relation.status == "ENDED"
        new_relation = (
            db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id == working_copy["id"],
                DocumentCategory.status == "AUTO_APPLIED",
            )
            .one()
        )
        assert new_relation.category_id == "school.hr.salary-social-security"

        db.refresh(current_copy)
        assert current_copy.relative_path == (
            "学校/人事师资/劳资社保/工资津贴发放情况自查通知.txt"
        )

        # 人工确认属于更高优先级事实。后续自动重新分类即使证据充分，
        # 也只能进入复核，不能覆盖关系或再次创建移动计划。
        new_relation.status = "CONFIRMED"
        protected_run = AgentRun(
            id="77777777-7777-4777-8777-777777777777",
            conversation_id="protected-reclassification-conversation",
            message_id="protected-reclassification-message",
            user_id=user.id,
        )
        protected_classification_run = DocumentClassificationRun(
            id="88888888-8888-4888-8888-888888888888",
            document_id=working_copy["document_id"],
            agent_run_id=protected_run.id,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            classifier_version="test-confirmed-category-protection",
            source="rule",
            status="COMPLETED",
        )
        protected_suggestion = DocumentCategorySuggestion(
            id="99999999-9999-4999-8999-999999999999",
            classification_run_id=protected_classification_run.id,
            document_id=working_copy["document_id"],
            document_version_id=working_copy["current_version_id"],
            category_id="school.audit",
            category_name="学校/审计",
            category_path_json=["学校", "审计"],
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            confidence=0.9,
            status="SUGGESTED",
            evidence_json=[
                {
                    "type": "text_quote",
                    "page_number": 1,
                    "sheet_name": None,
                    "quote": "开展专项审计检查",
                    "signals": ["审计", "检查"],
                    "source": "document_pages",
                }
            ],
            candidate_scores_json={
                "rule": 0.9,
                "matched_content_signals": ["审计", "检查"],
                "negative_signals": [],
            },
            source="rule",
            rank=1,
        )
        db.add_all(
            [protected_run, protected_classification_run, protected_suggestion]
        )
        db.commit()

        protected = ToolRegistry(db=db, user_id=user.id).invoke(
            "working-copy-action-plan-create",
            {
                "action": "MOVE_AFTER_AUTO_RECLASSIFICATION",
                "message": "重新分类这个文件",
                "document_ids": [working_copy["document_id"]],
                "conversation_id": protected_run.conversation_id,
                "agent_run_id": protected_run.id,
            },
        )

        assert protected.output_json["status"] == "COMPLETED"
        assert "operation_plan_id" not in protected.output_json
        assert protected.output_json["suggestions"][0]["status"] == "NEEDS_REVIEW"
        assert (
            "CONFIRMED_CATEGORY_PROTECTED"
            in protected.output_json["suggestions"][0]["reason_codes"]
        )
        db.refresh(new_relation)
        db.refresh(current_copy)
        assert new_relation.status == "CONFIRMED"
        assert new_relation.category_id == "school.hr.salary-social-security"
        assert current_copy.relative_path == (
            "学校/人事师资/劳资社保/工资津贴发放情况自查通知.txt"
        )
    finally:
        db.close()
        clear_overrides()


def test_explicit_target_classification_persists_relation_and_moves_immediately(
    monkeypatch,
    tmp_path,
):
    """“将文件分类为 X”必须一次完成正式分类和归位，不再返回确认卡。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "direct-target-classification-owner")
    _upload(client, headers, "待指定分类材料.txt", b"manual classification target")
    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]

    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.username == "direct-target-classification-owner")
            .one()
        )
        taxonomy = load_default_taxonomy()
        run = AgentRun(
            id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            conversation_id="direct-target-classify-conv",
            message_id="direct-target-classify-msg",
            user_id=user.id,
        )
        classification_run = DocumentClassificationRun(
            id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            document_id=working_copy["document_id"],
            agent_run_id=run.id,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            classifier_version="test-direct-target-classification",
            source="rule",
            status="COMPLETED",
        )
        suggestion = DocumentCategorySuggestion(
            id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            classification_run_id=classification_run.id,
            document_id=working_copy["document_id"],
            document_version_id=working_copy["current_version_id"],
            category_id="school.audit",
            category_name="学校/审计",
            category_path_json=["学校", "审计"],
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            confidence=0.7,
            status="SUGGESTED",
            evidence_json=[
                {
                    "type": "text_quote",
                    "page_number": 1,
                    "sheet_name": None,
                    "quote": "manual classification target",
                    "signals": ["classification"],
                    "source": "document_pages",
                }
            ],
            source="rule",
            rank=1,
        )
        db.add_all([run, classification_run, suggestion])
        db.commit()

        result = ToolRegistry(db=db, user_id=user.id).invoke(
            "classification-decision",
            {
                "action": "CORRECT",
                "message": "将这个文件分类为学校/人事师资/考核聘任",
                "document_ids": [working_copy["document_id"]],
                "conversation_id": run.conversation_id,
                "agent_run_id": run.id,
            },
        )

        assert result.output_json["status"] == "COMPLETED"
        assert result.output_json["file_position_changed"] is True
        moved = db.get(WorkingCopy, working_copy["id"])
        assert moved.relative_path == "学校/人事师资/考核聘任/待指定分类材料.txt"
        relation = (
            db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id == working_copy["id"],
                DocumentCategory.status == "CONFIRMED",
                DocumentCategory.relation_role == "PRIMARY",
            )
            .one()
        )
        assert relation.category_id == "school.hr.appointment-assessment"
    finally:
        db.close()
        clear_overrides()
