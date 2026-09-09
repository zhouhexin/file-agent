"""WorkBuddy 外部 OCR 页面准备、租约与正式结果回写。"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Document,
    DocumentExtractionRun,
    DocumentPage,
    DocumentVersion,
    ExternalExtractionPage,
    ExternalExtractionTask,
    IngestBatch,
    IngestItem,
    UploadArchiveRecord,
    UploadDuplicateCandidate,
    UploadDuplicateReview,
    User,
    WorkingCopy,
    utcnow,
)
from app.modules.file_lifecycle.service import UploadLifecycleService
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.ingestion.workflow import IngestionWorkflow


EXTERNAL_OCR_CONTRACT_VERSION = "workbuddy-ocr-v1"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


class ExternalExtractionService:
    """管理外部 OCR 任务，所有资源和结果均绑定当前用户与有效租约。"""

    def __init__(self, db: Session) -> None:
        """注入请求级数据库会话和生命周期存储。"""

        self.db = db
        self.settings = get_settings()
        self.storage = FileLifecycleStorageService(self.settings)

    def prepare_for_ingest_item(self, *, item: IngestItem) -> ExternalExtractionTask | None:
        """为扫描图片或 PDF 生成可领取页资源；其他格式返回空并走原生流程。"""

        if not self.settings.integration_external_ocr_enabled or not item.upload_document_version_id:
            return None
        version = self.db.get(DocumentVersion, item.upload_document_version_id)
        if version is None:
            return None
        suffix = Path(version.filename).suffix.lower()
        if suffix not in _IMAGE_SUFFIXES | {".pdf"}:
            return None
        existing = (
            self.db.query(ExternalExtractionTask)
            .filter(
                ExternalExtractionTask.ingest_item_id == item.id,
                ExternalExtractionTask.source_version_id == version.id,
                ExternalExtractionTask.provider_contract_version == EXTERNAL_OCR_CONTRACT_VERSION,
            )
            .one_or_none()
        )
        if existing is not None:
            return existing
        source_path = self.storage.upload_path(version.storage_path)
        manifest = self._build_page_manifest(item=item, version=version, source_path=source_path)
        if not manifest:
            # PDF 每一页都有原生文字时无需外部 OCR，继续既有解析/近似查重流程。
            return None
        task = ExternalExtractionTask(
            ingest_item_id=item.id,
            source_version_id=version.id,
            source_sha256=version.sha256,
            phase="PARSE_STAGING",
            provider_contract_version=EXTERNAL_OCR_CONTRACT_VERSION,
            page_manifest_json=manifest,
            status="PENDING",
            error_json={},
        )
        self.db.add(task)
        self.db.flush()
        for page in manifest:
            self.db.add(
                ExternalExtractionPage(
                    task_id=task.id,
                    page_number=int(page["page_number"]),
                    status="PENDING",
                    result_json={},
                    error_json={},
                )
            )
        item.status = "WAITING_EXTERNAL_EXTRACTION"
        item.stage = "PARSE_STAGING"
        item.result_json = {
            **dict(item.result_json or {}),
            "external_extraction_task_id": task.id,
            "external_extraction_page_count": len(manifest),
        }
        item.workflow_revision += 1
        self.db.flush()
        return task

    def claim(
        self,
        *,
        task_id: str,
        worker_id: str,
        current_user: User,
    ) -> dict[str, Any]:
        """发放随机租约 token；数据库只保存 token 哈希。"""

        task, item, _ = self._owned_task(task_id=task_id, user_id=current_user.id, for_update=True)
        now = utcnow()
        if task.status == "COMPLETED":
            self._raise(409, "EXTRACTION_ALREADY_COMPLETED", "外部提取任务已经完成。")
        if task.status == "PARTIAL" and item.final_document_id:
            self._raise(409, "EXTRACTION_ALREADY_PUBLISHED", "文件已经按部分结果发布，请通过显式重处理改善正文。")
        if task.status == "CLAIMED" and task.lease_expires_at:
            expires = self._aware(task.lease_expires_at)
            if expires > now and task.lease_owner != worker_id:
                self._raise(409, "EXTRACTION_ALREADY_CLAIMED", "任务正在由其他 Worker 处理。")
        token = secrets.token_urlsafe(32)
        task.lease_owner = worker_id
        task.lease_token_hash = self._token_hash(token)
        task.lease_expires_at = now + timedelta(seconds=self.settings.external_extraction_lease_seconds)
        task.status = "CLAIMED"
        task.attempt_count += 1
        self.db.commit()
        return {
            "task_id": task.id,
            "source_sha256": task.source_sha256,
            "source_version_id": task.source_version_id,
            "provider_contract_version": task.provider_contract_version,
            "lease_token": token,
            "lease_expires_at": task.lease_expires_at,
            "pages": [
                {
                    "page_number": int(page["page_number"]),
                    "content_type": str(page["content_type"]),
                    "resource_url": f"/api/integrations/v1/extraction-tasks/{task.id}/pages/{page['page_number']}",
                }
                for page in task.page_manifest_json
                if int(page["page_number"]) in self._expected_submission_pages(task)
            ],
        }

    def renew(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_token: str,
        current_user: User,
    ) -> ExternalExtractionTask:
        """只续当前有效租约，不允许过期 token 复活任务。"""

        task, _, _ = self._owned_task(task_id=task_id, user_id=current_user.id, for_update=True)
        self._validate_lease(task=task, worker_id=worker_id, lease_token=lease_token)
        task.lease_expires_at = utcnow() + timedelta(seconds=self.settings.external_extraction_lease_seconds)
        self.db.commit()
        self.db.refresh(task)
        return task

    def page_response(
        self,
        *,
        task_id: str,
        page_number: int,
        worker_id: str,
        lease_token: str,
        current_user: User,
    ) -> FileResponse:
        """在有效租约内返回真实图片资源，不暴露物理路径。"""

        task, _, _ = self._owned_task(task_id=task_id, user_id=current_user.id)
        self._validate_lease(task=task, worker_id=worker_id, lease_token=lease_token)
        entry = next(
            (page for page in task.page_manifest_json if int(page["page_number"]) == page_number),
            None,
        )
        if entry is None:
            self._raise(404, "EXTRACTION_PAGE_NOT_FOUND", "提取页面不存在。")
        path = self._safe_storage_path(str(entry["storage_path"]))
        if not path.is_file():
            self._raise(404, "EXTRACTION_PAGE_NOT_FOUND", "提取页面资源不存在。")
        return FileResponse(path=path, media_type=str(entry["content_type"]), filename=f"page-{page_number}.png")

    def submit(
        self,
        *,
        task_id: str,
        payload: Any,
        current_user: User,
    ) -> tuple[ExternalExtractionTask, str, str | None, bool]:
        """验收完整页集合、写入正式页面文本，并继续近似查重或归档。"""

        task, item, batch = self._owned_task(task_id=task_id, user_id=current_user.id, for_update=True)
        digest = self._submission_digest(payload)
        if task.last_submission_key == payload.submission_key:
            if task.last_submission_digest != digest:
                self._raise(409, "IDEMPOTENCY_CONFLICT", "同一提交键已经用于不同 OCR 结果。")
            return task, item.stage, item.current_job_id, True
        self._validate_lease(task=task, worker_id=payload.worker_id, lease_token=payload.lease_token)
        if payload.source_sha256 != task.source_sha256 or payload.source_version_id != task.source_version_id:
            self._raise(409, "SOURCE_SNAPSHOT_CHANGED", "OCR 结果对应的源版本或哈希不一致。")
        expected_pages = self._expected_submission_pages(task)
        submitted_pages = {page.page_number for page in payload.pages}
        if submitted_pages != expected_pages:
            self._raise(
                409,
                "EXTRACTION_PAGE_SCOPE_MISMATCH",
                "OCR 提交必须覆盖任务固定的完整页面集合。",
                details={"expected": sorted(expected_pages), "submitted": sorted(submitted_pages)},
            )
        version = self.db.get(DocumentVersion, task.source_version_id)
        document = self.db.get(Document, version.document_id) if version else None
        if version is None or document is None:
            self._raise(409, "SOURCE_VERSION_MISSING", "OCR 源版本不存在。")
        repository = FileExtractionRepository(self.db, current_user.id)
        run = repository.create_extraction_run(
            document_id=document.id,
            document_version_id=version.id,
            extractor="workbuddy-external-ocr",
            parser_name="external-ocr",
            parser_version=task.provider_contract_version,
            parser_config_hash=hashlib.sha256(task.provider_contract_version.encode()).hexdigest(),
        )
        submitted_by_page = {submitted.page_number: submitted for submitted in payload.pages}
        failed_pages = [
            submitted.page_number
            for submitted in payload.pages
            if submitted.error and not submitted.text.strip()
        ]
        if failed_pages and not self.settings.integration_allow_partial_extraction:
            self._raise(
                409,
                "EXTRACTION_PARTIAL_NOT_ALLOWED",
                "当前策略不允许提交不完整的 OCR 结果。",
                details={"failed_pages": sorted(failed_pages)},
            )
        pages = self._merge_native_and_external_pages(
            version=version,
            submitted_by_page=submitted_by_page,
        )
        if task.extraction_run_id and self._failed_page_numbers(task):
            pages = self._merge_previous_successful_pages(
                previous_run_id=task.extraction_run_id,
                pages=pages,
                submitted_page_numbers=submitted_pages,
            )
        for submitted in sorted(payload.pages, key=lambda value: value.page_number):
            metadata = {
                "ocr_fallback": True,
                "ocr_source": "workbuddy_external",
                "ocr_provider": submitted.provider_name,
                "ocr_provider_version": submitted.provider_version,
                "ocr_provider_request_id": submitted.provider_request_id,
                "ocr_confidence": submitted.confidence,
                "ocr_blocks": submitted.blocks,
                "ocr_error": submitted.error,
            }
            for page in pages:
                if page["page_number"] == submitted.page_number:
                    page["metadata"] = metadata
                    break
            page_row = (
                self.db.query(ExternalExtractionPage)
                .filter(ExternalExtractionPage.task_id == task.id, ExternalExtractionPage.page_number == submitted.page_number)
                .one()
            )
            page_row.status = "FAILED" if submitted.error and not submitted.text else "COMPLETED"
            page_row.result_digest = hashlib.sha256(
                json.dumps(submitted.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            page_row.result_json = {"char_count": len(submitted.text), "provider_name": submitted.provider_name}
            page_row.error_json = dict(submitted.error or {})
        repository.complete_extraction_run(run=run, pages=pages)
        task.extraction_run_id = run.id
        task.last_submission_key = payload.submission_key
        task.last_submission_digest = digest
        task.lease_token_hash = None
        task.lease_expires_at = None
        task.status = "PARTIAL" if failed_pages else "COMPLETED"
        task.error_json = (
            {
                "code": "EXTRACTION_PARTIAL",
                "message": "部分页面 OCR 未成功，已保留成功页并继续保守整理。",
                "failed_pages": sorted(failed_pages),
                "submitted_page_count": len(payload.pages),
                "successful_page_count": len(payload.pages) - len(failed_pages),
            }
            if failed_pages
            else {}
        )
        item.extraction_run_id = run.id
        item.result_json = {
            **dict(item.result_json or {}),
            "external_extraction_status": task.status,
            "external_extraction_failed_pages": sorted(failed_pages),
            "external_extraction_page_count": len(payload.pages),
        }
        item.stage = "NEAR_CHECK"
        item.status = "RUNNING"
        item.workflow_revision += 1
        review = (
            self.db.query(UploadDuplicateReview)
            .filter(UploadDuplicateReview.ingest_item_id == item.id)
            .one()
        )
        candidates = self._near_candidates(item=item, review=review, pages=pages)
        if candidates:
            review.status = "WAITING_CONFIRMATION"
            review.comparison_phase = "NEAR"
            review.revision += 1
            archive = self.db.get(UploadArchiveRecord, item.archive_record_id)
            if archive:
                archive.status = "WAITING_DUPLICATE_CONFIRMATION"
            item.status = "WAITING_DUPLICATE_CONFIRMATION"
            item.stage = "DUPLICATE_DECISION"
            next_job_id = None
        else:
            archive = self.db.get(UploadArchiveRecord, item.archive_record_id)
            if archive is None:
                self._raise(409, "ARCHIVE_STATE_MISSING", "上传归档状态不存在。")
            review.status = "RESOLVED"
            review.decision = "CONTINUE_UPLOAD"
            review.decided_at = utcnow()
            archive.status = "PENDING"
            archive_job = UploadLifecycleService(self.db).enqueue_archive_after_external_extraction(
                review=review,
                archive=archive,
            )
            archive.filesystem_job_id = archive_job.id
            item.current_job_id = archive_job.id
            item.stage = "ARCHIVE"
            next_job_id = archive_job.id
        IngestionWorkflow(self.db).synchronize_batch(batch)
        batch.result_revision += 1
        self.db.commit()
        return task, item.stage, next_job_id, False

    def _expected_submission_pages(self, task: ExternalExtractionTask) -> set[int]:
        """首次提交覆盖固定缺页集合；PARTIAL 重试只覆盖此前失败页。"""

        failed_pages = self._failed_page_numbers(task)
        if not task.extraction_run_id or not failed_pages:
            return {int(page["page_number"]) for page in task.page_manifest_json}
        return failed_pages

    def _failed_page_numbers(self, task: ExternalExtractionTask) -> set[int]:
        """读取仍失败的固定页号；claim 的 CLAIMED 状态不能抹掉上一轮部分结果范围。"""

        return {
            page.page_number
            for page in self.db.query(ExternalExtractionPage).filter(
                ExternalExtractionPage.task_id == task.id,
                ExternalExtractionPage.status == "FAILED",
            ).all()
        }

    def _merge_previous_successful_pages(
        self,
        *,
        previous_run_id: str,
        pages: list[dict[str, Any]],
        submitted_page_numbers: set[int],
    ) -> list[dict[str, Any]]:
        """重试失败页时复用上一运行的成功页，原生文字和已成功 OCR 不得丢失。"""

        merged = {int(page["page_number"]): dict(page) for page in pages}
        for previous in self.db.query(DocumentPage).filter(
            DocumentPage.extraction_run_id == previous_run_id
        ).all():
            if previous.page_number is None or previous.page_number in submitted_page_numbers:
                continue
            merged[previous.page_number] = {
                "page_number": previous.page_number,
                "sheet_name": previous.sheet_name,
                "text": previous.text_content,
                "metadata": dict(previous.metadata_json or {}),
            }
        return [merged[number] for number in sorted(merged)]

    def to_response(self, task: ExternalExtractionTask) -> dict[str, Any]:
        """投影不含租约哈希和存储路径的任务状态。"""

        return {
            "id": task.id,
            "ingest_item_id": task.ingest_item_id,
            "source_version_id": task.source_version_id,
            "source_sha256": task.source_sha256,
            "phase": task.phase,
            "provider_contract_version": task.provider_contract_version,
            "status": task.status,
            "page_numbers": [int(page["page_number"]) for page in task.page_manifest_json],
            "extraction_run_id": task.extraction_run_id,
            "error": dict(task.error_json or {}),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    def cleanup_expired_page_resources(self) -> dict[str, int]:
        """清理超过保留期的 OCR 页图，但保留任务、页状态、摘要和错误审计。"""

        cutoff = utcnow() - timedelta(hours=self.settings.external_page_retention_hours)
        tasks = (
            self.db.query(ExternalExtractionTask)
            .join(IngestItem, IngestItem.id == ExternalExtractionTask.ingest_item_id)
            .filter(
                or_(
                    ExternalExtractionTask.status.in_(["COMPLETED", "PARTIAL", "FAILED"]),
                    IngestItem.status.in_(["SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "EXPIRED"]),
                ),
                ExternalExtractionTask.updated_at < cutoff,
            )
            .all()
        )
        cleaned_files = 0
        cleaned_tasks = 0
        for task in tasks:
            manifest = []
            changed = False
            for entry in list(task.page_manifest_json or []):
                updated = dict(entry)
                storage_path = str(updated.get("storage_path") or "")
                # 图片原件本身位于上传暂存区，由上传生命周期清理；这里只删除服务端
                # 为 PDF 缺页生成的 external-extraction 派生图，绝不误删原件。
                if storage_path.startswith("external-extraction/") and not updated.get("resource_cleaned"):
                    path = self._safe_storage_path(storage_path)
                    if path.is_file():
                        path.unlink()
                        cleaned_files += 1
                    updated["resource_cleaned"] = True
                    changed = True
                manifest.append(updated)
            if changed:
                task.page_manifest_json = manifest
                cleaned_tasks += 1
                for page in self.db.query(ExternalExtractionPage).filter(
                    ExternalExtractionPage.task_id == task.id
                ).all():
                    page.result_json = {
                        **dict(page.result_json or {}),
                        "resource_cleaned": True,
                        "resource_cleaned_at": utcnow().isoformat(),
                    }
        self.db.flush()
        return {"tasks_checked": len(tasks), "tasks_cleaned": cleaned_tasks, "files_cleaned": cleaned_files}

    def _build_page_manifest(self, *, item: IngestItem, version: DocumentVersion, source_path: Path) -> list[dict]:
        """把图片或 PDF 渲染为受控页面资源，并记录相对存储路径。"""

        suffix = Path(version.filename).suffix.lower()
        root = Path(self.settings.file_storage_root).resolve()
        if suffix in _IMAGE_SUFFIXES:
            return [{
                "page_number": 1,
                "content_type": version.content_type if version.content_type.startswith("image/") else "image/png",
                "storage_path": source_path.resolve().relative_to(root).as_posix(),
            }]
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("缺少 PyMuPDF，无法准备外部 OCR 页面") from exc
        output_dir = root / "external-extraction" / item.id
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        with fitz.open(source_path) as pdf:
            for index, page in enumerate(pdf, start=1):
                if page.get_text("text").strip():
                    # 混合 PDF 的原生文字页不渲染、不外发，也不会被 OCR 结果覆盖。
                    continue
                output = output_dir / f"page-{index}.png"
                page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(output)
                manifest.append({
                    "page_number": index,
                    "content_type": "image/png",
                    "storage_path": output.relative_to(root).as_posix(),
                })
        return manifest

    def _merge_native_and_external_pages(
        self,
        *,
        version: DocumentVersion,
        submitted_by_page: dict[int, Any],
    ) -> list[dict[str, Any]]:
        """保留混合 PDF 原生文字页，只在固定缺页位置注入外部 OCR 文本。"""

        suffix = Path(version.filename).suffix.lower()
        if suffix in _IMAGE_SUFFIXES:
            submitted = submitted_by_page[1]
            return [{"page_number": 1, "text": submitted.text, "metadata": {}}]
        source_path = self.storage.upload_path(version.storage_path)
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("缺少 PyMuPDF，无法合并 PDF OCR 结果") from exc
        pages: list[dict[str, Any]] = []
        with fitz.open(source_path) as pdf:
            for index, page in enumerate(pdf, start=1):
                native_text = page.get_text("text")
                submitted = submitted_by_page.get(index)
                pages.append(
                    {
                        "page_number": index,
                        "text": native_text if native_text.strip() else submitted.text if submitted else "",
                        "metadata": {"native_text_layer": bool(native_text.strip())},
                    }
                )
        return pages

    def _near_candidates(self, *, item: IngestItem, review: UploadDuplicateReview, pages: list[dict]) -> list[UploadDuplicateCandidate]:
        """用 OCR 全文 token Jaccard 生成可审计近似候选。"""

        source_tokens = self._tokens("\n".join(str(page["text"]) for page in pages))
        if not source_tokens:
            return []
        candidates = []
        copies = self.db.query(WorkingCopy).filter(WorkingCopy.status == "ACTIVE").limit(100).all()
        for working_copy in copies:
            rows = (
                self.db.query(DocumentPage)
                .join(
                    DocumentExtractionRun,
                    DocumentExtractionRun.id == DocumentPage.extraction_run_id,
                )
                .filter(
                    DocumentPage.document_id == working_copy.document_id,
                    DocumentExtractionRun.document_version_id == working_copy.current_version_id,
                    DocumentExtractionRun.status == "COMPLETED",
                )
                .all()
            )
            tokens = self._tokens("\n".join(row.text_content for row in rows))
            if not tokens:
                continue
            score = len(source_tokens & tokens) / max(1, len(source_tokens | tokens))
            if score < self.settings.upload_duplicate_similarity_threshold:
                continue
            candidate = UploadDuplicateCandidate(
                duplicate_review_id=review.id,
                candidate_managed_file_id=working_copy.managed_file_id,
                candidate_working_copy_id=working_copy.id,
                compared_version_id=working_copy.current_version_id,
                compared_sha256=working_copy.content_sha256,
                compared_working_copy_revision=working_copy.revision,
                match_type="NEAR_DUPLICATE",
                match_scope="SAME_WORKSPACE",
                similarity_score=score,
                match_evidence_json={"method": "external_ocr_token_jaccard_v1"},
                user_visible_summary_json={"filename": working_copy.filename, "similarity_bucket": f"{int(score * 100)}%"},
                rank=len(candidates) + 1,
            )
            self.db.add(candidate)
            candidates.append(candidate)
            if len(candidates) >= self.settings.upload_duplicate_max_candidates:
                break
        self.db.flush()
        return candidates

    def _owned_task(self, *, task_id: str, user_id: str, for_update: bool = False) -> tuple[ExternalExtractionTask, IngestItem, IngestBatch]:
        """加载当前用户任务，隐藏其他用户任务是否存在。"""

        query = self.db.query(ExternalExtractionTask).filter(ExternalExtractionTask.id == task_id)
        task = query.with_for_update().one_or_none() if for_update else query.one_or_none()
        item_query = (
            self.db.query(IngestItem).filter(IngestItem.id == task.ingest_item_id)
            if task is not None
            else None
        )
        if item_query is not None and for_update:
            item_query = item_query.with_for_update()
        item = item_query.one_or_none() if item_query is not None else None
        batch_query = (
            self.db.query(IngestBatch).filter(IngestBatch.id == item.batch_id)
            if item is not None
            else None
        )
        if batch_query is not None and for_update:
            batch_query = batch_query.with_for_update()
        batch = batch_query.one_or_none() if batch_query is not None else None
        if task is None or item is None or batch is None or batch.user_id != user_id:
            self._raise(404, "EXTRACTION_TASK_NOT_FOUND", "外部提取任务不存在或无权访问。")
        return task, item, batch

    def _validate_lease(self, *, task: ExternalExtractionTask, worker_id: str, lease_token: str) -> None:
        """校验 Worker、token 哈希、状态和到期时间。"""

        if (
            task.status != "CLAIMED"
            or task.lease_owner != worker_id
            or task.lease_token_hash != self._token_hash(lease_token)
            or not task.lease_expires_at
            or self._aware(task.lease_expires_at) <= utcnow()
        ):
            self._raise(409, "LEASE_EXPIRED", "外部提取租约无效或已经过期，请重新领取。")

    def _safe_storage_path(self, relative_path: str) -> Path:
        """把页资源路径限制在 File Agent 存储根内。"""

        root = Path(self.settings.file_storage_root).resolve()
        path = (root / relative_path).resolve()
        if path == root or root not in path.parents:
            self._raise(404, "EXTRACTION_PAGE_NOT_FOUND", "提取页面路径无效。")
        return path

    @staticmethod
    def _tokens(text: str) -> set[str]:
        """保守规范化 OCR 文本，不移除日期、数字或否定词。"""

        import re
        return {token.lower() for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", text) if len(token) <= 80}

    @staticmethod
    def _token_hash(token: str) -> str:
        """只持久化租约 token 的 SHA-256。"""

        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _submission_digest(payload: Any) -> str:
        """排除明文租约 token 后计算结果幂等摘要。"""

        data = payload.model_dump(mode="json", exclude={"lease_token", "submission_key"})
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _aware(value):
        """兼容 SQLite 测试丢失时区的时间值。"""

        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _raise(status: int, code: str, message: str, *, details: dict | None = None) -> None:
        """抛出统一错误 Envelope 可识别的异常。"""

        raise HTTPException(status_code=status, detail={"code": code, "message": message, "details": details})
