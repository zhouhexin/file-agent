"""WorkBuddy 完整文档提取 API 的任务、租约和正文回传测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.db.models import ChangeItem, DocumentElement, DocumentExtractionConfirmation, DocumentExtractionRun, DocumentPage, DocumentVersion, ExternalExtractionTask, FilesystemJob, IngestBatch, IngestItem, ToolInvocation, UploadArchiveRecord, UploadDuplicateCandidate, UploadDuplicateReview, User, WorkingCopy
from app.modules.external_extraction.document_schemas import DocumentExtractionSubmitRequest, MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES
from app.modules.external_extraction.document_service import WorkBuddyDocumentExtractionService
from app.modules.external_extraction import document_router
from app.modules.file_lifecycle.service import FileLifecycleJobProcessor
from app.modules.managed_files.worker import process_next_filesystem_job
from app.modules.structured_datasets.tasks import DatasetTaskService
from app.tests.helpers import clear_overrides, client_with_database


@pytest.fixture(autouse=True)
def _enable_document_channel(monkeypatch):
    """新通道默认关闭，测试显式启用三个需要的试点开关。"""

    monkeypatch.setenv("INTEGRATION_INGEST_ENABLED", "true")
    monkeypatch.setenv("WORKBUDDY_DOCUMENT_EXTRACTION_ENABLED", "true")
    monkeypatch.setenv("WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _auth(client) -> dict[str, str]:
    """创建隔离用户并返回集成 API 所需认证头。"""

    client.post("/api/auth/register", json={"username": "document-extraction-user", "password": "password123", "display_name": "文档提取用户"})
    token = client.post("/api/auth/login", json={"username": "document-extraction-user", "password": "password123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _prepare_docx_task(
    client,
    SessionLocal,
    headers,
    content=b"minimal-docx-byte-snapshot",
    *,
    extraction_save_policy="DIRECT_ARCHIVE",
) -> tuple[str, str]:
    """上传固定 DOCX 字节并运行查重阶段，返回条目和新契约任务。"""

    digest = hashlib.sha256(content).hexdigest()
    batch = client.post("/api/integrations/v1/ingest-batches", headers=headers, json={
        "client_id": "workbuddy-attachment", "request_id": "document-task-request", "idempotency_key": "document-task-event",
        "source_root_ref": "workbuddy-attachments", "relative_directory": "submitted-attachments", "recursive": False,
        "user_request": "读取附件并填写登记表", "extraction_profile": "WORKBUDDY_DOCUMENT",
        "extraction_save_policy": extraction_save_policy,
    })
    assert batch.status_code == 201
    batch_id = batch.json()["id"]
    item = client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/items", headers=headers, json={"items": [{
        "client_item_id": "docx-1", "source_root_ref": "workbuddy-attachments", "source_relative_path": "submitted-attachments/docx-1/登记.docx",
        "original_filename": "登记.docx", "size_bytes": len(content), "mtime_ns": 1, "expected_sha256": digest,
    }]}).json()["items"][0]
    client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=headers)
    accepted = client.put(f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content", headers=headers, files={"file": ("登记.docx", content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert accepted.status_code == 202
    with SessionLocal() as db:
        job = db.get(FilesystemJob, accepted.json()["filesystem_job_id"])
        assert job is not None
        assert FileLifecycleJobProcessor(db).process(job) is True
        db.commit()
        task = db.query(ExternalExtractionTask).filter_by(ingest_item_id=item["id"]).one()
        assert task.provider_contract_version == "workbuddy-document-v1"
        return item["id"], task.id


def _submit_docx_with_duplicate_candidate(client, SessionLocal, headers) -> tuple[str, str, str]:
    """固定一条近似查重候选，使完整 WorkBuddy 结果进入用户选择分支。"""

    item_id, task_id = _prepare_docx_task(client, SessionLocal, headers)
    with SessionLocal() as db:
        item = db.get(IngestItem, item_id)
        review = db.query(UploadDuplicateReview).filter_by(ingest_item_id=item_id).one()
        db.add(UploadDuplicateCandidate(
            duplicate_review_id=review.id, candidate_ingest_item_id=item_id,
            compared_version_id=item.upload_document_version_id,
            match_type="NEAR_DUPLICATE", match_scope="SAME_BATCH",
            similarity_score=0.91, match_evidence_json={}, user_visible_summary_json={}, rank=1,
        ))
        db.commit()
        review_id = review.id
    claim = client.post(
        f"/api/integrations/v1/document-extraction-tasks/{task_id}/claim",
        headers=headers, json={"worker_id": "workbuddy-reader"},
    ).json()
    saved = client.post(
        f"/api/integrations/v1/document-extraction-tasks/{task_id}/results",
        headers=headers,
        json={
            "worker_id": "workbuddy-reader", "lease_token": claim["lease_token"],
            "submission_key": "duplicate-docx-submit", "source_sha256": claim["source_sha256"],
            "source_version_id": claim["source_version_id"], "provider_name": "workbuddy-docx-reader",
            "coverage": {"status": "COMPLETE", "unit_count": 1, "warnings": []},
            "units": [{"unit_id": "section-0", "kind": "TEXT_SECTION", "order": 0,
                       "status": "READ", "text": "姓名：样本甲", "blocks": []}],
        },
    )
    assert saved.status_code == 202, saved.text
    assert saved.json()["next_stage"] == "DUPLICATE_DECISION"
    return item_id, task_id, review_id


def _prepare_xls_task(client, SessionLocal, headers) -> tuple[str, str]:
    """上传固定旧版 XLS 快照；服务端不解析正文，只建立 WorkBuddy 工作表提取任务。"""

    content = b"fixed-legacy-xls-snapshot"
    digest = hashlib.sha256(content).hexdigest()
    batch = client.post(
        "/api/integrations/v1/ingest-batches",
        headers=headers,
        json={
            "client_id": "workbuddy-attachment",
            "request_id": "spreadsheet-task-request",
            "idempotency_key": "spreadsheet-task-event",
            "source_root_ref": "workbuddy-attachments",
            "relative_directory": "submitted-attachments",
            "recursive": False,
            "user_request": "读取旧版 Excel 的工作表和字段",
            "extraction_profile": "WORKBUDDY_DOCUMENT",
            "extraction_save_policy": "DIRECT_ARCHIVE",
        },
    )
    assert batch.status_code == 201
    batch_id = batch.json()["id"]
    item = client.post(
        f"/api/integrations/v1/ingest-batches/{batch_id}/items",
        headers=headers,
        json={
            "items": [{
                "client_item_id": "xls-1",
                "source_root_ref": "workbuddy-attachments",
                "source_relative_path": "submitted-attachments/xls-1/业绩汇总.xls",
                "original_filename": "业绩汇总.xls",
                "size_bytes": len(content),
                "mtime_ns": 1,
                "expected_sha256": digest,
            }]
        },
    ).json()["items"][0]
    client.post(
        f"/api/integrations/v1/ingest-batches/{batch_id}/seal",
        headers=headers,
    )
    accepted = client.put(
        f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
        headers=headers,
        files={"file": ("业绩汇总.xls", content, "application/vnd.ms-excel")},
    )
    assert accepted.status_code == 202
    with SessionLocal() as db:
        job = db.get(FilesystemJob, accepted.json()["filesystem_job_id"])
        assert job is not None
        assert FileLifecycleJobProcessor(db).process(job) is True
        db.commit()
        task = db.query(ExternalExtractionTask).filter_by(
            ingest_item_id=item["id"]
        ).one()
        return item["id"], task.id


def _prepare_image_document_task(
    client,
    SessionLocal,
    headers,
) -> tuple[str, str]:
    """以显式提取策略上传图片，验证完整文档通道的 OCR 来源约束。"""

    content = b"workbuddy-document-image-snapshot"
    digest = hashlib.sha256(content).hexdigest()
    batch_id = client.post(
        "/api/integrations/v1/ingest-batches",
        headers=headers,
        json={
            "client_id": "workbuddy-attachment",
            "request_id": "document-image-request",
            "idempotency_key": "document-image-event",
            "source_root_ref": "workbuddy-attachments",
            "relative_directory": "submitted-attachments",
            "recursive": False,
            "user_request": "识别图片中的姓名并生成登记表",
            "extraction_profile": "WORKBUDDY_DOCUMENT",
            "extraction_save_policy": "CONFIRM_BEFORE_SAVE",
        },
    ).json()["id"]
    item = client.post(
        f"/api/integrations/v1/ingest-batches/{batch_id}/items",
        headers=headers,
        json={
            "items": [{
                "client_item_id": "image-1",
                "source_root_ref": "workbuddy-attachments",
                "source_relative_path": "submitted-attachments/image-1/登记.png",
                "original_filename": "登记.png",
                "size_bytes": len(content),
                "mtime_ns": 1,
                "expected_sha256": digest,
            }]
        },
    ).json()["items"][0]
    client.post(
        f"/api/integrations/v1/ingest-batches/{batch_id}/seal",
        headers=headers,
    )
    accepted = client.put(
        f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
        headers=headers,
        files={"file": ("登记.png", content, "image/png")},
    )
    with SessionLocal() as db:
        job = db.get(FilesystemJob, accepted.json()["filesystem_job_id"])
        assert job is not None
        assert FileLifecycleJobProcessor(db).process(job) is True
        db.commit()
        task = db.query(ExternalExtractionTask).filter_by(
            ingest_item_id=item["id"]
        ).one()
        return item["id"], task.id


def test_document_image_preview_preserves_actual_workbuddy_ocr_provider(monkeypatch, tmp_path) -> None:
    """WorkBuddy 可选择任意可用 OCR Provider，后端应原样保留实际来源。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        _, task_id = _prepare_image_document_task(client, SessionLocal, headers)
        claim = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/claim",
            headers=headers,
            json={"worker_id": "workbuddy-image-ocr"},
        ).json()
        preview_payload = {
            "worker_id": "workbuddy-image-ocr",
            "lease_token": claim["lease_token"],
            "preview_key": "image-preview-1",
            "source_sha256": claim["source_sha256"],
            "source_version_id": claim["source_version_id"],
            "result_digest": "a" * 64,
            "provider_name": "workbuddy-ocr-plugin",
            "coverage_status": "COMPLETE",
            "unit_count": 1,
            "block_count": 0,
        }
        accepted = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/confirmation-previews",
            headers=headers,
            json=preview_payload,
        )
        assert accepted.status_code == 200
        assert accepted.json()["summary"]["provider_name"] == "workbuddy-ocr-plugin"
    finally:
        clear_overrides()


