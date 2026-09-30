"""任务绑定的 PDF 区域 OCR：只渲染两页的冻结矩形，永不宣称全文完成。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import (Document, DocumentExtractionRun, DocumentPage, DocumentVersion,
                           ExternalExtractionTask, IngestBatch, IngestItem, User, utcnow)
from app.modules.file_lifecycle.service import create_lifecycle_audit
from app.modules.ingestion.workflow import IngestionWorkflow

from .document_service import WorkBuddyDocumentExtractionService
from .scoped_schemas import CONTRACT, REQUIRED_COLUMNS, ScopedSubmit, labeled_b_column_value
from .service import ExternalExtractionService


REGIONS = (
    {"region_id": "cover_identity", "page_number": 1, "bbox": [0.30, 0.40, 0.75, 0.50]},
    {"region_id": "basic_profile", "page_number": 3, "bbox": [0.12, 0.12, 0.88, 0.56]},
)

# 已核对的两种评审表版式共用第 1、3 页 B–I 冻结区域。
LAYOUT_VARIANTS = (
    {"name": "professor_25_page", "page_count": 25, "width": 576, "height": 823},
    {"name": "a4_22_page", "page_count": 22, "width": 595, "height": 842},
)


class ScopedPdfExtractionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.legacy = ExternalExtractionService(db)
        self.storage = self.legacy.storage

    def prepare_for_ingest_item(self, *, item: IngestItem) -> ExternalExtractionTask | None:
        batch = self.db.get(IngestBatch, item.batch_id)
        if batch is None or (batch.policy_json or {}).get("extraction_profile") != "SCOPED_EXCEL_SOURCE":
            return None
        version = self.db.get(DocumentVersion, item.upload_document_version_id) if item.upload_document_version_id else None
        if version is None:
            return None
        if Path(version.filename).suffix.lower() != ".pdf":
            self.legacy._raise(422, "SCOPED_PDF_REQUIRED", "定向来源必须是 PDF。")
        manifest = WorkBuddyDocumentExtractionService(self.db)._source_manifest(version)
        import fitz
        with fitz.open(self.storage.upload_path(version.storage_path)) as pdf:
            variant = next((layout for layout in LAYOUT_VARIANTS
                if manifest.get("page_count") == layout["page_count"]
                and all(abs(pdf[index].rect.width - layout["width"]) <= 5
                        and abs(pdf[index].rect.height - layout["height"]) <= 5
                        for index in (0, 2))), None)
            if variant is None:
                self.legacy._raise(422, "SCOPE_LOCATION_UNRESOLVED",
                                   "PDF 页数或第 1、3 页尺寸不符合已核对的定向版式。")
        scope = {"contract": CONTRACT, "source_version_id": version.id, "source_sha256": version.sha256,
                 "layout_variant": variant["name"],
                 "page_count": manifest["page_count"], "required_pdf_columns": list(REQUIRED_COLUMNS),
                 "write_columns": list("ABCDEFGHI"), "regions": list(REGIONS)}
        scope_digest = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = self.db.query(ExternalExtractionTask).filter_by(
            ingest_item_id=item.id, source_version_id=version.id, provider_contract_version=CONTRACT).one_or_none()
        if existing is not None:
            return existing
        task = ExternalExtractionTask(
            ingest_item_id=item.id, source_version_id=version.id, source_sha256=version.sha256,
            phase="SCOPED_OCR", provider_contract_version=CONTRACT, page_manifest_json=list(REGIONS),
            source_manifest_json={**manifest, **scope, "scope_digest": scope_digest},
            result_summary_json={}, status="PENDING", error_json={})
        self.db.add(task)
        self.db.flush()
        version.extraction_policy_json = {"mode": "SCOPED_EXCEL_SOURCE", "contract_version": CONTRACT,
                                          "origin_task_id": task.id, "source_sha256": version.sha256}
        item.status = "WAITING_EXTERNAL_EXTRACTION"
        item.stage = "SCOPED_OCR"
        item.result_json = {**dict(item.result_json or {}), "scoped_pdf_task_id": task.id,
                            "content_extraction_contract": CONTRACT}
        item.workflow_revision += 1
        self.db.flush()
        return task

    def _owned(self, task_id: str, user: User, *, lock: bool = False):
        task, item, batch = self.legacy._owned_task(task_id=task_id, user_id=user.id, for_update=lock)
        if task.provider_contract_version != CONTRACT:
            self.legacy._raise(409, "EXTRACTION_CONTRACT_MISMATCH", "任务不属于定向 PDF 契约。")
        return task, item, batch

    def receipt(self, task: ExternalExtractionTask) -> dict:
        manifest = task.source_manifest_json or {}
        version = self.db.get(DocumentVersion, task.source_version_id)
        return {"task_id": task.id, "status": task.status, "source_version_id": task.source_version_id,
                "source_document_id": version.document_id if version else None,
                "source_sha256": task.source_sha256, "scope_digest": manifest.get("scope_digest"),
                "page_count": manifest.get("page_count"), "regions": manifest.get("regions"),
                "required_pdf_columns": list(REQUIRED_COLUMNS), "write_columns": list("ABCDEFGHI"),
                "extraction_run_id": task.extraction_run_id,
                "processed_pages": (task.result_summary_json or {}).get("processed_pages", []),
                "coverage": (task.result_summary_json or {}).get("coverage", {})}

    def get(self, *, task_id: str, user: User) -> dict:
        task, _, _ = self._owned(task_id, user)
        return self.receipt(task)

    def claim(self, *, task_id: str, worker_id: str, user: User) -> dict:
        task, item, _ = self._owned(task_id, user, lock=True)
        if task.status == "SCOPE_COMPLETE":
            self.legacy._raise(409, "EXTRACTION_ALREADY_COMPLETED", "定向 OCR 已完成。")
        if item.status in {"CANCELLED", "EXPIRED", "FAILED", "SKIPPED"}:
            self.legacy._raise(409, "EXTRACTION_TASK_INACTIVE", "来源任务已结束。")
        if task.status == "CLAIMED" and task.lease_expires_at and self.legacy._aware(task.lease_expires_at) > utcnow() and task.lease_owner != worker_id:
            self.legacy._raise(409, "EXTRACTION_ALREADY_CLAIMED", "任务已由其他 Worker 领取。")
        token = self.legacy._new_lease_token()
        task.lease_owner = worker_id
        task.lease_token_hash = self.legacy._token_hash(token)
        task.lease_expires_at = utcnow() + self.legacy._lease_duration()
        task.status = "CLAIMED"
        task.attempt_count += 1
        self.db.flush()
        return {**self.receipt(task), "lease_token": token, "lease_expires_at": task.lease_expires_at,
                "provider_contract_version": CONTRACT}

    def _region_bytes(self, task: ExternalExtractionTask, region_id: str) -> bytes:
        region = next((r for r in REGIONS if r["region_id"] == region_id), None)
        if region is None:
            self.legacy._raise(404, "SCOPE_REGION_UNKNOWN", "区域不在冻结白名单。")
        version = self.db.get(DocumentVersion, task.source_version_id)
        if version is None or version.sha256 != task.source_sha256:
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "来源版本已变化。")
        path = self.storage.upload_path(version.storage_path)
        if not path.is_file() or self.storage.sha256_file(path) != task.source_sha256:
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "来源文件已变化。")
        import fitz
        with fitz.open(path) as pdf:
            if pdf.needs_pass or pdf.page_count < region["page_number"]:
                self.legacy._raise(422, "SCOPE_LOCATION_UNRESOLVED", "冻结页无法读取。")
            page = pdf.load_page(region["page_number"] - 1)
            left, top, right, bottom = region["bbox"]
            crop = fitz.Rect(left * page.rect.width, top * page.rect.height,
                             right * page.rect.width, bottom * page.rect.height)
            return page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=crop, alpha=False).tobytes("png")

    def renew(self, *, task_id: str, worker_id: str, lease_token: str, user: User) -> dict:
        task, _, _ = self._owned(task_id, user, lock=True)
        self.legacy._validate_lease(task=task, worker_id=worker_id, lease_token=lease_token)
        task.lease_expires_at = utcnow() + self.legacy._lease_duration()
        self.db.flush()
        return {**self.receipt(task), "lease_expires_at": task.lease_expires_at}

    def region(self, *, task_id: str, region_id: str, worker_id: str, lease_token: str, user: User) -> bytes:
        task, _, _ = self._owned(task_id, user)
        self.legacy._validate_lease(task=task, worker_id=worker_id, lease_token=lease_token)
        return self._region_bytes(task, region_id)

    def submit(self, *, task_id: str, payload: ScopedSubmit, user: User) -> dict:
        task, item, batch = self._owned(task_id, user, lock=True)
        body = payload.model_dump(mode="json", exclude={"lease_token"})
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if task.last_submission_key == payload.submission_key:
            if task.last_submission_digest != digest:
                self.legacy._raise(409, "IDEMPOTENCY_CONFLICT", "提交键已绑定不同结果。")
            return {**self.receipt(task), "reused": True}
        self.legacy._validate_lease(task=task, worker_id=payload.worker_id, lease_token=payload.lease_token)
        manifest = task.source_manifest_json or {}
        if (str(payload.source_version_id) != task.source_version_id or payload.source_sha256 != task.source_sha256
                or payload.scope_digest != manifest.get("scope_digest")):
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "OCR 结果与冻结来源或范围不一致。")
        regions = {region.region_id: region for region in payload.regions}
        for region_id, result in regions.items():
            if hashlib.sha256(self._region_bytes(task, region_id)).hexdigest() != result.image_sha256:
                self.legacy._raise(409, "SCOPE_IMAGE_CHANGED", "OCR 区域图像哈希不一致。")
        field_refs = {}
        for field in payload.fields:
            text = regions[field.region_id].text
            if field.start is None:
                start = text.find(field.quote)
                if start < 0 or text.find(field.quote, start + 1) >= 0:
                    self.legacy._raise(422, "EVIDENCE_AMBIGUOUS", f"{field.column} 列引文不唯一。")
                end = start + len(field.quote)
            else:
                start, end = field.start, field.end
                if text[start:end] != field.quote:
                    self.legacy._raise(422, "EVIDENCE_SCOPE_MISMATCH", f"{field.column} 列引文不匹配。")
            if "".join(field.value.split()) not in "".join(field.quote.split()):
                self.legacy._raise(422, "EVIDENCE_SCOPE_MISMATCH", f"{field.column} 列值不在引文中。")
            if field.column == "B":
                name = labeled_b_column_value(field.quote)
                if name is not None and field.value != name:
                    self.legacy._raise(422, "SCOPED_B_VALUE_INVALID", "B 列只能填写“单位/学院”标签后的学院名称。")
            field_refs[field.column] = {"region_id": field.region_id, "start": start, "end": end}
        version = self.db.get(DocumentVersion, task.source_version_id)
        document = self.db.get(Document, version.document_id) if version else None
        if version is None or document is None:
            self.legacy._raise(409, "SOURCE_VERSION_MISSING", "来源版本不存在。")
        run = DocumentExtractionRun(document_id=document.id, document_version_id=version.id,
            status="SCOPE_COMPLETE", extractor="workbuddy-scoped-pdf", parser_name=payload.provider_name,
            parser_version=payload.provider_version or CONTRACT, parser_config_hash=payload.scope_digest)
        self.db.add(run)
        self.db.flush()
        for order, region in enumerate(REGIONS):
            self.db.add(DocumentPage(document_id=document.id, extraction_run_id=run.id,
                page_number=region["page_number"], text_content=regions[region["region_id"]].text,
                metadata_json={"content_origin": "WORKBUDDY_SCOPED", "scope_digest": payload.scope_digest,
                    "source_sha256": task.source_sha256, "region_id": region["region_id"],
                    "bbox": region["bbox"], "unit_order": order, "unit_kind": "PDF_PAGE",
                    "provider_name": payload.provider_name}))
        task.extraction_run_id = run.id
        task.status = "SCOPE_COMPLETE"
        task.last_submission_key = payload.submission_key
        task.last_submission_digest = digest
        task.lease_token_hash = None
        task.lease_expires_at = None
        task.result_summary_json = {"coverage": {"status": "SCOPE_COMPLETE", "required_pdf_columns": list(REQUIRED_COLUMNS),
            "resolved_columns": list(REQUIRED_COLUMNS)}, "processed_pages": [1, 3],
            "region_count": len(REGIONS), "provider_name": payload.provider_name, "field_refs": field_refs}
        item.extraction_run_id = run.id
        item.status = "SUCCEEDED"
        item.stage = "SCOPED_SOURCE_READY"
        item.result_json = {**dict(item.result_json or {}), "scoped_pdf_status": "SCOPE_COMPLETE",
                            "scoped_pdf_run_id": run.id}
        item.workflow_revision += 1
        self.db.flush()
        create_lifecycle_audit(db=self.db, user_id=user.id, workspace_id=batch.workspace_id,
            conversation_id=batch.conversation_id, tool_name="scoped_pdf_source_submit",
            message_content="WorkBuddy PDF 定向 OCR 完成", change_type="TEXT_EXTRACTED",
            target_type="EXTERNAL_EXTRACTION_TASK", target_id=task.id, target_document_id=document.id,
            after_value={"contract": CONTRACT, "scope_digest": payload.scope_digest,
                         "processed_pages": [1, 3], "region_count": 2, "resolved_columns": list(REQUIRED_COLUMNS),
                         "provider_name": payload.provider_name, "extraction_run_id": run.id},
            execution_status="COMPLETED", visible_in_conversation=False)
        IngestionWorkflow(self.db).synchronize_batch(batch)
        return {**self.receipt(task), "reused": False}
