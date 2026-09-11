"""WorkBuddy 重复确认的固定版本预览与下载服务。

本模块只从已经冻结的重复候选解析两侧资源。它不创建解析、归档、分类或工作副本任务，
也不接受客户端路径、Document ID 或 URL 作为读取目标。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Document,
    DocumentExtractionRun,
    DocumentPage,
    DocumentVersion,
    IngestBatch,
    IngestDuplicateGroup,
    IngestDuplicateGroupMember,
    IngestItem,
    ManagedFile,
    ManagedRoot,
    UploadDuplicateCandidate,
    UploadDuplicateReview,
    User,
    WorkingCopy,
    utcnow,
)
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.files.content_types import infer_content_type
from app.modules.ingestion.schemas import (
    IngestDuplicateComparisonQuery,
    IngestDuplicateComparisonResponse,
    IngestDuplicateComparisonSide,
    IngestDuplicateContentQuery,
    IngestDuplicatePreviewQuery,
    IngestDuplicatePreviewResponse,
    IngestDuplicatePreviewSection,
)
from app.modules.managed_files.path_policy import PathPolicyError, resolve_managed_relative_path


@dataclass(frozen=True)
class _ResolvedSide:
    """服务端内部固定的文件侧；绝不直接序列化路径或哈希。"""

    source_kind: str
    version: DocumentVersion | None
    filename: str
    content_type: str
    size_bytes: int
    expected_sha256: str | None
    path: Path | None
    revision_marker: str
    reason_code: str | None = None


class IngestDuplicateComparisonService:
    """以 review、候选和修订为边界提供只读的两侧资源。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话；本服务不提交事务或修改业务状态。"""

        self.db = db
        self.storage = FileLifecycleStorageService()

    def comparison(
        self,
        *,
        item_id: str,
        query: IngestDuplicateComparisonQuery,
        current_user: User,
    ) -> IngestDuplicateComparisonResponse:
        """返回脱敏元数据、固定快照和浏览器入口，不返回文件内容。"""

        item, review, candidate, group_revision = self._context(
            item_id=item_id, query=query, current_user=current_user
        )
        upload = self._upload_side(item)
        existing = self._candidate_side(
            candidate=candidate, current_user=current_user, expected_batch_id=item.batch_id
        )
        snapshot_id = self._snapshot_id(
            item=item, review=review, candidate=candidate, group_revision=group_revision,
            upload=upload, existing=existing,
        )
        upload_projection = self._side_projection(upload)
        existing_projection = self._side_projection(existing)
        available = sum(
            side.preview_status == "AVAILABLE" or side.download_available
            for side in (upload_projection, existing_projection)
        )
        status = "READY" if available == 2 else "PARTIAL" if available else "UNAVAILABLE"
        return IngestDuplicateComparisonResponse(
            item_id=item.id,
            review_id=review.id,
            review_revision=review.revision,
            candidate_id=candidate.id,
            group_revision=group_revision,
            snapshot_id=snapshot_id,
            status=status,
            verdict=self._verdict(candidate=candidate, upload=upload, existing=existing),
            comparison_url=self._comparison_url(
                item_id=item.id, review=review, candidate=candidate, group_revision=group_revision
            ),
            upload=upload_projection,
            candidate=existing_projection,
        )

    def content(
        self,
        *,
        item_id: str,
        query: IngestDuplicateContentQuery,
        current_user: User,
    ) -> FileResponse:
        """下载固定候选侧的完整文件；每次请求都重新核验快照与字节事实。"""

        resolved, snapshot_id = self._resolve_for_side(
            item_id=item_id, query=query, current_user=current_user
        )
        if resolved.path is None:
            self._error(410, resolved.reason_code or "COMPARISON_CONTENT_GONE", "对比文件已经不可读取。")
        if query.disposition == "inline" and not self._inline_supported(resolved.content_type, resolved.filename):
            self._error(415, "COMPARISON_INLINE_UNSUPPORTED", "当前文件类型只能下载，不能内嵌预览。")
        self._verify_path(resolved)
        download_name = self._safe_download_name(query.side, resolved.filename)
        disposition = "inline" if query.disposition == "inline" else "attachment"
        return FileResponse(
            path=resolved.path,
            media_type=resolved.content_type,
            filename=download_name,
            content_disposition_type=disposition,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "X-Comparison-Snapshot": snapshot_id},
        )

    def preview(
        self,
        *,
        item_id: str,
        query: IngestDuplicatePreviewQuery,
        current_user: User,
    ) -> IngestDuplicatePreviewResponse:
        """读取同一固定版本已有正文页；预览从不隐式触发文件解析。"""

        resolved, snapshot_id = self._resolve_for_side(
            item_id=item_id, query=query, current_user=current_user
        )
        if resolved.version is None:
            self._error(409, "PREVIEW_NOT_AVAILABLE", "当前候选没有可复用的固定正文版本。")
        extraction = (
            self.db.query(DocumentExtractionRun)
            .filter(
                DocumentExtractionRun.document_id == resolved.version.document_id,
                DocumentExtractionRun.document_version_id == resolved.version.id,
                DocumentExtractionRun.status == "COMPLETED",
            )
            .order_by(DocumentExtractionRun.updated_at.desc())
            .first()
        )
        if extraction is None:
            self._error(409, "PREVIEW_NOT_AVAILABLE", "文件正文尚未完成解析，暂时无法预览。")
        remaining = query.max_chars
        sections: list[IngestDuplicatePreviewSection] = []
        truncated = False
        for page in (
            self.db.query(DocumentPage)
            .filter(DocumentPage.extraction_run_id == extraction.id)
            .order_by(DocumentPage.page_number.asc(), DocumentPage.id.asc())
        ):
            value = str(page.text_content or "")
            if not value:
                continue
            shown = value[:remaining]
            sections.append(IngestDuplicatePreviewSection(page_number=page.page_number, sheet_name=page.sheet_name, text=shown))
            remaining -= len(shown)
            if len(shown) < len(value) or remaining <= 0:
                truncated = True
                break
        return IngestDuplicatePreviewResponse(
            item_id=item_id, review_id=query.review_id, candidate_id=query.candidate_id,
            snapshot_id=snapshot_id, side=query.side, filename=resolved.filename,
            sections=sections, truncated=truncated,
        )

    def _resolve_for_side(self, *, item_id: str, query, current_user: User) -> tuple[_ResolvedSide, str]:
        """统一校验请求快照，防止 content 与 preview 在候选变化后读取新对象。"""

        comparison = self.comparison(
            item_id=item_id,
            query=IngestDuplicateComparisonQuery(
                review_id=query.review_id, review_revision=query.review_revision,
                candidate_id=query.candidate_id, group_revision=query.group_revision,
            ),
            current_user=current_user,
        )
        if comparison.snapshot_id != query.snapshot_id:
            self._error(409, "DUPLICATE_CANDIDATE_CHANGED", "重复候选已经变化，请重新查看后再操作。")
        item, _, candidate, _ = self._context(item_id=item_id, query=query, current_user=current_user)
        return (
            self._upload_side(item)
            if query.side == "UPLOAD"
            else self._candidate_side(
                candidate=candidate, current_user=current_user, expected_batch_id=item.batch_id
            ),
            comparison.snapshot_id,
        )

    def _context(self, *, item_id: str, query, current_user: User) -> tuple[IngestItem, UploadDuplicateReview, UploadDuplicateCandidate, int | None]:
        """按当前用户和冻结修订读取候选；此处不调用会改变 review 的同步逻辑。"""

        item = self.db.get(IngestItem, item_id)
        batch = self.db.get(IngestBatch, item.batch_id) if item else None
        if item is None or batch is None or batch.user_id != current_user.id or not item.upload_document_version_id:
            self._error(404, "INGEST_ITEM_NOT_FOUND", "导入条目不存在或无权访问。")
        review = self.db.query(UploadDuplicateReview).filter(UploadDuplicateReview.upload_document_version_id == item.upload_document_version_id).one_or_none()
        if review is None or review.user_id != current_user.id or review.id != query.review_id:
            self._error(404, "DUPLICATE_REVIEW_NOT_FOUND", "当前条目没有可访问的重复确认。")
        if review.status != "WAITING_CONFIRMATION":
            self._error(410, "DUPLICATE_REVIEW_CLOSED", "重复确认已经结束。")
        expires_at = review.expires_at.replace(tzinfo=timezone.utc) if review.expires_at.tzinfo is None else review.expires_at
        if expires_at < utcnow():
            self._error(410, "DUPLICATE_REVIEW_EXPIRED", "重复确认已经过期。")
        if review.revision != query.review_revision:
            self._error(409, "DUPLICATE_REVIEW_REVISION_CONFLICT", "重复确认已经变化，请重新查看。")
        candidate = self.db.get(UploadDuplicateCandidate, query.candidate_id)
        allowed_ids = set((review.decision_scope_json or {}).get("candidate_ids") or [])
        if candidate is None or candidate.duplicate_review_id != review.id or (allowed_ids and candidate.id not in allowed_ids):
            self._error(404, "DUPLICATE_CANDIDATE_NOT_FOUND", "重复候选不存在或无权访问。")
        group_revision = self._validate_group(review=review, item=item, requested=query.group_revision)
        return item, review, candidate, group_revision

    def _validate_group(self, *, review: UploadDuplicateReview, item: IngestItem, requested: int | None) -> int | None:
        """对同批重复使用当前真实成员修订，避免查看到被重新分组的暂存文件。"""

        group_id = str((review.decision_scope_json or {}).get("duplicate_group_id") or "")
        if not group_id:
            if requested is not None:
                self._error(422, "DUPLICATE_GROUP_NOT_APPLICABLE", "当前候选不属于同批重复组。")
            return None
        group = self.db.get(IngestDuplicateGroup, group_id)
        member = self.db.query(IngestDuplicateGroupMember).filter(IngestDuplicateGroupMember.group_id == group_id, IngestDuplicateGroupMember.ingest_item_id == item.id).one_or_none()
        if group is None or member is None or requested is None or group.revision != requested:
            self._error(409, "DUPLICATE_GROUP_REVISION_CONFLICT", "重复组成员已经变化，请重新查看。")
        return group.revision

    def _upload_side(self, item: IngestItem) -> _ResolvedSide:
        """解析本次上传的固定版本，不因临时文件清理回退到其他版本。"""

        version = self.db.get(DocumentVersion, item.upload_document_version_id)
        if version is None:
            return _ResolvedSide("UPLOAD", None, item.original_filename, "application/octet-stream", item.expected_size, None, None, "missing", "COMPARISON_CONTENT_GONE")
        return self._version_side("UPLOAD", version)

    def _candidate_side(
        self,
        *,
        candidate: UploadDuplicateCandidate,
        current_user: User,
        expected_batch_id: str,
    ) -> _ResolvedSide:
        """仅按候选保存的身份解析已有文件、同批暂存或受管源文件。"""

        if candidate.candidate_ingest_item_id:
            item = self.db.get(IngestItem, candidate.candidate_ingest_item_id)
            batch = self.db.get(IngestBatch, item.batch_id) if item else None
            if item is None or batch is None or batch.user_id != current_user.id or item.batch_id != expected_batch_id:
                self._error(404, "DUPLICATE_CANDIDATE_NOT_FOUND", "重复候选不存在或无权访问。")
            version = self.db.get(DocumentVersion, candidate.compared_version_id or item.upload_document_version_id)
            if version is None:
                return _ResolvedSide("SAME_BATCH_UPLOAD", None, item.original_filename, "application/octet-stream", item.expected_size, None, None, "missing", "COMPARISON_CONTENT_GONE")
            return self._version_side("SAME_BATCH_UPLOAD", version)
        if candidate.candidate_working_copy_id:
            copy = self.db.get(WorkingCopy, candidate.candidate_working_copy_id)
            if copy is None or copy.status != "ACTIVE" or copy.current_version_id != candidate.compared_version_id or (candidate.compared_working_copy_revision is not None and copy.revision != candidate.compared_working_copy_revision) or (candidate.compared_sha256 and copy.content_sha256 != candidate.compared_sha256):
                self._error(409, "DUPLICATE_CANDIDATE_CHANGED", "已有文件已经变化，请重新查看。")
            version = self.db.get(DocumentVersion, copy.current_version_id)
            if version is None:
                self._error(409, "DUPLICATE_CANDIDATE_CHANGED", "已有文件版本已经变化，请重新查看。")
            return self._version_side("WORKING_COPY", version, revision_marker=f"{copy.id}:{copy.revision}")
        if candidate.candidate_managed_file_id:
            managed = self.db.get(ManagedFile, candidate.candidate_managed_file_id)
            root = self.db.get(ManagedRoot, managed.root_id) if managed else None
            if managed is None or root is None or not root.enabled or managed.status != "ACTIVE":
                self._error(409, "DUPLICATE_CANDIDATE_CHANGED", "受管候选已经变化，请重新查看。")
            if candidate.compared_sha256 and managed.content_sha256 != candidate.compared_sha256:
                self._error(409, "DUPLICATE_CANDIDATE_CHANGED", "受管候选内容已经变化，请重新查看。")
            try:
                path = resolve_managed_relative_path(root_path=Path(root.container_path), relative_path=managed.relative_path)
            except PathPolicyError:
                return _ResolvedSide("MANAGED_SOURCE", None, managed.filename, infer_content_type(filename=managed.filename), managed.size_bytes, managed.content_sha256, None, "invalid", "COMPARISON_CONTENT_GONE")
            return _ResolvedSide("MANAGED_SOURCE", None, managed.filename, infer_content_type(filename=managed.filename), managed.size_bytes, managed.content_sha256, path if path.is_file() else None, f"{managed.id}:{managed.content_sha256 or ''}", None if path.is_file() else "COMPARISON_CONTENT_GONE")
        self._error(409, "DUPLICATE_CANDIDATE_CHANGED", "重复候选缺少可读取的固定对象。")

    def _version_side(self, source_kind: str, version: DocumentVersion, revision_marker: str | None = None) -> _ResolvedSide:
        """将 DocumentVersion 解析到对应受控存储根，禁止将 managed-source URI 当文件路径。"""

        try:
            if version.storage_tier == "UPLOAD":
                path = self.storage.upload_path(version.storage_path)
            elif version.storage_tier == "WORKING_COPY":
                path = self.storage.working_copy_path(version.storage_path)
            else:
                path = None
        except ValueError:
            path = None
        return _ResolvedSide(source_kind, version, version.filename, version.content_type, version.size_bytes, version.sha256, path if path and path.is_file() else None, revision_marker or version.id, None if path and path.is_file() else "COMPARISON_CONTENT_GONE")

    def _side_projection(self, side: _ResolvedSide) -> IngestDuplicateComparisonSide:
        """构造不包含服务器路径、哈希或来源用户的浏览器/MCP 元数据。"""

        supported = self._preview_mode(side.content_type, side.filename)
        available = side.path is not None
        return IngestDuplicateComparisonSide(filename=side.filename, source_kind=side.source_kind, size_bytes=side.size_bytes, content_type=side.content_type, preview_status="AVAILABLE" if available and supported != "NONE" else "UNAVAILABLE", preview_mode=supported, download_available=available, reason_code=side.reason_code)

    def _snapshot_id(self, *, item, review, candidate, group_revision, upload, existing) -> str:
        """计算不泄漏原始哈希的固定事实指纹，内容或修订变化时必然失效。"""

        payload = {"v": 1, "item": item.id, "review": review.id, "revision": review.revision, "candidate": candidate.id, "group": group_revision, "upload": [upload.version.id if upload.version else None, upload.expected_sha256, upload.size_bytes, upload.revision_marker], "candidate_side": [existing.version.id if existing.version else None, existing.expected_sha256, existing.size_bytes, existing.revision_marker]}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _comparison_url(self, *, item_id: str, review: UploadDuplicateReview, candidate: UploadDuplicateCandidate, group_revision: int | None) -> str | None:
        """仅以部署配置构造浏览器链接，URL 不携带令牌、路径、文件名或哈希。"""

        base = get_settings().integration_review_web_base_url
        if not base:
            return None
        params = {"item_id": item_id, "review_id": review.id, "review_revision": review.revision, "candidate_id": candidate.id}
        if group_revision is not None:
            params["group_revision"] = group_revision
        return f"{base}/duplicate-comparison?{urlencode(params)}"

    @staticmethod
    def _verdict(*, candidate: UploadDuplicateCandidate, upload: _ResolvedSide, existing: _ResolvedSide) -> str:
        """只在可靠哈希和大小同时一致时声明字节完全一致。"""

        if candidate.match_type in {"EXACT_SHA256", "EXACT_HASH"} and upload.expected_sha256 and upload.expected_sha256 == existing.expected_sha256 and upload.size_bytes == existing.size_bytes:
            return "EXACT_CONTENT"
        if candidate.match_type == "NEAR_DUPLICATE":
            return "SIMILAR_CONTENT"
        if candidate.match_type == "SAME_FILENAME":
            return "SAME_NAME"
        return "UNKNOWN"

    @staticmethod
    def _preview_mode(content_type: str, filename: str) -> str:
        """返回现有浏览器渲染器可安全处理的格式，不触发新的解析流程。"""

        suffix = Path(filename).suffix.lower()
        if content_type.startswith("image/"):
            return "IMAGE"
        if content_type == "application/pdf" or suffix == ".pdf":
            return "PDF"
        if suffix in {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".log"} or content_type.startswith("text/"):
            return "TEXT"
        if suffix == ".docx":
            return "DOCX"
        if suffix == ".xlsx":
            return "XLSX"
        return "SECTIONS"

    @staticmethod
    def _inline_supported(content_type: str, filename: str) -> bool:
        """限制 inline 为现有安全浏览器预览格式，避免上传 HTML/SVG 作为应用页面执行。"""

        suffix = Path(filename).suffix.lower()
        return content_type.startswith("image/") and suffix != ".svg" or content_type == "application/pdf" or suffix == ".pdf"

    def _verify_path(self, side: _ResolvedSide) -> None:
        """在发送前核对实际大小和哈希，避免按数据库字段读取被替换的同名文件。"""

        if side.path is None or not side.path.is_file():
            self._error(410, "COMPARISON_CONTENT_GONE", "对比文件已经不可读取。")
        if side.path.stat().st_size != side.size_bytes:
            self._error(409, "DUPLICATE_CANDIDATE_CHANGED", "文件大小已经变化，请重新查看。")
        if side.expected_sha256 and self.storage.sha256_file(side.path) != side.expected_sha256:
            self._error(409, "DUPLICATE_CANDIDATE_CHANGED", "文件内容已经变化，请重新查看。")

    @staticmethod
    def _safe_download_name(side: str, filename: str) -> str:
        """生成只影响浏览器保存名称的安全区分前缀，绝不修改服务器文件名。"""

        value = Path(filename).name.replace("\r", "_").replace("\n", "_") or "file"
        return f"{'本次上传' if side == 'UPLOAD' else '候选'}_{value}"

    @staticmethod
    def _error(status_code: int, code: str, message: str) -> None:
        """统一抛出集成 API 错误，调用方不得把内部异常或路径暴露给客户端。"""

        raise HTTPException(status_code=status_code, detail={"code": code, "message": message})