def test_legacy_xls_uses_workbuddy_spreadsheet_units(monkeypatch, tmp_path) -> None:
    """旧版 XLS 由 WorkBuddy 读取为带工作表名和单元格坐标的 SHEET，不得误标为 IMAGE。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        _, task_id = _prepare_xls_task(client, SessionLocal, headers)
        claim = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/claim",
            headers=headers,
            json={"worker_id": "workbuddy-spreadsheet-reader"},
        )
        assert claim.status_code == 200, claim.text
        claimed = claim.json()
        assert claimed["source"]["source_kind"] == "SPREADSHEET"
        submission = {
            "worker_id": "workbuddy-spreadsheet-reader",
            "lease_token": claimed["lease_token"],
            "submission_key": "xls-submit-1",
            "source_sha256": claimed["source_sha256"],
            "source_version_id": claimed["source_version_id"],
            "provider_name": "workbuddy-office-reader",
            "coverage": {"status": "COMPLETE", "unit_count": 1, "warnings": []},
            "units": [{
                "unit_id": "sheet-0",
                "kind": "SHEET",
                "sheet_name": "业绩汇总",
                "page_number": None,
                "order": 0,
                "status": "READ",
                "text": "姓名\t金额",
                "blocks": [{
                    "block_id": "cell-a1",
                    "kind": "TABLE_CELL",
                    "text": "姓名",
                    "start": 0,
                    "end": 2,
                    "row_index": 0,
                    "column_index": 0,
                }],
            }],
        }
        saved = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/results",
            headers=headers,
            json=submission,
        )
        assert saved.status_code == 202, saved.text
        with SessionLocal() as db:
            task = db.get(ExternalExtractionTask, task_id)
            page = db.query(DocumentPage).filter_by(
                extraction_run_id=task.extraction_run_id
            ).one()
            element = db.query(DocumentElement).filter_by(
                extraction_run_id=task.extraction_run_id
            ).one()
            assert page.page_number is None
            assert page.metadata_json["unit_kind"] == "SHEET"
            assert page.metadata_json["sheet_name"] == "业绩汇总"
            assert element.metadata_json["row_index"] == 0
            assert element.metadata_json["column_index"] == 0
    finally:
        clear_overrides()


def _drain_lifecycle_jobs(SessionLocal, maximum: int = 30) -> None:
    """在测试内依次运行当前队列，验证发布后的完整复用链路。"""

    for _ in range(maximum):
        if process_next_filesystem_job(session_factory=SessionLocal, worker_id="document-extraction-test") is None:
            return
    raise AssertionError("文档提取测试的生命周期任务没有在预期次数内收敛")


def test_document_claim_download_and_complete_submit_persists_workbuddy_origin(monkeypatch, tmp_path) -> None:
    """DOCX 正文只能由 WorkBuddy 回传，File Agent 保存证据但不调用本地解析器。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        item_id, task_id = _prepare_docx_task(client, SessionLocal, headers)
        claim = client.post(f"/api/integrations/v1/document-extraction-tasks/{task_id}/claim", headers=headers, json={"worker_id": "workbuddy-reader"})
        assert claim.status_code == 200
        claimed = claim.json()
        source = client.get(claimed["source"]["resource_url"], headers={**headers, "X-Extraction-Lease-Token": claimed["lease_token"]}, params={"worker_id": "workbuddy-reader"})
        assert source.status_code == 200
        assert source.content == b"minimal-docx-byte-snapshot"
        submission = {
            "worker_id": "workbuddy-reader", "lease_token": claimed["lease_token"], "submission_key": "docx-submit-1",
            "source_sha256": claimed["source_sha256"], "source_version_id": claimed["source_version_id"],
            "provider_name": "workbuddy-docx-reader", "provider_version": "1", "coverage": {"status": "COMPLETE", "unit_count": 1, "warnings": []},
            "units": [{"unit_id": "section-0", "kind": "TEXT_SECTION", "page_number": None, "order": 0, "status": "READ", "text": "姓名：样本甲\n金额：100", "blocks": [{"block_id": "p-0", "kind": "PARAGRAPH", "text": "姓名：样本甲", "start": 0, "end": 6}]}],
        }
        response = client.post(f"/api/integrations/v1/document-extraction-tasks/{task_id}/results", headers=headers, json=submission)
        replay = client.post(f"/api/integrations/v1/document-extraction-tasks/{task_id}/results", headers=headers, json=submission)
        assert response.status_code == 202
        assert response.json()["ok"] is True
        assert response.json()["next_stage"] == "ARCHIVE"
        assert replay.json()["reused"] is True
        assert replay.json()["ok"] is True
        assert replay.json()["filesystem_job_id"] == response.json()["filesystem_job_id"]
        # 文档契约的终态是 COMPLETE，成功提交后不能再次领取并重复 OCR。
        repeated_claim = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/claim",
            headers=headers, json={"worker_id": "another-worker"},
        )
        assert repeated_claim.status_code == 409
        assert repeated_claim.json()["error"]["code"] == "EXTRACTION_ALREADY_COMPLETED"
        # 伪 DOCX 一旦重新落入服务器解析器必然失败；用抛错桩证明发布、分类和索引
        # 只消费宿主已验收的正文与元素。
        def _server_parse_forbidden(*args, **kwargs):
            raise AssertionError("WORKBUDDY_DOCUMENT 不得调用服务器正文解析")

        monkeypatch.setattr("app.modules.file_rename.parsing_service.extract_document_text", _server_parse_forbidden)
        monkeypatch.setattr("app.modules.file_rename.parsing_service.extract_document_text_native", _server_parse_forbidden)
        _drain_lifecycle_jobs(SessionLocal)
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            version = db.get(DocumentVersion, item.upload_document_version_id)
            run = db.get(DocumentExtractionRun, item.extraction_run_id)
            page = db.query(DocumentPage).filter_by(extraction_run_id=run.id).one()
            element = db.query(DocumentElement).filter_by(extraction_run_id=run.id).one()
            assert version.extraction_policy_json["mode"] == "WORKBUDDY_DOCUMENT"
            assert run.extractor == "workbuddy-external-document"
            assert page.page_number is None
            assert page.metadata_json["content_origin"] == "WORKBUDDY"
            assert element.metadata_json["source_block_id"] == "p-0"
            working_copy = db.query(WorkingCopy).order_by(WorkingCopy.created_at.desc()).one()
            working_version = db.get(DocumentVersion, working_copy.current_version_id)
            working_run = db.query(DocumentExtractionRun).filter_by(
                document_id=working_copy.document_id,
                document_version_id=working_version.id,
                extractor="workbuddy-external-document",
                status="COMPLETED",
            ).one()
            assert working_version.extraction_policy_json["mode"] == "WORKBUDDY_DOCUMENT"
            assert db.query(DocumentElement).filter_by(extraction_run_id=working_run.id).count() == 1
            invocation_names = {
                row.tool_name for row in db.query(ToolInvocation).filter(
                    ToolInvocation.tool_name.in_(["document_extraction_claim", "document_extraction_submit"])
                ).all()
            }
            assert invocation_names == {"document_extraction_claim", "document_extraction_submit"}
            change_types = {
                row.change_type for row in db.query(ChangeItem).filter(
                    ChangeItem.change_type.in_([
                        "DOCUMENT_EXTRACTION_CLAIMED", "TEXT_EXTRACTED", "DOCUMENT_PAGES_CREATED",
                    ])
                ).all()
            }
            assert change_types == {"DOCUMENT_EXTRACTION_CLAIMED", "TEXT_EXTRACTED", "DOCUMENT_PAGES_CREATED"}
    finally:
        clear_overrides()


