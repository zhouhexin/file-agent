"""WorkBuddy 外部 OCR 任务、租约、页资源和正式正文回写测试。"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.db.models import (
    DocumentExtractionRun,
    DocumentOrganizationDecision,
    DocumentPage,
    ExternalExtractionPage,
    ExternalExtractionTask,
    FilesystemJob,
    IngestItem,
    WorkingCopy,
    utcnow,
)
from app.modules.external_extraction.service import ExternalExtractionService
from app.modules.file_lifecycle.service import FileLifecycleJobProcessor
from app.modules.managed_files.worker import process_next_filesystem_job
from app.tests.helpers import clear_overrides, client_with_database


@pytest.fixture(autouse=True)
def _enable_integration_channel(monkeypatch):
    """外部 OCR 属于试点集成面，测试必须显式启用而不能依赖生产默认值。"""

    monkeypatch.setenv("INTEGRATION_INGEST_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _auth(client) -> dict[str, str]:
    """创建独立用户并返回认证头。"""

    client.post(
        "/api/auth/register",
        json={"username": "external-ocr-user", "password": "password123", "display_name": "OCR用户"},
    )
    token = client.post(
        "/api/auth/login",
        json={"username": "external-ocr-user", "password": "password123"},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _prepare_image_task(client, SessionLocal, headers: dict[str, str]) -> tuple[str, str, str]:
    """上传图片并执行精确查重阶段，返回批次、条目和外部任务 ID。"""

    content = b"fake-image-content-for-lease-test"
    digest = hashlib.sha256(content).hexdigest()
    batch_id = client.post(
        "/api/integrations/v1/ingest-batches",
        headers=headers,
        json={
            "client_id": "workbuddy-local",
            "request_id": "ocr-batch-request",
            "idempotency_key": "ocr-batch-event",
            "source_root_ref": "materials",
            "relative_directory": "scan",
            "user_request": None,
        },
    ).json()["id"]
    item = client.post(
        f"/api/integrations/v1/ingest-batches/{batch_id}/items",
        headers=headers,
        json={
            "items": [
                {
                    "client_item_id": "scan-1",
                    "source_root_ref": "materials",
                    "source_relative_path": "scan/page.png",
                    "original_filename": "page.png",
                    "size_bytes": len(content),
                    "mtime_ns": 1,
                    "expected_sha256": digest,
                }
            ]
        },
    ).json()["items"][0]
    client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=headers)
    accepted = client.put(
        f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
        headers=headers,
        files={"file": ("page.png", content, "image/png")},
    )
    assert accepted.status_code == 202
    with SessionLocal() as db:
        job = db.get(FilesystemJob, accepted.json()["filesystem_job_id"])
        assert job is not None
        assert FileLifecycleJobProcessor(db).process(job) is True
        db.commit()
        task = db.query(ExternalExtractionTask).filter(ExternalExtractionTask.ingest_item_id == item["id"]).one()
        return batch_id, item["id"], task.id


def test_external_ocr_claim_page_submit_and_idempotent_replay(monkeypatch, tmp_path) -> None:
    """外部 OCR 正文必须随归档复制到工作副本，正式分析不得再次调用内部 OCR。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "true")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        batch_id, item_id, task_id = _prepare_image_task(client, SessionLocal, headers)
        batch = client.get(f"/api/integrations/v1/ingest-batches/{batch_id}", headers=headers)
        assert batch.json()["counts"]["waiting_external_extraction"] == 1

        claimed = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/claim",
            headers=headers,
            json={"worker_id": "workbuddy-worker-1"},
        )
        assert claimed.status_code == 200
        claim = claimed.json()
        assert claim["pages"][0]["resource_url"].endswith(f"/{task_id}/pages/1")
        page = client.get(
            claim["pages"][0]["resource_url"],
            headers={
                **headers,
                "X-Extraction-Lease-Token": claim["lease_token"],
            },
            params={"worker_id": "workbuddy-worker-1"},
        )
        assert page.status_code == 200
        assert page.content == b"fake-image-content-for-lease-test"

        submission = {
            "worker_id": "workbuddy-worker-1",
            "lease_token": claim["lease_token"],
            "submission_key": "ocr-submit-1",
            "source_sha256": claim["source_sha256"],
            "source_version_id": claim["source_version_id"],
            "pages": [
                {
                    "page_number": 1,
                    "text": "国家励志奖学金申请材料",
                    "confidence": 0.93,
                    "blocks": [],
                    "provider_name": "workbuddy-ocr",
                    "provider_version": "1",
                    "provider_request_id": "provider-1",
                    "error": None,
                }
            ],
        }
        submitted = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/results",
            headers=headers,
            json=submission,
        )
        replay = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/results",
            headers=headers,
            json=submission,
        )
        assert submitted.status_code == 202
        assert submitted.json()["next_stage"] == "ARCHIVE"
        assert submitted.json()["filesystem_job_id"]
        assert replay.status_code == 202
        assert replay.json()["reused"] is True
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            assert item and item.extraction_run_id
            page_row = db.query(DocumentPage).filter(DocumentPage.extraction_run_id == item.extraction_run_id).one()
            assert page_row.text_content == "国家励志奖学金申请材料"
            assert page_row.metadata_json["ocr_confidence"] == 0.93

        # 继续跑完归档、工作副本发布和分析，保护外部 OCR 正文跨版本复用边界。
        processed = 0
        while process_next_filesystem_job(
            session_factory=SessionLocal,
            worker_id="external-ocr-publish-test",
        ):
            processed += 1
            assert processed < 12
        # 批次查询负责把异步文件生命周期事实投影回条目和最终回执。
        recovered = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=headers,
        )
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "SUCCEEDED"
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            working_copy = db.get(WorkingCopy, item.final_working_copy_id)
            assert working_copy is not None
            copied_run = (
                db.query(DocumentExtractionRun)
                .filter_by(document_version_id=working_copy.current_version_id)
                .one()
            )
            copied_page = db.query(DocumentPage).filter_by(extraction_run_id=copied_run.id).one()
            organization = db.query(DocumentOrganizationDecision).filter_by(
                working_copy_id=working_copy.id
            ).one()
            assert copied_run.extractor == "workbuddy-external-ocr"
            assert copied_page.text_content == "国家励志奖学金申请材料"
            assert copied_page.metadata_json["reused_from_upload_extraction"] == item.extraction_run_id
            assert organization.authorization_source == "WORKBUDDY_INGEST_POLICY"
            assert working_copy.status == "ACTIVE"
    finally:
        clear_overrides()


