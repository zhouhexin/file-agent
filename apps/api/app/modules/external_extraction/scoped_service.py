"""任务绑定的 PDF 区域 OCR：当前 v4 仅冻结第 25 页，旧 v3 保留倒序兼容。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import (Document, DocumentExtractionRun, DocumentPage, DocumentVersion,
                           ExternalExtractionTask, IngestBatch, IngestItem, User, utcnow)
from app.modules.file_lifecycle.service import create_lifecycle_audit
from app.modules.ingestion.workflow import IngestionWorkflow

from .document_service import WorkBuddyDocumentExtractionService
from .scoped_schemas import (CONTRACT, DISCOVERY_CONTRACT, FIXED_25_CONTRACT, LEGACY_CONTRACT, REQUIRED_COLUMNS,
                             ReviewProbeSubmit, ReviewRegionReject, ScopedSubmit,
                             excellent_years, has_review_heading, labeled_b_column_value)
from .service import ExternalExtractionService


REGIONS = (
    {"region_id": "cover_identity", "page_number": 1, "bbox": [0.30, 0.40, 0.75, 0.50]},
    {"region_id": "basic_profile", "page_number": 3, "bbox": [0.12, 0.12, 0.88, 0.56]},
)

# 已核对的两种评审表尺寸共用第 1、3 页 B–I 冻结区域；v4 的 L 只看第 25 页。
LAYOUT_VARIANTS = (
    {"name": "professor_576x823", "width": 576, "height": 823,
     "review_bbox": [0.12, 0.43, 0.97, 0.73]},
    {"name": "a4_595x842", "width": 595, "height": 842,
     "review_bbox": [0.08, 0.34, 0.97, 0.85]},
)


class ScopedPdfExtractionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.legacy = ExternalExtractionService(db)
        self.storage = self.legacy.storage

    @staticmethod
    def _review_bbox(page, heading_rect, fallback: list[float]) -> list[float]:
        """有文字层坐标时只冻结栏目附近；扫描件可回退到受控版式区域。"""
        if heading_rect is None:
            return fallback
        top = max(0.0, heading_rect.y0 / page.rect.height - 0.025)
        bottom = min(0.98, max(top + 0.30, heading_rect.y1 / page.rect.height + 0.28))
        return [0.08, top, 0.97, bottom]

    @staticmethod
    def _scope_digest(manifest: dict) -> str:
        """发现阶段和最终冻结范围分别形成不可混用的指纹。"""
        scope = {key: manifest.get(key) for key in (
            "contract", "source_version_id", "source_sha256", "layout_variant",
            "page_count", "required_pdf_columns", "write_columns", "regions")}
        discovery = manifest.get("review_discovery", {})
        scope["review_discovery"] = {key: discovery.get(key) for key in (
            "status", "selected_page", "checked_pages")}
        return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def prepare_for_ingest_item(self, *, item: IngestItem,
                                contract: str = FIXED_25_CONTRACT) -> ExternalExtractionTask | None:
        """新任务只冻结 B–I 与第 25 页 L；v3 旧任务仍可倒序定位。"""
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
                if manifest.get("page_count", 0) >= 3
                and all(abs(pdf[index].rect.width - layout["width"]) <= 5
                        and abs(pdf[index].rect.height - layout["height"]) <= 5
                        for index in ((0, 2) if contract == FIXED_25_CONTRACT else
                                      (0, 2, manifest["page_count"] - 1)))), None)
            if variant is None:
                self.legacy._raise(422, "SCOPE_LOCATION_UNRESOLVED",
                                   "PDF 第 1、3 页尺寸不符合已核对的定向版式。")
            regions = list(REGIONS)
            if contract == FIXED_25_CONTRACT:
                if manifest["page_count"] >= 25:
                    regions.append({"region_id": "annual_review", "page_number": 25,
                                    "bbox": variant["review_bbox"]})
                discovery = {"status": "FOUND" if manifest["page_count"] >= 25 else "NOT_FOUND",
                             "selected_page": 25 if manifest["page_count"] >= 25 else None,
                             "next_page": None, "checked_pages": [25] if manifest["page_count"] >= 25 else [],
                             "method": "FIXED_PAGE_25" if manifest["page_count"] >= 25 else "PAGE_25_ABSENT"}
            else:
                discovery = {"status": "DISCOVERING", "next_page": manifest["page_count"], "checked_pages": []}
                for index in range(manifest["page_count"] - 1, -1, -1):
                    page = pdf[index]
                    heading = page.search_for("任现职以来的考核情况")
                    for heading_rect in heading:
                        if not 0.12 <= heading_rect.y0 / page.rect.height <= 0.9:
                            continue
                        bbox = self._review_bbox(page, heading_rect, variant["review_bbox"])
                        clip = fitz.Rect(bbox[0] * page.rect.width, bbox[1] * page.rect.height,
                                         bbox[2] * page.rect.width, bbox[3] * page.rect.height)
                        text = page.get_text("text", clip=clip)
                        if not (has_review_heading(text) and re.search(
                                r"(?:19|20)\d{2}.{0,20}考核.{0,12}(?:优秀|合格|称职)", text, re.S)):
                            continue
                        regions.append({"region_id": "annual_review", "page_number": index + 1,
                                        "bbox": bbox})
                        discovery = {"status": "FOUND", "selected_page": index + 1,
                                     "next_page": None,
                                     "checked_pages": list(range(manifest["page_count"], index, -1)),
                                     "method": "PDF_TEXT_LAYER"}
                        break
                    if discovery["status"] == "FOUND":
                        break
        scope = {"contract": contract, "source_version_id": version.id, "source_sha256": version.sha256,
                 "layout_variant": variant["name"],
                 "page_count": manifest["page_count"], "required_pdf_columns": [*REQUIRED_COLUMNS, "L"],
                 "write_columns": [*list("ABCDEFGHI"), "L"], "regions": regions,
                 "review_discovery": discovery}
        scope_digest = self._scope_digest(scope)
        existing = self.db.query(ExternalExtractionTask).filter_by(
            ingest_item_id=item.id, source_version_id=version.id,
            provider_contract_version=contract).one_or_none()
        if existing is not None:
            return existing
        task = ExternalExtractionTask(
            ingest_item_id=item.id, source_version_id=version.id, source_sha256=version.sha256,
            phase="SCOPED_OCR", provider_contract_version=contract, page_manifest_json=regions,
            source_manifest_json={**manifest, **scope, "scope_digest": scope_digest},
            result_summary_json={}, status="PENDING", error_json={})
        self.db.add(task)
        self.db.flush()
        version.extraction_policy_json = {"mode": "SCOPED_EXCEL_SOURCE", "contract_version": contract,
                                          "origin_task_id": task.id, "source_sha256": version.sha256}
        item.status = "WAITING_EXTERNAL_EXTRACTION"
        item.stage = "SCOPED_OCR"
        item.result_json = {**dict(item.result_json or {}), "scoped_pdf_task_id": task.id,
                            "content_extraction_contract": contract}
        item.workflow_revision += 1
        self.db.flush()
        return task

    def _owned(self, task_id: str, user: User, *, lock: bool = False):
        task, item, batch = self.legacy._owned_task(task_id=task_id, user_id=user.id, for_update=lock)
        if task.provider_contract_version not in {LEGACY_CONTRACT, CONTRACT, DISCOVERY_CONTRACT, FIXED_25_CONTRACT}:
            self.legacy._raise(409, "EXTRACTION_CONTRACT_MISMATCH", "任务不属于定向 PDF 契约。")
        return task, item, batch

    def receipt(self, task: ExternalExtractionTask) -> dict:
        """只暴露任务范围与发现进度，不暴露候选页 OCR 全文。"""
        manifest = task.source_manifest_json or {}
        version = self.db.get(DocumentVersion, task.source_version_id)
        return {"task_id": task.id, "status": task.status,
                "provider_contract_version": task.provider_contract_version,
                "source_version_id": task.source_version_id,
                "source_document_id": version.document_id if version else None,
                "source_sha256": task.source_sha256, "scope_digest": manifest.get("scope_digest"),
                "page_count": manifest.get("page_count"), "regions": manifest.get("regions"),
                "required_pdf_columns": manifest.get("required_pdf_columns", list(REQUIRED_COLUMNS)),
                "write_columns": manifest.get("write_columns", list("ABCDEFGHI")),
                "extraction_run_id": task.extraction_run_id,
                "processed_pages": (task.result_summary_json or {}).get("processed_pages", []),
                "coverage": (task.result_summary_json or {}).get("coverage", {}),
                "review_discovery": manifest.get("review_discovery") if task.provider_contract_version in {DISCOVERY_CONTRACT, FIXED_25_CONTRACT} else None}

    def get(self, *, task_id: str, user: User) -> dict:
        task, _, _ = self._owned(task_id, user)
        return self.receipt(task)

    def upgrade_review_discovery(self, *, task_id: str, user: User) -> dict:
        """把未完成的 v2 固定末页任务换成 v3；旧租约作废且不复用错误 L 裁剪。"""
        old, item, batch = self._owned(task_id, user, lock=True)
        if old.provider_contract_version != CONTRACT:
            self.legacy._raise(409, "EXTRACTION_CONTRACT_MISMATCH", "只允许升级旧版 v2 定向任务。")
        if old.extraction_run_id or item.status != "WAITING_EXTERNAL_EXTRACTION":
            self.legacy._raise(409, "EXTRACTION_TASK_INACTIVE", "已完成或已结束的来源不能重新定位。")
        version = self.db.get(DocumentVersion, old.source_version_id)
        if (version is None or item.upload_document_version_id != old.source_version_id
                or version.sha256 != old.source_sha256):
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "旧任务来源版本已变化。")
        current_task_id = (item.result_json or {}).get("scoped_pdf_task_id")
        upgraded = self.db.query(ExternalExtractionTask).filter_by(
            ingest_item_id=item.id, source_version_id=version.id,
            provider_contract_version=DISCOVERY_CONTRACT).one_or_none()
        if current_task_id not in {old.id, upgraded.id if upgraded else None}:
            self.legacy._raise(409, "EXTRACTION_TASK_SUPERSEDED", "来源已绑定其他定向任务。")
        reused = upgraded is not None
        if upgraded is None:
            upgraded = self.prepare_for_ingest_item(item=item, contract=DISCOVERY_CONTRACT)
        if upgraded is None:
            self.legacy._raise(409, "EXTRACTION_CONTRACT_MISMATCH", "来源不再属于定向 PDF 任务。")
        if old.status != "SUPERSEDED" or current_task_id != upgraded.id:
            old.status = "SUPERSEDED"
            old.lease_owner = None
            old.lease_token_hash = None
            old.lease_expires_at = None
            version.extraction_policy_json = {"mode": "SCOPED_EXCEL_SOURCE",
                "contract_version": DISCOVERY_CONTRACT, "origin_task_id": upgraded.id,
                "source_sha256": version.sha256}
            item.result_json = {**dict(item.result_json or {}),
                                "scoped_pdf_task_id": upgraded.id,
                                "content_extraction_contract": DISCOVERY_CONTRACT}
            item.workflow_revision += 1
            self.db.flush()
            create_lifecycle_audit(db=self.db, user_id=user.id, workspace_id=batch.workspace_id,
                conversation_id=batch.conversation_id, tool_name="scoped_pdf_review_discovery_upgrade",
                message_content="旧版固定末页任务升级为倒序定位", change_type="TEXT_EXTRACTED",
                target_type="EXTERNAL_EXTRACTION_TASK", target_id=upgraded.id,
                target_document_id=version.document_id,
                after_value={"old_task_id": old.id, "new_task_id": upgraded.id,
                             "contract": DISCOVERY_CONTRACT, "source_version_id": version.id},
                execution_status="COMPLETED", visible_in_conversation=False)
        return {**self.receipt(upgraded), "provider_contract_version": DISCOVERY_CONTRACT,
                "upgraded_from_task_id": old.id, "reused": reused}

    def upgrade_fixed_25(self, *, task_id: str, user: User) -> dict:
        """将未完成的 v2/v3 任务重新签发为只读取物理第 25 页的 v4 任务。"""
        old, item, batch = self._owned(task_id, user, lock=True)
        if old.provider_contract_version not in {CONTRACT, DISCOVERY_CONTRACT}:
            self.legacy._raise(409, "EXTRACTION_CONTRACT_MISMATCH", "只允许升级未完成的 v2/v3 定向任务。")
        if old.extraction_run_id or item.status != "WAITING_EXTERNAL_EXTRACTION":
            self.legacy._raise(409, "EXTRACTION_TASK_INACTIVE", "已完成或已结束的来源不能重新签发。")
        version = self.db.get(DocumentVersion, old.source_version_id)
        if (version is None or item.upload_document_version_id != old.source_version_id
                or version.sha256 != old.source_sha256):
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "旧任务来源版本已变化。")
        current_task_id = (item.result_json or {}).get("scoped_pdf_task_id")
        upgraded = self.db.query(ExternalExtractionTask).filter_by(
            ingest_item_id=item.id, source_version_id=version.id,
            provider_contract_version=FIXED_25_CONTRACT).one_or_none()
        if current_task_id not in {old.id, upgraded.id if upgraded else None}:
            self.legacy._raise(409, "EXTRACTION_TASK_SUPERSEDED", "来源已绑定其他定向任务。")
        reused = upgraded is not None
        if upgraded is None:
            upgraded = self.prepare_for_ingest_item(item=item)
        if upgraded is None:
            self.legacy._raise(409, "EXTRACTION_CONTRACT_MISMATCH", "来源不再属于定向 PDF 任务。")
        if old.status != "SUPERSEDED" or current_task_id != upgraded.id:
            old.status = "SUPERSEDED"
            old.lease_owner = None
            old.lease_token_hash = None
            old.lease_expires_at = None
            version.extraction_policy_json = {"mode": "SCOPED_EXCEL_SOURCE",
                "contract_version": FIXED_25_CONTRACT, "origin_task_id": upgraded.id,
                "source_sha256": version.sha256}
            item.result_json = {**dict(item.result_json or {}),
                                "scoped_pdf_task_id": upgraded.id,
                                "content_extraction_contract": FIXED_25_CONTRACT}
            item.workflow_revision += 1
            self.db.flush()
            create_lifecycle_audit(db=self.db, user_id=user.id, workspace_id=batch.workspace_id,
                conversation_id=batch.conversation_id, tool_name="scoped_pdf_fixed25_upgrade",
                message_content="旧版考核栏目任务升级为固定第 25 页", change_type="TEXT_EXTRACTED",
                target_type="EXTERNAL_EXTRACTION_TASK", target_id=upgraded.id,
                target_document_id=version.document_id,
                after_value={"old_task_id": old.id, "new_task_id": upgraded.id,
                             "contract": FIXED_25_CONTRACT, "source_version_id": version.id},
                execution_status="COMPLETED", visible_in_conversation=False)
        return {**self.receipt(upgraded), "upgraded_from_task_id": old.id, "reused": reused}

    def claim(self, *, task_id: str, worker_id: str, user: User) -> dict:
        task, item, _ = self._owned(task_id, user, lock=True)
        if task.status == "SUPERSEDED" or (item.result_json or {}).get("scoped_pdf_task_id") != task.id:
            self.legacy._raise(409, "EXTRACTION_TASK_SUPERSEDED", "旧任务已由新版倒序定位任务替换。")
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
                "provider_contract_version": task.provider_contract_version}

    def _region_bytes(self, task: ExternalExtractionTask, region_id: str) -> bytes:
        region = next((r for r in (task.source_manifest_json or {}).get("regions", REGIONS)
                       if r["region_id"] == region_id), None)
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

    def _probe_bytes(self, task: ExternalExtractionTask) -> bytes:
        """仅渲染当前服务端签发的一页定位图，不接受调用方页码。"""
        discovery = (task.source_manifest_json or {}).get("review_discovery") or {}
        page_number = discovery.get("next_page")
        if (task.provider_contract_version != DISCOVERY_CONTRACT
                or discovery.get("status") != "DISCOVERING" or not page_number):
            self.legacy._raise(409, "REVIEW_DISCOVERY_NOT_PENDING", "没有待查找的考核候选页。")
        version = self.db.get(DocumentVersion, task.source_version_id)
        if version is None or version.sha256 != task.source_sha256:
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "来源版本已变化。")
        path = self.storage.upload_path(version.storage_path)
        if not path.is_file() or self.storage.sha256_file(path) != task.source_sha256:
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "来源文件已变化。")
        import fitz
        with fitz.open(path) as pdf:
            if pdf.needs_pass or not 1 <= page_number <= pdf.page_count:
                self.legacy._raise(422, "SCOPE_LOCATION_UNRESOLVED", "候选页无法读取。")
            return pdf[page_number - 1].get_pixmap(
                matrix=fitz.Matrix(1.4, 1.4), alpha=False).tobytes("png")

    def probe_info(self, *, task_id: str, worker_id: str, lease_token: str, user: User) -> dict:
        """验证租约后签发当前页哈希，避免客户端跳页或并发预读。"""
        task, _, _ = self._owned(task_id, user)
        self.legacy._validate_lease(task=task, worker_id=worker_id, lease_token=lease_token)
        if task.provider_contract_version != DISCOVERY_CONTRACT:
            self.legacy._raise(409, "REVIEW_DISCOVERY_NOT_PENDING", "当前任务不允许逐页定位。")
        discovery = (task.source_manifest_json or {}).get("review_discovery") or {}
        result = {**self.receipt(task), "page_number": discovery.get("next_page")}
        if discovery.get("status") == "DISCOVERING":
            result["image_sha256"] = hashlib.sha256(self._probe_bytes(task)).hexdigest()
        return result

    def probe_image(self, *, task_id: str, worker_id: str, lease_token: str, user: User) -> bytes:
        """验证租约并仅返回当前候选页的低分辨率图像。"""
        task, _, _ = self._owned(task_id, user)
        self.legacy._validate_lease(task=task, worker_id=worker_id, lease_token=lease_token)
        return self._probe_bytes(task)

    def probe_submit(self, *, task_id: str, payload: ReviewProbeSubmit, user: User) -> dict:
        """按当前页幂等记录定位结果；命中即冻结 L，未命中只推进一页。"""
        task, item, batch = self._owned(task_id, user, lock=True)
        if task.provider_contract_version != DISCOVERY_CONTRACT:
            self.legacy._raise(409, "EXTRACTION_CONTRACT_MISMATCH", "旧版定向任务不支持倒序找页。")
        manifest = dict(task.source_manifest_json or {})
        discovery = dict(manifest.get("review_discovery") or {})
        body = payload.model_dump(mode="json", exclude={"lease_token"})
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")).encode()).hexdigest()
        if discovery.get("last_submission_key") == payload.submission_key:
            if discovery.get("last_submission_digest") != digest:
                self.legacy._raise(409, "IDEMPOTENCY_CONFLICT", "定位提交键已绑定不同结果。")
            return {**self.receipt(task), "reused": True}
        self.legacy._validate_lease(task=task, worker_id=payload.worker_id, lease_token=payload.lease_token)
        if discovery.get("status") != "DISCOVERING":
            self.legacy._raise(409, "REVIEW_DISCOVERY_NOT_PENDING", "考核栏目定位已结束。")
        page_number = discovery.get("next_page")
        if payload.page_number != page_number:
            self.legacy._raise(409, "REVIEW_PROBE_PAGE_MISMATCH", "只能提交当前倒序候选页。")
        if payload.image_sha256 != hashlib.sha256(self._probe_bytes(task)).hexdigest():
            self.legacy._raise(409, "SCOPE_IMAGE_CHANGED", "定位页图像哈希不一致。")
        checked = [*discovery.get("checked_pages", []), page_number]
        discovery.update(checked_pages=checked, last_submission_key=payload.submission_key,
                         last_submission_digest=digest, last_provider_name=payload.provider_name)
        if payload.section_confirmed and not has_review_heading(payload.text):
            self.legacy._raise(422, "ANNUAL_REVIEW_SECTION_MISSING",
                               "已确认栏目必须包含真实标题 OCR 文字。")
        if payload.section_confirmed:
            assert payload.heading_bbox is not None
            _, top, _, bottom = payload.heading_bbox
            bbox = [0.08, max(0.0, top - 0.025), 0.97,
                    min(0.98, max(top + 0.30, bottom + 0.28))]
            regions = [*REGIONS, {"region_id": "annual_review", "page_number": page_number,
                                  "bbox": bbox}]
            discovery.update(status="FOUND", selected_page=page_number, next_page=None,
                             method="WORKBUDDY_OCR")
        else:
            regions = list(REGIONS)
            discovery.update(status="NOT_FOUND" if page_number == 1 else "DISCOVERING",
                             next_page=page_number - 1 if page_number > 1 else None)
        manifest.update(regions=regions, review_discovery=discovery)
        manifest["scope_digest"] = self._scope_digest(manifest)
        task.page_manifest_json = regions
        task.source_manifest_json = manifest
        item.workflow_revision += 1
        self.db.flush()
        create_lifecycle_audit(db=self.db, user_id=user.id, workspace_id=batch.workspace_id,
            conversation_id=batch.conversation_id, tool_name="scoped_pdf_review_probe_submit",
            message_content="WorkBuddy 倒序定位考核栏目", change_type="TEXT_EXTRACTED",
            target_type="EXTERNAL_EXTRACTION_TASK", target_id=task.id,
            target_document_id=self.db.get(DocumentVersion, task.source_version_id).document_id,
            after_value={"page_number": page_number, "discovery_status": discovery["status"],
                         "checked_page_count": len(checked), "provider_name": payload.provider_name},
            execution_status="COMPLETED", visible_in_conversation=False)
        return {**self.receipt(task), "reused": False}

    def reject_review_region(self, *, task_id: str, payload: ReviewRegionReject, user: User) -> dict:
        """冻结图缺少栏目时作废 L 范围；v4 留空，v3 才继续定位。"""
        task, item, batch = self._owned(task_id, user, lock=True)
        if task.provider_contract_version not in {DISCOVERY_CONTRACT, FIXED_25_CONTRACT}:
            self.legacy._raise(409, "EXTRACTION_CONTRACT_MISMATCH", "旧版任务须先升级为倒序定位。")
        manifest = dict(task.source_manifest_json or {})
        discovery = dict(manifest.get("review_discovery") or {})
        body = payload.model_dump(mode="json", exclude={"lease_token"})
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")).encode()).hexdigest()
        if discovery.get("last_rejection_key") == payload.submission_key:
            if discovery.get("last_rejection_digest") != digest:
                self.legacy._raise(409, "IDEMPOTENCY_CONFLICT", "栏目退回键已绑定不同结果。")
            return {**self.receipt(task), "reused": True}
        self.legacy._validate_lease(task=task, worker_id=payload.worker_id, lease_token=payload.lease_token)
        if discovery.get("status") != "FOUND" or discovery.get("selected_page") is None:
            self.legacy._raise(409, "REVIEW_DISCOVERY_NOT_FOUND", "当前没有可退回的冻结栏目。")
        if payload.image_sha256 != hashlib.sha256(self._region_bytes(task, "annual_review")).hexdigest():
            self.legacy._raise(409, "SCOPE_IMAGE_CHANGED", "退回的栏目图像与当前冻结范围不一致。")
        rejected_page = discovery["selected_page"]
        continue_backwards = task.provider_contract_version == DISCOVERY_CONTRACT and rejected_page > 1
        discovery.update(status="DISCOVERING" if continue_backwards else "NOT_FOUND",
                         selected_page=None, next_page=rejected_page - 1 if continue_backwards else None,
                         rejected_pages=[*discovery.get("rejected_pages", []), rejected_page],
                         last_rejection_key=payload.submission_key,
                         last_rejection_digest=digest)
        manifest.update(regions=list(REGIONS), review_discovery=discovery)
        manifest["scope_digest"] = self._scope_digest(manifest)
        task.page_manifest_json = list(REGIONS)
        task.source_manifest_json = manifest
        item.workflow_revision += 1
        self.db.flush()
        version = self.db.get(DocumentVersion, task.source_version_id)
        create_lifecycle_audit(db=self.db, user_id=user.id, workspace_id=batch.workspace_id,
            conversation_id=batch.conversation_id, tool_name="scoped_pdf_review_region_reject",
            message_content="冻结考核栏目图缺少正文，按当前契约处理", change_type="TEXT_EXTRACTED",
            target_type="EXTERNAL_EXTRACTION_TASK", target_id=task.id,
            target_document_id=version.document_id,
            after_value={"rejected_page": rejected_page, "reason": payload.reason,
                         "next_page": discovery["next_page"]},
            execution_status="COMPLETED", visible_in_conversation=False)
        return {**self.receipt(task), "reused": False}

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
        """只接收已冻结的 B–I 与 L 范围；缺栏目时仅允许带留空原因。"""
        task, item, batch = self._owned(task_id, user, lock=True)
        body = payload.model_dump(mode="json", exclude={"lease_token"})
        if body.get("annual_review") is None:
            body.pop("annual_review", None)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if task.last_submission_key == payload.submission_key:
            if task.last_submission_digest != digest:
                self.legacy._raise(409, "IDEMPOTENCY_CONFLICT", "提交键已绑定不同结果。")
            return {**self.receipt(task), "reused": True}
        self.legacy._validate_lease(task=task, worker_id=payload.worker_id, lease_token=payload.lease_token)
        manifest = task.source_manifest_json or {}
        is_review_scope = task.provider_contract_version in {CONTRACT, DISCOVERY_CONTRACT, FIXED_25_CONTRACT}
        discovery = manifest.get("review_discovery") or {}
        if task.provider_contract_version in {DISCOVERY_CONTRACT, FIXED_25_CONTRACT} and discovery.get("status") not in {"FOUND", "NOT_FOUND"}:
            self.legacy._raise(409, "REVIEW_DISCOVERY_NOT_COMPLETE", "须先完成倒序考核栏目定位。")
        regions_spec = manifest.get("regions", list(REGIONS))
        expected_region_ids = {region["region_id"] for region in regions_spec}
        if ({item.region_id for item in payload.regions} != expected_region_ids
                or (payload.annual_review is not None) != is_review_scope):
            self.legacy._raise(422, "SCOPE_REGION_MISMATCH", "OCR 结果与冻结区域或 L 列契约不一致。")
        if (task.provider_contract_version != FIXED_25_CONTRACT
                and {field.column for field in payload.fields} != set(REQUIRED_COLUMNS)):
            self.legacy._raise(422, "SCOPE_FIELD_MISSING", "旧版定向任务仍须覆盖 B–I 八列。")
        if is_review_scope and ((discovery.get("status") == "NOT_FOUND")
                != (payload.annual_review.status == "NOT_FOUND")):
            self.legacy._raise(422, "ANNUAL_REVIEW_STATUS_MISMATCH", "考核栏目状态与倒序定位结果不一致。")
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
        if is_review_scope:
            review = payload.annual_review
            review_text = regions["annual_review"].text if "annual_review" in regions else ""
            if review.status != "NOT_FOUND" and not has_review_heading(review_text):
                self.legacy._raise(422, "ANNUAL_REVIEW_SECTION_MISSING", "冻结区域未定位任现职以来的考核情况。")
            detected = excellent_years(review_text)
            submitted = [item.year for item in review.years]
            if review.status == "EXCELLENT_FOUND" and submitted != detected:
                self.legacy._raise(422, "ANNUAL_REVIEW_YEARS_MISMATCH", "优秀年度与该栏 OCR 原文不一致。")
            if review.status == "NO_EXCELLENT" and detected:
                self.legacy._raise(422, "ANNUAL_REVIEW_YEARS_MISMATCH", "OCR 含优秀线索，不能声明没有优秀年度。")
            if review.status == "UNREADABLE" and detected:
                self.legacy._raise(422, "ANNUAL_REVIEW_YEARS_MISMATCH", "清晰优秀年度不能标记为未识别。")
            year_refs = []
            for year_item in review.years:
                if year_item.start is None:
                    start = review_text.find(year_item.quote)
                    if start < 0 or review_text.find(year_item.quote, start + 1) >= 0:
                        self.legacy._raise(422, "EVIDENCE_AMBIGUOUS", "L 列优秀年度引文不唯一。")
                    end = start + len(year_item.quote)
                else:
                    start, end = year_item.start, year_item.end
                    if review_text[start:end] != year_item.quote:
                        self.legacy._raise(422, "EVIDENCE_SCOPE_MISMATCH", "L 列优秀年度引文不匹配。")
                year_refs.append({"year": year_item.year, "start": start, "end": end})
            field_refs["L"] = {"region_id": "annual_review" if review.status != "NOT_FOUND" else None,
                               "status": review.status,
                               "value": "、".join(map(str, submitted)) if submitted else None,
                               "years": year_refs}
        version = self.db.get(DocumentVersion, task.source_version_id)
        document = self.db.get(Document, version.document_id) if version else None
        if version is None or document is None:
            self.legacy._raise(409, "SOURCE_VERSION_MISSING", "来源版本不存在。")
        run = DocumentExtractionRun(document_id=document.id, document_version_id=version.id,
            status="SCOPE_COMPLETE", extractor="workbuddy-scoped-pdf", parser_name=payload.provider_name,
            parser_version=payload.provider_version or CONTRACT, parser_config_hash=payload.scope_digest)
        self.db.add(run)
        self.db.flush()
        for order, region in enumerate(regions_spec):
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
        required_columns = manifest.get("required_pdf_columns", list(REQUIRED_COLUMNS))
        resolved_columns = [column for column in REQUIRED_COLUMNS if column in field_refs]
        if is_review_scope:
            resolved_columns.append("L")
        processed_pages = sorted({region["page_number"] for region in regions_spec})
        task.result_summary_json = {"coverage": {"status": "SCOPE_COMPLETE", "required_pdf_columns": required_columns,
            "resolved_columns": resolved_columns, "annual_review_status":
            payload.annual_review.status if is_review_scope else None}, "processed_pages": processed_pages,
            "review_checked_pages": discovery.get("checked_pages", []),
            "region_count": len(regions_spec), "provider_name": payload.provider_name, "field_refs": field_refs}
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
            after_value={"contract": task.provider_contract_version, "scope_digest": payload.scope_digest,
                         "processed_pages": processed_pages, "region_count": len(regions_spec), "resolved_columns": resolved_columns,
                         "provider_name": payload.provider_name, "extraction_run_id": run.id},
            execution_status="COMPLETED", visible_in_conversation=False)
        IngestionWorkflow(self.db).synchronize_batch(batch)
        return {**self.receipt(task), "reused": False}