def test_partial_document_result_is_saved_but_not_archived(monkeypatch, tmp_path) -> None:
    """PARTIAL 结果必须保留诊断并允许重试，不能因旧 OCR 部分发布策略进入归档。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        item_id, task_id = _prepare_docx_task(client, SessionLocal, headers)
        claim = client.post(f"/api/integrations/v1/document-extraction-tasks/{task_id}/claim", headers=headers, json={"worker_id": "workbuddy-reader"}).json()
        result = client.post(f"/api/integrations/v1/document-extraction-tasks/{task_id}/results", headers=headers, json={
            "worker_id": "workbuddy-reader", "lease_token": claim["lease_token"], "submission_key": "docx-partial-1", "source_sha256": claim["source_sha256"], "source_version_id": claim["source_version_id"], "provider_name": "workbuddy-docx-reader", "coverage": {"status": "PARTIAL", "unit_count": 1, "warnings": ["内嵌图片未识别"]}, "units": [{"unit_id": "section-0", "kind": "TEXT_SECTION", "order": 0, "status": "READ", "text": "正文", "blocks": []}],
        })
        assert result.status_code == 202
        assert result.json()["ok"] is False
        assert result.json()["filesystem_job_id"] is None
        # 不完整结果不能借恢复入口跳过再次提取或用户确认。
        resume = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/resume", headers=headers,
        )
        assert resume.status_code == 409
        assert resume.json()["error"]["code"] == "EXTRACTION_RECOVERY_NOT_AVAILABLE"
        with SessionLocal() as db:
            task = db.get(ExternalExtractionTask, task_id)
            item = db.get(IngestItem, item_id)
            assert task.status == "PARTIAL"
            assert item.status == "WAITING_EXTERNAL_EXTRACTION"
            invocation = db.query(ToolInvocation).filter_by(tool_name="document_extraction_submit").one()
            assert invocation.status == "FAILED"
            failure = db.query(ChangeItem).filter_by(change_type="DOCUMENT_PROCESSING_FAILED").one()
            assert failure.execution_status == "FAILED"
    finally:
        clear_overrides()


def test_document_result_rejects_oversized_raw_body_before_schema_validation(monkeypatch, tmp_path) -> None:
    """请求体上限必须在构造深层 Pydantic 对象前生效。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        _, task_id = _prepare_docx_task(client, SessionLocal, headers)
        response = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/results",
            headers={**headers, "Content-Type": "application/json"},
            content=b"x" * (MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES + 1),
        )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "EXTRACTION_PAYLOAD_TOO_LARGE"
    finally:
        clear_overrides()