def test_mixed_pdf_sends_only_blank_page_and_preserves_native_text(monkeypatch, tmp_path) -> None:
    """混合 PDF 只外发缺字页，合并结果时不得用 OCR 覆盖原生文字层。"""

    import fitz

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "true")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    pdf = fitz.open()
    native_page = pdf.new_page()
    native_page.insert_text((72, 72), "Native searchable scholarship text")
    pdf.new_page()
    content = pdf.tobytes()
    pdf.close()
    digest = hashlib.sha256(content).hexdigest()
    try:
        headers = _auth(client)
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=headers,
            json={
                "client_id": "workbuddy-local",
                "request_id": "mixed-pdf-request",
                "idempotency_key": "mixed-pdf-event",
                "source_root_ref": "materials",
                "relative_directory": "scan",
                "user_request": None,
            },
        ).json()["id"]
        item = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=headers,
            json={
                "items": [{
                    "client_item_id": "mixed-pdf-1",
                    "source_root_ref": "materials",
                    "source_relative_path": "scan/mixed.pdf",
                    "original_filename": "mixed.pdf",
                    "size_bytes": len(content),
                    "mtime_ns": 1,
                    "expected_sha256": digest,
                }]
            },
        ).json()["items"][0]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=headers)
        accepted = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
            headers=headers,
            files={"file": ("mixed.pdf", content, "application/pdf")},
        )
        assert accepted.status_code == 202
        with SessionLocal() as db:
            job = db.get(FilesystemJob, accepted.json()["filesystem_job_id"])
            assert job is not None
            assert FileLifecycleJobProcessor(db).process(job) is True
            db.commit()
            task = db.query(ExternalExtractionTask).filter_by(ingest_item_id=item["id"]).one()
            task_id = task.id
            assert [entry["page_number"] for entry in task.page_manifest_json] == [2]

        claim = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/claim",
            headers=headers,
            json={"worker_id": "mixed-pdf-worker"},
        ).json()
        submitted = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/results",
            headers=headers,
            json={
                "worker_id": "mixed-pdf-worker",
                "lease_token": claim["lease_token"],
                "submission_key": "mixed-pdf-submit",
                "source_sha256": claim["source_sha256"],
                "source_version_id": claim["source_version_id"],
                "pages": [{
                    "page_number": 2,
                    "text": "OCR text for scanned second page",
                    "confidence": 0.91,
                    "blocks": [],
                    "provider_name": "fake-ocr",
                }],
            },
        )
        assert submitted.status_code == 202
        with SessionLocal() as db:
            task = db.get(ExternalExtractionTask, task_id)
            pages = db.query(DocumentPage).filter_by(
                extraction_run_id=task.extraction_run_id
            ).order_by(DocumentPage.page_number.asc()).all()
            assert [page.page_number for page in pages] == [1, 2]
            assert "Native searchable scholarship text" in pages[0].text_content
            assert pages[0].metadata_json["native_text_layer"] is True
            assert pages[1].text_content == "OCR text for scanned second page"
            assert pages[1].metadata_json["ocr_source"] == "workbuddy_external"
    finally:
        clear_overrides()


