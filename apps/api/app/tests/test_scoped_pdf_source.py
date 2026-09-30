"""真实导入任务上的区域裁剪、B–I 覆盖和全文隔离回归。"""

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from openpyxl import Workbook, load_workbook

from app.core.config import get_settings
from app.db.models import (DocumentExtractionRun, DocumentPage, ExternalExtractionTask,
                           FilesystemJob, IngestItem, UploadArchiveRecord, UploadDuplicateReview)
from app.modules.file_lifecycle.service import FileLifecycleJobProcessor
from app.tests.helpers import clear_overrides, client_with_database
from app.tests.test_structured_datasets import seed_excel_target, seed_workbuddy_document
from app.modules.structured_datasets.task_schemas import TaskPayload
from app.modules.structured_datasets.writeback import ExcelWritebackService, writeback_digest


def test_scoped_append_preserves_merged_footer_under_server_diff(monkeypatch, tmp_path):
    labels = ["序号", *[f"字段{column}" for column in range(2, 28)]]
    book = Workbook()
    sheet = book.active
    sheet.title = "汇总"
    sheet.append(labels)
    sheet["A5"] = 1
    sheet["A7"] = "填表说明"
    sheet.merge_cells("A7:AA7")
    baseline = tmp_path / "baseline.xlsx"
    book.save(baseline)
    book.close()
    layout = {"header_range": "A1:AA1", "data_range": "A5:AA6"}
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "mcp"))
    from file_agent_mcp.xlsx_writeback import patch_workbook
    values = [f"值{column}" for column in range(2, 10)]
    current = baseline
    for number in (1, 2, 3):
        output = tmp_path / f"result-{number}.xlsx"
        patch_workbook(baseline=current, output=output, sheet_name="汇总", labels=labels,
            rows=[[None, *values, *([None] * 18)]], layout=layout, scoped_a_i=True)
        dataset = SimpleNamespace(target_layout_json=layout,
            schema_json=[{"label": label} for label in labels], target_sheet_name="汇总")
        inserted = ExcelWritebackService.__new__(ExcelWritebackService)._verify_workbook(
            base_path=current, candidate_path=output, dataset=dataset,
            expected_rows=[[None, *values, *([None] * 18)]], scoped_a_i=True)
        assert inserted == (1 if number == 3 else 0)
        current = output
    saved = load_workbook(current)
    assert saved["汇总"]["A8"].value == "填表说明"
    assert "A8:AA8" in {str(item) for item in saved["汇总"].merged_cells.ranges}
    saved.close()