def test_expired_submit_does_not_block_event_loop_and_rolls_back(monkeypatch) -> None:
    """过期租约持锁失败时，其他异步请求仍可运行，事务须在响应错误前释放。"""

    entered = threading.Event()
    release = threading.Event()

    class FakeRequest:
        """只提供路由所需的已编码请求体。"""

        async def body(self):
            return json.dumps({
                "worker_id": "workbuddy-reader", "lease_token": "lease-token-value-123456789",
                "submission_key": "expired-submit", "source_sha256": "a" * 64,
                "source_version_id": "11111111-1111-4111-8111-111111111111",
                "provider_name": "test-provider",
                "coverage": {"status": "PARTIAL", "unit_count": 1, "warnings": []},
                "units": [{"unit_id": "section-0", "kind": "TEXT_SECTION", "order": 0,
                           "status": "READ", "text": "已读取原文", "blocks": []}],
            }).encode("utf-8")

    class FakeSession:
        """记录错误分支是否主动回滚。"""

        rollback_count = 0

        def rollback(self):
            self.rollback_count += 1

    class FakeService:
        """模拟租约校验持锁后返回业务错误。"""

        def __init__(self, db):
            self.db = db

        def submit(self, **kwargs):
            entered.set()
            release.wait(timeout=0.5)
            raise HTTPException(409, detail={"code": "LEASE_EXPIRED"})

    monkeypatch.setattr(document_router, "WorkBuddyDocumentExtractionService", FakeService)
    db = FakeSession()

    async def scenario():
        task = asyncio.create_task(document_router.submit_document_extraction(
            "task-id", FakeRequest(), current_user=object(), db=db,
        ))
        await asyncio.sleep(0.05)
        # 旧实现会把同步数据库等待放在事件循环，直到 0.5 秒超时才恢复。
        assert entered.is_set() and not task.done()
        release.set()
        with pytest.raises(HTTPException) as error:
            await task
        assert error.value.status_code == 409
        assert db.rollback_count == 1

    asyncio.run(scenario())