def test_external_ocr_rejects_wrong_page_scope_and_invalid_lease(monkeypatch, tmp_path) -> None:
    """无效租约或缺页结果不能进入正式解析表。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "true")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        _, item_id, task_id = _prepare_image_task(client, SessionLocal, headers)
        claimed = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/claim",
            headers=headers,
            json={"worker_id": "worker-a"},
        ).json()
        invalid_page = client.get(
            f"/api/integrations/v1/extraction-tasks/{task_id}/pages/1",
            headers={**headers, "X-Extraction-Lease-Token": "x" * 24},
            params={"worker_id": "worker-a"},
        )
        assert invalid_page.status_code == 409
        assert invalid_page.json()["error"]["code"] == "LEASE_EXPIRED"
        missing_pages = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/results",
            headers=headers,
            json={
                "worker_id": "worker-a",
                "lease_token": claimed["lease_token"],
                "submission_key": "empty-scope",
                "source_sha256": claimed["source_sha256"],
                "source_version_id": claimed["source_version_id"],
                "pages": [
                    {
                        "page_number": 2,
                        "text": "wrong page",
                        "provider_name": "fake",
                        "blocks": [],
                    }
                ],
            },
        )
        assert missing_pages.status_code == 409
        assert missing_pages.json()["error"]["code"] == "EXTRACTION_PAGE_SCOPE_MISMATCH"
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            assert item and item.extraction_run_id is None
            assert db.query(DocumentPage).count() == 0
    finally:
        clear_overrides()


def test_external_ocr_partial_result_is_preserved_and_continues_conservatively(monkeypatch, tmp_path) -> None:
    """允许部分提取时必须保留失败页范围、继续归档，并最终投影为 PARTIAL 而非成功。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "true")
    monkeypatch.setenv("INTEGRATION_ALLOW_PARTIAL_EXTRACTION", "true")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        _, item_id, task_id = _prepare_image_task(client, SessionLocal, headers)
        claim = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/claim",
            headers=headers,
            json={"worker_id": "partial-worker"},
        ).json()
        response = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/results",
            headers=headers,
            json={
                "worker_id": "partial-worker",
                "lease_token": claim["lease_token"],
                "submission_key": "partial-submit",
                "source_sha256": claim["source_sha256"],
                "source_version_id": claim["source_version_id"],
                "pages": [{
                    "page_number": 1,
                    "text": "",
                    "provider_name": "fake-ocr",
                    "blocks": [],
                    "error": {"code": "UNREADABLE", "message": "图像无法识别"},
                }],
            },
        )

        assert response.status_code == 202
        assert response.json()["task"]["status"] == "PARTIAL"
        assert response.json()["next_stage"] == "ARCHIVE"
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            task = db.get(ExternalExtractionTask, task_id)
            assert item.result_json["external_extraction_status"] == "PARTIAL"
            assert item.result_json["external_extraction_failed_pages"] == [1]
            assert task.error_json["code"] == "EXTRACTION_PARTIAL"
            page = db.query(DocumentPage).filter(DocumentPage.extraction_run_id == item.extraction_run_id).one()
            assert page.text_content == ""
            assert page.metadata_json["ocr_error"]["code"] == "UNREADABLE"
    finally:
        clear_overrides()