def test_legacy_scoped_archive_retry_wait_is_recovered_once(monkeypatch, tmp_path):
    """旧版式校验 422 留下的已完成重试任务，应恢复查重并签发真正的 OCR 任务。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("INTEGRATION_INGEST_ENABLED", "true")
    monkeypatch.setenv("WORKBUDDY_DOCUMENT_EXTRACTION_ENABLED", "true")
    monkeypatch.setenv("WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED", "true")
    get_settings.cache_clear()
    client, sessions = client_with_database()
    try:
        client.post("/api/auth/register", json={"username": "dataset-user-a", "password": "password123"})
        token = client.post("/api/auth/login", json={"username": "dataset-user-a", "password": "password123"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        pdf = fitz.open()
        for _ in range(22):
            pdf.new_page(width=595, height=842)
        content = pdf.tobytes()
        pdf.close()
        batch = client.post("/api/integrations/v1/ingest-batches", headers=headers, json={
            "client_id": "workbuddy-attachment", "request_id": "scoped-recover",
            "idempotency_key": "scoped-recover", "source_root_ref": "workbuddy-attachments",
            "relative_directory": "submitted-attachments", "recursive": False,
            "user_request": "只填 A–I", "extraction_profile": "SCOPED_EXCEL_SOURCE"})
        assert batch.status_code == 201, batch.text
        batch_id = batch.json()["id"]
        item = client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/items", headers=headers,
            json={"items": [{"client_item_id": "recover-pdf", "source_root_ref": "workbuddy-attachments",
                "source_relative_path": "submitted-attachments/recover/评审.pdf", "original_filename": "评审.pdf",
                "size_bytes": len(content), "mtime_ns": 1,
                "expected_sha256": hashlib.sha256(content).hexdigest()}]}).json()["items"][0]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=headers)
        uploaded = client.put(f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
            headers=headers, files={"file": ("评审.pdf", content, "application/pdf")})
        assert uploaded.status_code == 202, uploaded.text
        with sessions() as db:
            ingest_item = db.get(IngestItem, item["id"])
            archive = db.get(UploadArchiveRecord, ingest_item.archive_record_id)
            review = db.query(UploadDuplicateReview).filter_by(
                upload_document_version_id=archive.upload_document_version_id).one()
            old_job = db.get(FilesystemJob, uploaded.json()["filesystem_job_id"])
            old_job.status = "COMPLETED"
            old_job.result_json = {"status": "RETRY_WAIT", "idempotent": True}
            archive.status = "RETRY_WAIT"
            review.status = "CHECKING"
            reconcile = FilesystemJob(job_type="RECONCILE_UPLOAD_ARCHIVES", queue_name="RECONCILE",
                status="RUNNING", payload_json={}, result_json={})
            db.add(reconcile)
            db.flush()
            assert FileLifecycleJobProcessor(db).process(reconcile) is True
            assert archive.status == "DUPLICATE_CHECK_PENDING"
            recovered_job = db.get(FilesystemJob, archive.filesystem_job_id)
            assert recovered_job.id != old_job.id and recovered_job.status == "PENDING"
            assert FileLifecycleJobProcessor(db).process(recovered_job) is True
            task = db.query(ExternalExtractionTask).filter_by(ingest_item_id=item["id"]).one()
            assert task.status == "PENDING"
            assert task.source_manifest_json["layout_variant"] == "a4_22_page"
            assert archive.status == "WAITING_EXTERNAL_EXTRACTION"
    finally:
        clear_overrides()


@pytest.mark.parametrize("page_count,width,height,variant", [
    (25, 576, 823, "professor_25_page"),
    (22, 595, 842, "a4_22_page"),
])
def test_scoped_pdf_only_exposes_two_crops_and_requires_eight_columns(
        monkeypatch, tmp_path, page_count, width, height, variant):
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("INTEGRATION_INGEST_ENABLED", "true")
    monkeypatch.setenv("WORKBUDDY_DOCUMENT_EXTRACTION_ENABLED", "true")
    monkeypatch.setenv("WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED", "true")
    get_settings.cache_clear()
    client, sessions = client_with_database()
    try:
        client.post("/api/auth/register", json={"username": "dataset-user-a", "password": "password123"})
        token = client.post("/api/auth/login", json={"username": "dataset-user-a", "password": "password123"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        pdf = fitz.open()
        for page_number in range(1, page_count + 1):
            page = pdf.new_page(width=width, height=height)
            if page_number in {1, 3}:
                page.insert_text((120, 300), f"SCOPED PAGE {page_number}")
        content = pdf.tobytes()
        pdf.close()
        sha = hashlib.sha256(content).hexdigest()
        batch = client.post("/api/integrations/v1/ingest-batches", headers=headers, json={
            "client_id": "workbuddy-attachment", "request_id": "scoped-source", "idempotency_key": "scoped-source",
            "source_root_ref": "workbuddy-attachments", "relative_directory": "submitted-attachments",
            "recursive": False, "user_request": "只填 A–I", "extraction_profile": "SCOPED_EXCEL_SOURCE",
        })
        assert batch.status_code == 201, batch.text
        batch_id = batch.json()["id"]
        item = client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/items", headers=headers, json={"items": [{
            "client_item_id": "pdf-1", "source_root_ref": "workbuddy-attachments",
            "source_relative_path": "submitted-attachments/pdf-1/评审.pdf", "original_filename": "评审.pdf",
            "size_bytes": len(content), "mtime_ns": 1, "expected_sha256": sha,
        }]}).json()["items"][0]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=headers)
        uploaded = client.put(f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
            headers=headers, files={"file": ("评审.pdf", content, "application/pdf")})
        assert uploaded.status_code == 202, uploaded.text
        with sessions() as db:
            job = db.get(FilesystemJob, uploaded.json()["filesystem_job_id"])
            assert FileLifecycleJobProcessor(db).process(job) is True
            db.commit()
            task = db.query(ExternalExtractionTask).filter_by(ingest_item_id=item["id"]).one()
            task_id = task.id
            assert task.provider_contract_version == "workbuddy-scoped-pdf-v1"
            assert task.source_manifest_json["layout_variant"] == variant
        claimed = client.post(f"/api/integrations/v1/scoped-pdf-sources/{task_id}/claim", headers=headers,
            json={"worker_id": "ocr-worker"})
        assert claimed.status_code == 200, claimed.text
        scope = claimed.json()
        assert [r["page_number"] for r in scope["regions"]] == [1, 3]
        renewed = client.post(f"/api/integrations/v1/scoped-pdf-sources/{task_id}/renew", headers=headers,
            json={"worker_id": "ocr-worker", "lease_token": scope["lease_token"]})
        assert renewed.status_code == 200, renewed.text
        region_results = []
        for region in scope["regions"]:
            image = client.get(f"/api/integrations/v1/scoped-pdf-sources/{task_id}/regions/{region['region_id']}",
                headers={**headers, "X-Extraction-Lease-Token": scope["lease_token"]},
                params={"worker_id": "ocr-worker"})
            assert image.status_code == 200, image.text
            assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
            region_results.append({"region_id": region["region_id"],
                "image_sha256": hashlib.sha256(image.content).hexdigest(),
                "text": "单位 计算机科学与工程学院 姓名 石程" if region["page_number"] == 1 else
                        "出生 1986.11 工作 2013.06 职称 副教授 2019年11月 学历 博士 2016.6"})
        denied = client.get(f"/api/integrations/v1/scoped-pdf-sources/{task_id}/regions/page_4",
            headers={**headers, "X-Extraction-Lease-Token": scope["lease_token"]},
            params={"worker_id": "ocr-worker"})
        assert denied.status_code == 404
        fields = [
            ("B", "计算机科学与工程学院", "单位 计算机科学与工程学院", "cover_identity"),
            ("C", "石程", "姓名 石程", "cover_identity"),
            ("D", "1986.11", "出生 1986.11", "basic_profile"),
            ("E", "2013.06", "工作 2013.06", "basic_profile"),
            ("F", "副教授", "职称 副教授", "basic_profile"),
            ("G", "2019年11月", "2019年11月", "basic_profile"),
            ("H", "博士", "学历 博士", "basic_profile"),
            ("I", "2016.6", "2016.6", "basic_profile"),
        ]
        payload = {"worker_id": "ocr-worker", "lease_token": scope["lease_token"],
            "submission_key": "ocr-result-1", "source_version_id": scope["source_version_id"],
            "source_sha256": scope["source_sha256"], "scope_digest": scope["scope_digest"],
            "provider_name": "workbuddy-ocr-provider", "regions": region_results,
            "fields": [{"column": column, "value": value, "quote": quote, "region_id": region}
                       for column, value, quote, region in fields]}
        missing = client.post(f"/api/integrations/v1/scoped-pdf-sources/{task_id}/results", headers=headers,
            json={**payload, "fields": payload["fields"][:-1]})
        assert missing.status_code == 422
        labeled_value = json.loads(json.dumps(payload, ensure_ascii=False))
        labeled_value["fields"][0]["value"] = "单位 计算机科学与工程学院"
        rejected = client.post(f"/api/integrations/v1/scoped-pdf-sources/{task_id}/results", headers=headers,
            json=labeled_value)
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "SCOPED_B_VALUE_INVALID"
        accepted = client.post(f"/api/integrations/v1/scoped-pdf-sources/{task_id}/results",
            headers=headers, json=payload)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "SCOPE_COMPLETE"
        run_id = accepted.json()["extraction_run_id"]
        with sessions() as db:
            run = db.get(DocumentExtractionRun, run_id)
            assert run.status == "SCOPE_COMPLETE" and run.extractor == "workbuddy-scoped-pdf"
            assert {p.page_number for p in db.query(DocumentPage).filter_by(extraction_run_id=run_id)} == {1, 3}
        source = client.get(f"/api/structured-datasets/sources/{scope['source_document_id']}", headers=headers,
            params={"document_version_id": scope["source_version_id"], "extraction_run_id": run_id,
                    "content_origin": "WORKBUDDY_SCOPED"})
        assert source.status_code == 200, source.text
        assert set(source.json()["field_refs"]) == set("BCDEFGHI")
        assert len(source.json()["source_units"]) == 2
        # 老 XLS 转换后 A 标题可能已乱码；物理 A5 的预置序号仍必须保留。
        labels = ["���", "学院", "姓名", "出生年月", "工作时间", "职称", "职称取得时间", "学历", "学历取得时间"] + [f"其他字段{i}" for i in range(10, 28)]
        target_id = seed_excel_target(sessions, filename="教授汇总.xlsx",
            sheets={"汇总": [labels, [1] + [None] * 26]})
        layout = {"header_range": "A1:AA1", "data_range": "A2:AA3"}
        created = client.post("/api/structured-datasets/from-excel", headers=headers,
            json={"document_id": target_id, "sheet_name": "汇总", "layout": layout,
                  "idempotency_key": "scoped-target"})
        assert created.status_code == 200, created.text
        checked = client.post("/api/excel-writeback/checkout", headers=headers,
            json={"filename": "教授汇总.xlsx", "request_key": "scoped-writeback"})
        assert checked.status_code == 200, checked.text
        target = checked.json()
        refs = source.json()["field_refs"]
        fields_payload = [{"raw_text": None, "blank_reason": "TARGET_DERIVED"}]
        values = [value for _, value, _, _ in fields]
        for column, value in zip("BCDEFGHI", values):
            ref = refs[column]
            fields_payload.append({"raw_text": value, "confidence": 0.99,
                "evidence_refs": [{"source_unit_id": ref["source_unit_id"], "quote": ref["quote"],
                                   "start": ref["start"], "end": ref["end"]}]})
        fields_payload.extend({"raw_text": None, "blank_reason": "OUT_OF_SCOPE"} for _ in range(18))
        candidate = TaskPayload.model_validate({"manifest": {
            "sources": [{"document_id": scope["source_document_id"],
                         "document_version_id": scope["source_version_id"],
                         "extraction_run_id": run_id, "source_sha256": scope["source_sha256"]}],
            "target": {"dataset_id": target["dataset_id"], "document_version_id": target["document_version_id"],
                       "filename": target["filename"], "sheet_name": target["sheet_name"],
                       "layout": target["layout"], "labels": target["labels"]},
            "write_scope": "A:I", "required_pdf_columns": "B:I"},
            "records": [{"source_index": 0, "fields": fields_payload}]}).model_dump(mode="json")
        invalid = json.loads(json.dumps(candidate))
        invalid["records"][0]["fields"][9] = {"raw_text": "不得填写"}
        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            TaskPayload.model_validate(invalid)
        invalid = json.loads(json.dumps(candidate))
        invalid["records"][0]["fields"][1]["evidence_refs"] = []
        with pytest.raises(ValueError, match="B–I"):
            TaskPayload.model_validate(invalid)
        baseline = tmp_path / "baseline.xlsx"
        baseline.write_bytes(client.get(target["download_url"], headers=headers).content)
        result_path = tmp_path / "result.xlsx"
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "mcp"))
        from file_agent_mcp.xlsx_writeback import patch_workbook
        patch_workbook(baseline=baseline, output=result_path, sheet_name="汇总", labels=target["labels"],
            rows=[[None, *values, *([None] * 18)]], layout=target["layout"], scoped_a_i=True)
        result_sha = hashlib.sha256(result_path.read_bytes()).hexdigest()
        result_digest = writeback_digest(candidate, result_sha)
        preview = client.post(f"/api/excel-writeback/tasks/{target['task_id']}/preview", headers=headers,
            json={"result_digest": result_digest, "file_sha256": result_sha})
        assert preview.status_code == 200, preview.text
        published = client.post(f"/api/excel-writeback/tasks/{target['task_id']}/publish", headers=headers,
            data={"result_digest": result_digest, "file_sha256": result_sha,
                  "payload_json": json.dumps(candidate, ensure_ascii=False)},
            files={"file": (target["filename"], result_path.read_bytes(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert published.status_code == 200, published.text
        assert published.json()["excel_published"] is True
        book = load_workbook(io.BytesIO(client.get(published.json()["download_url"], headers=headers).content))
        assert book["汇总"]["A2"].value == 1
        assert [book["汇总"].cell(2, column).value for column in range(2, 10)] == values
        assert book["汇总"]["B2"].value == "计算机科学与工程学院"
        assert all(book["汇总"].cell(2, column).value is None for column in range(10, 28))
        book.close()
        # 后续只上传 PDF 时，checkout 按完整文件名冻结当前版本；新增行自动生成 A 序号。
        # 第三条记录越过原 A2:AA3 数据区，发布后须同步扩展冻结布局。
        for index in (2, 3):
            next_pdf = fitz.open()
            for page_number in range(1, 26):
                page = next_pdf.new_page(width=576, height=823)
                if page_number in {1, 3}:
                    page.insert_text((120, 300), f"SCOPED PAGE {page_number} SOURCE {index}")
            next_content = next_pdf.tobytes()
            next_pdf.close()
            next_sha = hashlib.sha256(next_content).hexdigest()
            next_batch = client.post("/api/integrations/v1/ingest-batches", headers=headers, json={
                "client_id": "workbuddy-attachment", "request_id": f"scoped-append-source-{index}",
                "idempotency_key": f"scoped-append-source-{index}",
                "source_root_ref": "workbuddy-attachments", "relative_directory": "submitted-attachments",
                "recursive": False, "user_request": "按完整文件名续写教授汇总.xlsx",
                "extraction_profile": "SCOPED_EXCEL_SOURCE"})
            assert next_batch.status_code == 201, next_batch.text
            next_batch_id = next_batch.json()["id"]
            next_name = f"评审-{index}.pdf"
            next_item = client.post(f"/api/integrations/v1/ingest-batches/{next_batch_id}/items",
                headers=headers, json={"items": [{"client_item_id": f"pdf-{index}",
                    "source_root_ref": "workbuddy-attachments",
                    "source_relative_path": f"submitted-attachments/pdf-{index}/{next_name}",
                    "original_filename": next_name, "size_bytes": len(next_content),
                    "mtime_ns": index, "expected_sha256": next_sha}]}).json()["items"][0]
            client.post(f"/api/integrations/v1/ingest-batches/{next_batch_id}/seal", headers=headers)
            next_upload = client.put(
                f"/api/integrations/v1/ingest-batches/{next_batch_id}/items/{next_item['id']}/content",
                headers=headers, files={"file": (next_name, next_content, "application/pdf")})
            assert next_upload.status_code == 202, next_upload.text
            with sessions() as db:
                next_job = db.get(FilesystemJob, next_upload.json()["filesystem_job_id"])
                assert FileLifecycleJobProcessor(db).process(next_job) is True
                db.commit()
                next_task_id = db.query(ExternalExtractionTask).filter_by(ingest_item_id=next_item["id"]).one().id
            next_claim = client.post(f"/api/integrations/v1/scoped-pdf-sources/{next_task_id}/claim",
                headers=headers, json={"worker_id": "ocr-worker"})
            assert next_claim.status_code == 200, next_claim.text
            next_scope = next_claim.json()
            next_regions = []
            for region in next_scope["regions"]:
                next_image = client.get(
                    f"/api/integrations/v1/scoped-pdf-sources/{next_task_id}/regions/{region['region_id']}",
                    headers={**headers, "X-Extraction-Lease-Token": next_scope["lease_token"]},
                    params={"worker_id": "ocr-worker"})
                assert next_image.status_code == 200, next_image.text
                next_regions.append({"region_id": region["region_id"],
                    "image_sha256": hashlib.sha256(next_image.content).hexdigest(),
                    "text": region_results[len(next_regions)]["text"]})
            next_result = client.post(f"/api/integrations/v1/scoped-pdf-sources/{next_task_id}/results",
                headers=headers, json={"worker_id": "ocr-worker", "lease_token": next_scope["lease_token"],
                    "submission_key": f"ocr-result-{index}",
                    "source_version_id": next_scope["source_version_id"],
                    "source_sha256": next_scope["source_sha256"],
                    "scope_digest": next_scope["scope_digest"], "provider_name": "workbuddy-ocr-provider",
                    "regions": next_regions, "fields": payload["fields"]})
            assert next_result.status_code == 200, next_result.text
            next_refs = client.get(
                f"/api/structured-datasets/sources/{next_scope['source_document_id']}",
                headers=headers, params={"document_version_id": next_scope["source_version_id"],
                    "extraction_run_id": next_result.json()["extraction_run_id"],
                    "content_origin": "WORKBUDDY_SCOPED"}).json()["field_refs"]
            append_checkout = client.post("/api/excel-writeback/checkout", headers=headers,
                json={"filename": "教授汇总.xlsx", "request_key": f"scoped-append-{index}"})
            assert append_checkout.status_code == 200, append_checkout.text
            append_target = append_checkout.json()
            append_candidate = json.loads(json.dumps(candidate, ensure_ascii=False))
            append_candidate["manifest"]["sources"] = [{
                "document_id": next_scope["source_document_id"],
                "document_version_id": next_scope["source_version_id"],
                "extraction_run_id": next_result.json()["extraction_run_id"],
                "source_sha256": next_scope["source_sha256"]}]
            append_candidate["manifest"]["target"]["document_version_id"] = append_target["document_version_id"]
            append_candidate["manifest"]["target"]["layout"] = append_target["layout"]
            for column, field in zip("BCDEFGHI", append_candidate["records"][0]["fields"][1:9]):
                ref = next_refs[column]
                field["evidence_refs"] = [{"source_unit_id": ref["source_unit_id"],
                    "quote": ref["quote"], "start": ref["start"], "end": ref["end"]}]
            append_candidate = TaskPayload.model_validate(append_candidate).model_dump(mode="json")
            append_baseline = tmp_path / f"append-baseline-{index}.xlsx"
            append_baseline.write_bytes(client.get(append_target["download_url"], headers=headers).content)
            append_file = tmp_path / f"append-result-{index}.xlsx"
            patch_workbook(baseline=append_baseline, output=append_file, sheet_name="汇总",
                labels=append_target["labels"], rows=[[None, *values, *([None] * 18)]],
                layout=append_target["layout"], scoped_a_i=True)
            append_sha = hashlib.sha256(append_file.read_bytes()).hexdigest()
            append_digest = writeback_digest(append_candidate, append_sha)
            append_preview = client.post(f"/api/excel-writeback/tasks/{append_target['task_id']}/preview",
                headers=headers, json={"result_digest": append_digest, "file_sha256": append_sha})
            assert append_preview.status_code == 200, append_preview.text
            append_publish = client.post(f"/api/excel-writeback/tasks/{append_target['task_id']}/publish",
                headers=headers, data={"result_digest": append_digest, "file_sha256": append_sha,
                                       "payload_json": json.dumps(append_candidate, ensure_ascii=False)},
                files={"file": (append_target["filename"], append_file.read_bytes(),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
            assert append_publish.status_code == 200, append_publish.text
            assert append_publish.json()["excel_published"] is True
            saved = load_workbook(io.BytesIO(client.get(append_publish.json()["download_url"],
                                                      headers=headers).content))
            assert saved["汇总"].cell(index + 1, 1).value == index
            assert saved["汇总"].cell(index + 1, 2).value == values[0]
            saved.close()
        assert client.get("/api/structured-datasets/targets/by-filename", headers=headers,
            params={"filename": "教授汇总.xlsx"}).json()["layout"]["data_range"] == "A2:AA4"
        # 已完成 OCR 只保存完整引文；写回阶段仍须拒绝把字段标签写进 B 格。
        second = client.post("/api/excel-writeback/checkout", headers=headers,
            json={"filename": "教授汇总.xlsx", "request_key": "scoped-b-label-rejected"})
        assert second.status_code == 200, second.text
        second_target = second.json()
        bad_candidate = json.loads(json.dumps(candidate, ensure_ascii=False))
        bad_candidate["manifest"]["target"]["document_version_id"] = second_target["document_version_id"]
        bad_candidate["manifest"]["target"]["layout"] = second_target["layout"]
        bad_candidate["records"][0]["fields"][1]["raw_text"] = "单位 计算机科学与工程学院"
        bad_candidate = TaskPayload.model_validate(bad_candidate).model_dump(mode="json")
        frozen_file = tmp_path / "second-baseline.xlsx"
        frozen_file.write_bytes(client.get(second_target["download_url"], headers=headers).content)
        frozen_sha = hashlib.sha256(frozen_file.read_bytes()).hexdigest()
        bad_digest = writeback_digest(bad_candidate, frozen_sha)
        bad_preview = client.post(f"/api/excel-writeback/tasks/{second_target['task_id']}/preview",
            headers=headers, json={"result_digest": bad_digest, "file_sha256": frozen_sha})
        assert bad_preview.status_code == 200, bad_preview.text
        bad_publish = client.post(f"/api/excel-writeback/tasks/{second_target['task_id']}/publish",
            headers=headers, data={"result_digest": bad_digest, "file_sha256": frozen_sha,
                                   "payload_json": json.dumps(bad_candidate, ensure_ascii=False)},
            files={"file": (second_target["filename"], frozen_file.read_bytes(),
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert bad_publish.status_code == 422, bad_publish.text
        assert bad_publish.json()["error"]["code"] == "SCOPED_B_VALUE_INVALID"
        # 同一轮两份 Word 各追加一行，只发布一次目标新版本。
        word_sources = []
        word_values = []
        for index, name in enumerate(("张一", "李二"), start=1):
            values_for_word = ["计算机科学与工程学院", name, "1986.11", "2013.06",
                               "副教授", "2019年11月", "博士", "2016.6"]
            word_text = (f"单位 {values_for_word[0]} 姓名 {name} 出生 {values_for_word[2]} "
                         f"工作 {values_for_word[3]} 职称 {values_for_word[4]} "
                         f"{values_for_word[5]} 学历 {values_for_word[6]} {values_for_word[7]}")
            document_id = seed_workbuddy_document(sessions, filename=f"评审-{name}.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                units=[{"kind": "TEXT_SECTION", "text": word_text}])
            response = client.get(f"/api/structured-datasets/sources/{document_id}", headers=headers,
                                  params={"content_origin": "WORKBUDDY"})
            assert response.status_code == 200, response.text
            source_data = response.json()
            assert source_data["source_kind"] == "WORKBUDDY_DOCUMENT"
            word_sources.append(source_data)
            word_values.append(values_for_word)
        multi_checkout = client.post("/api/excel-writeback/checkout", headers=headers,
            json={"filename": "教授汇总.xlsx", "request_key": "scoped-two-word-append"})
        assert multi_checkout.status_code == 200, multi_checkout.text
        multi_target = multi_checkout.json()
        multi_candidate = json.loads(json.dumps(candidate, ensure_ascii=False))
        multi_candidate["manifest"]["target"]["document_version_id"] = multi_target["document_version_id"]
        multi_candidate["manifest"]["target"]["layout"] = multi_target["layout"]
        multi_candidate["manifest"]["sources"] = [{key: source_data[key] for key in
            ("document_id", "document_version_id", "extraction_run_id", "source_sha256")}
            for source_data in word_sources]
        multi_candidate["records"] = []
        for index, (source_data, values_for_word) in enumerate(zip(word_sources, word_values)):
            unit = source_data["source_units"][0]
            fields_for_word = [{"blank_reason": "TARGET_DERIVED"}]
            quotes = [f"单位 {values_for_word[0]}", f"姓名 {values_for_word[1]}",
                      f"出生 {values_for_word[2]}", f"工作 {values_for_word[3]}",
                      f"职称 {values_for_word[4]}", values_for_word[5],
                      f"学历 {values_for_word[6]}", values_for_word[7]]
            fields_for_word.extend({"raw_text": value, "confidence": 0.99,
                "evidence_refs": [{"source_unit_id": unit["source_unit_id"],
                                   "quote": quote, "start": unit["text"].index(quote),
                                   "end": unit["text"].index(quote) + len(quote)}]}
                for value, quote in zip(values_for_word, quotes))
            fields_for_word.extend({"blank_reason": "OUT_OF_SCOPE"} for _ in range(18))
            multi_candidate["records"].append({"source_index": index, "fields": fields_for_word})
        multi_candidate = TaskPayload.model_validate(multi_candidate).model_dump(mode="json")
        multi_baseline = tmp_path / "multi-baseline.xlsx"
        multi_baseline.write_bytes(client.get(multi_target["download_url"], headers=headers).content)
        multi_result = tmp_path / "multi-result.xlsx"
        patch_workbook(baseline=multi_baseline, output=multi_result, sheet_name="汇总",
            labels=multi_target["labels"],
            rows=[[None, *values_for_word, *([None] * 18)] for values_for_word in word_values],
            layout=multi_target["layout"], scoped_a_i=True)
        multi_sha = hashlib.sha256(multi_result.read_bytes()).hexdigest()
        multi_digest = writeback_digest(multi_candidate, multi_sha)
        multi_preview = client.post(f"/api/excel-writeback/tasks/{multi_target['task_id']}/preview",
            headers=headers, json={"result_digest": multi_digest, "file_sha256": multi_sha})
        assert multi_preview.status_code == 200, multi_preview.text
        multi_publish = client.post(f"/api/excel-writeback/tasks/{multi_target['task_id']}/publish",
            headers=headers, data={"result_digest": multi_digest, "file_sha256": multi_sha,
                                   "payload_json": json.dumps(multi_candidate, ensure_ascii=False)},
            files={"file": (multi_target["filename"], multi_result.read_bytes(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert multi_publish.status_code == 200, multi_publish.text
        assert multi_publish.json()["excel_published"] is True
        multi_book = load_workbook(io.BytesIO(client.get(multi_publish.json()["download_url"], headers=headers).content))
        for row_number, values_for_word in enumerate(word_values, start=5):
            assert multi_book["汇总"].cell(row_number, 1).value == row_number - 1
            assert [multi_book["汇总"].cell(row_number, column).value for column in range(2, 10)] == values_for_word
            assert all(multi_book["汇总"].cell(row_number, column).value is None for column in range(10, 28))
        multi_book.close()
    finally:
        clear_overrides()