def test_expired_renew_rolls_back_before_returning_error(monkeypatch) -> None:
    """续租在持有任务行锁后拒绝过期 token 时，不得留下未结束事务。"""

    class FakeSession:
        """记录异常路径的事务收尾。"""

        rollback_count = 0

        def rollback(self):
            self.rollback_count += 1

    class FakeService:
        """模拟续租校验已获取行锁后发现租约过期。"""

        def __init__(self, db):
            self.db = db

        def renew(self, **kwargs):
            raise HTTPException(409, detail={"code": "LEASE_EXPIRED"})

    monkeypatch.setattr(document_router, "WorkBuddyDocumentExtractionService", FakeService)
    db = FakeSession()
    request = document_router.ExternalExtractionRenewRequest(
        worker_id="workbuddy-reader", lease_token="lease-token-value-123456789",
    )
    with pytest.raises(HTTPException) as error:
        document_router.renew_document_extraction(
            "task-id", request, current_user=object(), db=db,
        )
    assert error.value.status_code == 409
    assert db.rollback_count == 1


def test_completed_document_run_can_resume_stale_claim_without_reextracting(monkeypatch, tmp_path) -> None:
    """旧版误领后仅凭已保存完整运行恢复原归档 job，不重存页面或新建租约。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        item_id, task_id = _prepare_docx_task(client, SessionLocal, headers)
        claim = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/claim",
            headers=headers, json={"worker_id": "workbuddy-reader"},
        ).json()
        submission = {
            "worker_id": "workbuddy-reader", "lease_token": claim["lease_token"],
            "submission_key": "resume-submit-1", "source_sha256": claim["source_sha256"],
            "source_version_id": claim["source_version_id"], "provider_name": "workbuddy-docx-reader",
            "coverage": {"status": "COMPLETE", "unit_count": 1, "warnings": []},
            "units": [{"unit_id": "section-0", "kind": "TEXT_SECTION", "order": 0,
                       "status": "READ", "text": "姓名：样本甲", "blocks": []}],
        }
        saved = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/results",
            headers=headers, json=submission,
        )
        assert saved.status_code == 202
        original_job_id = saved.json()["filesystem_job_id"]
        with SessionLocal() as db:
            task = db.get(ExternalExtractionTask, task_id)
            item = db.get(IngestItem, item_id)
            task.status = "CLAIMED"
            task.lease_owner = "old-worker"
            task.lease_token_hash = "stale-hash"
            item.status = "WAITING_EXTERNAL_EXTRACTION"
            item.stage = "PARSE_STAGING"
            item.current_job_id = None
            db.commit()
        blocked = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/claim",
            headers=headers, json={"worker_id": "new-worker"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "EXTRACTION_ALREADY_COMPLETED"
        resumed = client.post(f"/api/integrations/v1/document-extraction-tasks/{task_id}/resume", headers=headers)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["resumed"] is True
        assert resumed.json()["task"]["status"] == "COMPLETE"
        assert resumed.json()["filesystem_job_id"] == original_job_id
        repeated = client.post(f"/api/integrations/v1/document-extraction-tasks/{task_id}/resume", headers=headers)
        assert repeated.status_code == 200
        assert repeated.json()["resumed"] is False
        assert repeated.json()["filesystem_job_id"] == original_job_id
        with SessionLocal() as db:
            assert db.query(DocumentExtractionRun).filter_by(document_version_id=claim["source_version_id"]).count() == 1
            assert db.query(FilesystemJob).filter_by(id=original_job_id).count() == 1
    finally:
        clear_overrides()


def test_confirmed_duplicate_archives_completed_workbuddy_document(monkeypatch, tmp_path) -> None:
    """用户已决定继续上传时，COMPLETE 文档运行应建立归档 job，不得退回提取等待。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        item_id, task_id, review_id = _submit_docx_with_duplicate_candidate(client, SessionLocal, headers)
        with SessionLocal() as db:
            review = db.get(UploadDuplicateReview, review_id)
            assert review.status == "WAITING_CONFIRMATION"
            assert review.comparison_phase == "NEAR"
            revision = review.revision
        decided = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/duplicate-decision",
            headers=headers,
            json={
                "client_id": "workbuddy-attachment", "request_id": "confirmed-near-duplicate",
                "idempotency_key": "confirmed-near-duplicate", "review_id": review_id,
                "review_revision": revision, "decision": "CONTINUE_UPLOAD",
            },
        )
        assert decided.status_code == 202, decided.text
        job_id = decided.json()["filesystem_job_id"]
        assert job_id
        with SessionLocal() as db:
            task = db.get(ExternalExtractionTask, task_id)
            item = db.get(IngestItem, item_id)
            archive = db.get(UploadArchiveRecord, item.archive_record_id)
            job = db.get(FilesystemJob, job_id)
            assert task.status == "COMPLETE"
            assert item.stage == "ARCHIVE"
            assert archive.status == "PENDING"
            assert archive.filesystem_job_id == job_id
            assert job.job_type == "ARCHIVE_UPLOAD_TO_MANAGED_ROOT"
            assert job.status == "PENDING"
        _drain_lifecycle_jobs(SessionLocal)
        with SessionLocal() as db:
            task = db.get(ExternalExtractionTask, task_id)
            item = db.get(IngestItem, item_id)
            batch = db.get(IngestBatch, item.batch_id)
            # 模拟异步 worker 已发布副本、上传条目投影仍滞后的真实窗口。
            item.status = "RUNNING"
            item.stage = "ARCHIVE"
            item.final_document_id = None
            item.final_version_id = None
            item.final_working_copy_id = None
            db.commit()
            binding = DatasetTaskService(db, db.get(User, batch.user_id))._working(
                task_id, {task_id: {"version_id": task.source_version_id, "sha256": task.source_sha256}},
            )
            assert binding is not None
            assert binding["source_sha256"] == task.source_sha256
            assert item.status == "SUCCEEDED"
            assert item.final_working_copy_id is not None
    finally:
        clear_overrides()