def test_external_page_retention_deletes_only_derived_resources_and_keeps_audit(monkeypatch, tmp_path) -> None:
    """保留期清理只能删除外部提取派生页图，任务、页结果和上传原件必须保留。"""

    storage = tmp_path / "storage"
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("EXTERNAL_PAGE_RETENTION_HOURS", "1")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        _, _, task_id = _prepare_image_task(client, SessionLocal, headers)
        derived = storage / "external-extraction" / "item" / "page-1.png"
        derived.parent.mkdir(parents=True)
        derived.write_bytes(b"derived-page")
        with SessionLocal() as db:
            task = db.get(ExternalExtractionTask, task_id)
            task.status = "COMPLETED"
            task.updated_at = utcnow() - timedelta(hours=2)
            task.page_manifest_json = [{
                "page_number": 1,
                "content_type": "image/png",
                "storage_path": "external-extraction/item/page-1.png",
            }]
            db.commit()
        with SessionLocal() as db:
            result = ExternalExtractionService(db).cleanup_expired_page_resources()
            db.commit()
            task = db.get(ExternalExtractionTask, task_id)
            page = db.query(ExternalExtractionPage).filter_by(task_id=task_id).one()
            assert result["files_cleaned"] == 1
            assert not derived.exists()
            assert task.page_manifest_json[0]["resource_cleaned"] is True
            assert page.result_json["resource_cleaned"] is True
    finally:
        clear_overrides()


def test_partial_external_ocr_can_retry_only_failed_pages_before_publish(monkeypatch, tmp_path) -> None:
    """部分结果发布前再次领取时只返回失败页，并用新运行替换失败页正文。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("INTEGRATION_ALLOW_PARTIAL_EXTRACTION", "true")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        headers = _auth(client)
        _, item_id, task_id = _prepare_image_task(client, SessionLocal, headers)
        first = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/claim",
            headers=headers,
            json={"worker_id": "retry-worker"},
        ).json()
        common = {
            "worker_id": "retry-worker",
            "source_sha256": first["source_sha256"],
            "source_version_id": first["source_version_id"],
        }
        partial = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/results",
            headers=headers,
            json={
                **common,
                "lease_token": first["lease_token"],
                "submission_key": "partial-first",
                "pages": [{
                    "page_number": 1,
                    "text": "",
                    "provider_name": "fake",
                    "blocks": [],
                    "error": {"code": "TEMPORARY", "message": "暂时失败"},
                }],
            },
        )
        assert partial.json()["task"]["status"] == "PARTIAL"
        second = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/claim",
            headers=headers,
            json={"worker_id": "retry-worker"},
        ).json()
        assert [page["page_number"] for page in second["pages"]] == [1]
        completed = client.post(
            f"/api/integrations/v1/extraction-tasks/{task_id}/results",
            headers=headers,
            json={
                **common,
                "lease_token": second["lease_token"],
                "submission_key": "partial-retry",
                "pages": [{
                    "page_number": 1,
                    "text": "重试后识别成功",
                    "provider_name": "fake",
                    "blocks": [],
                    "error": None,
                }],
            },
        )
        assert completed.status_code == 202
        assert completed.json()["task"]["status"] == "COMPLETED"
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            page = db.query(DocumentPage).filter_by(extraction_run_id=item.extraction_run_id).one()
            assert page.text_content == "重试后识别成功"
            assert item.result_json["external_extraction_status"] == "COMPLETED"
    finally:
        clear_overrides()
