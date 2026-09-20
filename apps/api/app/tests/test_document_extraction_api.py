"""WorkBuddy 完整文档提取 API 的任务、租约和正文回传测试。"""

from __future__ import annotations

import hashlib

import pytest

from app.core.config import get_settings
from app.db.models import ChangeItem, DocumentElement, DocumentExtractionRun, DocumentPage, DocumentVersion, ExternalExtractionTask, FilesystemJob, IngestItem, ToolInvocation, WorkingCopy
from app.modules.external_extraction.document_schemas import MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES
from app.modules.file_lifecycle.service import FileLifecycleJobProcessor
from app.modules.managed_files.worker import process_next_filesystem_job
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


def _prepare_docx_task(client, SessionLocal, headers, content=b"minimal-docx-byte-snapshot") -> tuple[str, str]:
    """上传固定 DOCX 字节并运行查重阶段，返回条目和新契约任务。"""

    digest = hashlib.sha256(content).hexdigest()
    batch = client.post("/api/integrations/v1/ingest-batches", headers=headers, json={
        "client_id": "workbuddy-attachment", "request_id": "document-task-request", "idempotency_key": "document-task-event",
        "source_root_ref": "workbuddy-attachments", "relative_directory": "submitted-attachments", "recursive": False,
        "user_request": "读取附件并填写登记表", "extraction_profile": "WORKBUDDY_DOCUMENT",
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