def test_resume_confirmed_near_duplicate_creates_only_missing_archive_job(monkeypatch, tmp_path) -> None:
    """旧错误已确认但无归档 job 时，只沿原提取运行恢复一次归档，不重判或重读正文。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        item_id, task_id, review_id = _submit_docx_with_duplicate_candidate(client, SessionLocal, headers)
        with SessionLocal() as db:
            task = db.get(ExternalExtractionTask, task_id)
            item = db.get(IngestItem, item_id)
            review = db.get(UploadDuplicateReview, review_id)
            archive = db.get(UploadArchiveRecord, item.archive_record_id)
            # 模拟旧版状态错配：用户已确认继续，但归档 job 没有入队。
            review.status = "RESOLVED"
            review.decision = "CONTINUE_UPLOAD"
            review.decision_scope_json = {"decision": "CONTINUE_UPLOAD"}
            item.decision = "CONTINUE_UPLOAD"
            item.status = "WAITING_EXTERNAL_EXTRACTION"
            item.stage = "PARSE_STAGING"
            archive.status = "WAITING_EXTERNAL_EXTRACTION"
            archive.filesystem_job_id = None
            db.query(UploadDuplicateCandidate).filter_by(duplicate_review_id=review_id).delete()
            original_run_id = task.extraction_run_id
            db.commit()
        resumed = client.post(f"/api/integrations/v1/document-extraction-tasks/{task_id}/resume", headers=headers)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["resumed"] is True
        assert resumed.json()["next_stage"] == "ARCHIVE"
        job_id = resumed.json()["filesystem_job_id"]
        assert job_id
        repeated = client.post(f"/api/integrations/v1/document-extraction-tasks/{task_id}/resume", headers=headers)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["resumed"] is False
        assert repeated.json()["filesystem_job_id"] == job_id
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            archive = db.get(UploadArchiveRecord, item.archive_record_id)
            review = db.get(UploadDuplicateReview, review_id)
            assert item.status == "RUNNING" and item.stage == "ARCHIVE"
            assert archive.filesystem_job_id == job_id and archive.status == "PENDING"
            assert review.status == "RESOLVED" and review.decision == "CONTINUE_UPLOAD"
            assert db.query(FilesystemJob).filter_by(deduplication_key=f"upload-archive:{item.upload_document_version_id}").count() == 1
            assert db.query(DocumentExtractionRun).filter_by(id=original_run_id).count() == 1
    finally:
        clear_overrides()


def test_explicit_extraction_requires_matching_user_confirmation_before_persistence(monkeypatch, tmp_path) -> None:
    """显式提取在确认前不能保存正文，确认只授权用户看到的完全相同结果。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        _, task_id = _prepare_docx_task(
            client,
            SessionLocal,
            headers,
            extraction_save_policy="CONFIRM_BEFORE_SAVE",
        )
        claim = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/claim",
            headers=headers,
            json={"worker_id": "workbuddy-reader"},
        ).json()
        assert claim["confirmation_required"] is True
        assert claim["extraction_save_policy"] == "CONFIRM_BEFORE_SAVE"
        submission = {
            "worker_id": "workbuddy-reader",
            "lease_token": claim["lease_token"],
            "submission_key": "confirmed-submit-1",
            "source_sha256": claim["source_sha256"],
            "source_version_id": claim["source_version_id"],
            "provider_name": "workbuddy-docx-reader",
            "provider_version": "1",
            "provider_request_id": None,
            "coverage": {"status": "COMPLETE", "unit_count": 1, "warnings": []},
            "units": [{
                "unit_id": "section-0", "kind": "TEXT_SECTION", "page_number": None,
                "order": 0, "status": "READ", "text": "姓名：样本甲\n金额：100", "blocks": [],
            }],
            "error": None,
        }
        blocked = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/results",
            headers=headers,
            json=submission,
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "EXTRACTION_CONFIRMATION_REQUIRED"
        with SessionLocal() as db:
            task = db.get(ExternalExtractionTask, task_id)
            assert db.query(DocumentPage).filter_by(document_id=db.get(DocumentVersion, task.source_version_id).document_id).count() == 0
            assert db.query(DocumentExtractionConfirmation).count() == 0

        validated = DocumentExtractionSubmitRequest.model_validate(submission)
        result_digest = WorkBuddyDocumentExtractionService._submission_digest(validated)
        preview = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/confirmation-previews",
            headers=headers,
            json={
                "worker_id": submission["worker_id"],
                "lease_token": submission["lease_token"],
                "preview_key": "preview-1",
                "source_sha256": submission["source_sha256"],
                "source_version_id": submission["source_version_id"],
                "result_digest": result_digest,
                "provider_name": submission["provider_name"],
                "coverage_status": "COMPLETE",
                "unit_count": 1,
                "block_count": 0,
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["status"] == "WAITING_CONFIRMATION"
        confirmation_id = preview.json()["id"]
        with SessionLocal() as db:
            assert db.query(DocumentPage).count() == 0
            confirmation = db.get(DocumentExtractionConfirmation, confirmation_id)
            assert confirmation.summary_json == {
                "provider_name": "workbuddy-docx-reader",
                "coverage_status": "COMPLETE",
                "unit_count": 1,
                "block_count": 0,
            }

        confirmed = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/confirmation-previews/{confirmation_id}/confirm",
            headers=headers,
            json={"result_digest": result_digest},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "CONFIRMED"
        changed = {
            **submission,
            "submission_key": "changed-submit",
            "units": [{**submission["units"][0], "text": "姓名：被修改"}],
        }
        rejected = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/results",
            headers=headers,
            json=changed,
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "EXTRACTION_RESULT_CHANGED_AFTER_CONFIRMATION"
        saved = client.post(
            f"/api/integrations/v1/document-extraction-tasks/{task_id}/results",
            headers=headers,
            json=submission,
        )
        assert saved.status_code == 202
        assert saved.json()["ok"] is True
        with SessionLocal() as db:
            confirmation = db.get(DocumentExtractionConfirmation, confirmation_id)
            assert confirmation.status == "SAVED"
            assert confirmation.saved_at is not None
            assert db.query(DocumentPage).count() == 1
            names = {row.tool_name for row in db.query(ToolInvocation).all()}
            assert {"document_extraction_preview", "document_extraction_confirm", "document_extraction_submit"} <= names
    finally:
        clear_overrides()
