"""工作副本首次创建前的自动整理决策。

本模块只生成最终文件名、主分类目录和轻量审计结果，不执行文件系统写入。LLM 不能返回
目标路径；目录始终由后端根据固定 taxonomy 候选和安全组件规则构造。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Document, DocumentVersion, ManagedFile
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.file_lifecycle.storage import FileLifecycleStorageService


@dataclass(slots=True)
class InitialOrganizationDecision:
    """一次首次工作副本整理的确定性输出。"""

    filename: str
    relative_path: str
    extraction_result: dict[str, Any] | None
    categories: list[dict[str, Any]]
    primary_category: dict[str, Any] | None
    document_summary_id: str | None
    classification_summary_id: str | None
    summary_status: str
    rename_status: str

    def document_result(
        self,
        *,
        document_id: str,
        document_version_id: str,
    ) -> dict[str, Any]:
        """转换为现有分类持久化和逐文件审计可消费的轻量结构。"""

        extraction = self.extraction_result or {}
        return {
            "document_id": document_id,
            "document_version_id": document_version_id,
            "filename": self.filename,
            "extraction_status": extraction.get("status") or "FAILED",
            "extraction_run_id": extraction.get("extraction_run_id"),
            "extractor": extraction.get("extractor"),
            "categories": self.categories,
            "document_summary_id": self.document_summary_id,
            "classification_summary_id": self.classification_summary_id,
            "summary_status": self.summary_status,
            "source": "initial-working-copy-organization",
            "warnings": list(extraction.get("warnings") or []),
            "errors": [extraction.get("error")] if extraction.get("error") else [],
        }


class InitialWorkingCopyOrganizer:
    """在工作副本正式落位前完成解析、双摘要、分类和首次命名。"""

    def __init__(self, *, db: Session, user_id: str, settings: Settings | None = None) -> None:
        """保存 worker 级数据库会话和确定用户边界。"""

        self.db = db
        self.user_id = user_id
        self.settings = settings or get_settings()

    def decide(
        self,
        *,
        document: Document,
        version: DocumentVersion,
        managed_file: ManagedFile,
    ) -> InitialOrganizationDecision:
        """生成最终工作副本路径；任何失败都降级到内部待整理目录。"""

        if not self.settings.initial_working_copy_organization_enabled:
            filename = FileLifecycleStorageService.sanitize_filename(managed_file.filename)
            return InitialOrganizationDecision(
                filename=filename,
                relative_path=_pending_path(managed_file_id=managed_file.id, filename=filename),
                extraction_result=None,
                categories=[],
                primary_category=None,
                document_summary_id=None,
                classification_summary_id=None,
                summary_status="DISABLED",
                rename_status="DISABLED",
            )

        # 延迟导入避免重命名 OperationPlan 服务反向引用生命周期审计造成模块循环。
        from app.modules.file_rename.uploaded_suggestion_service import UploadedRenameSuggestionService

        rename_suggestion, extraction_result = UploadedRenameSuggestionService(
            db=self.db,
            user_id=self.user_id,
        ).suggest_for_initial_import(document=document)
        filename = _resolved_filename(
            original_filename=managed_file.filename,
            suggestion=rename_suggestion,
        )
        classification_result: dict[str, Any] = {}
        if extraction_result and extraction_result.get("status") == "COMPLETED":
            try:
                classification_result = DocumentClassificationService(db=self.db).classify(
                    document_id=document.id,
                    document_version_id=version.id,
                    extraction_run_id=str(extraction_result.get("extraction_run_id") or ""),
                    filename=filename,
                    force_reprocess=False,
                )
            except Exception:
                # 自动整理属于体验增强；分类异常不能阻止不可变原始文件生成可用工作副本。
                classification_result = {"categories": [], "summary_status": "FAILED"}
        categories = [item for item in classification_result.get("categories", []) if isinstance(item, dict)]
        primary = _select_primary_category(
            categories=categories,
            minimum_confidence=self.settings.initial_organization_confidence,
        )
        relative_path = (
            _classified_path(category=primary, filename=filename)
            if primary is not None
            else _pending_path(managed_file_id=managed_file.id, filename=filename)
        )
        return InitialOrganizationDecision(
            filename=filename,
            relative_path=relative_path,
            extraction_result=extraction_result,
            categories=categories,
            primary_category=primary,
            document_summary_id=classification_result.get("document_summary_id"),
            classification_summary_id=classification_result.get("classification_summary_id"),
            summary_status=str(classification_result.get("summary_status") or "FULL_TEXT_FALLBACK"),
            rename_status=str(rename_suggestion.get("status") or "FAILED"),
        )


def _resolved_filename(*, original_filename: str, suggestion: dict[str, Any]) -> str:
    """只接受通过质量门禁的 basename，并始终保留原扩展名。"""

    proposed = str(suggestion.get("proposed_filename") or "")
    if suggestion.get("status") not in {"READY", "NO_CHANGE"} or not proposed:
        return FileLifecycleStorageService.sanitize_filename(original_filename)
    if Path(proposed).name != proposed or Path(proposed).suffix.lower() != Path(original_filename).suffix.lower():
        return FileLifecycleStorageService.sanitize_filename(original_filename)
    return FileLifecycleStorageService.sanitize_filename(proposed)


def _select_primary_category(
    *,
    categories: list[dict[str, Any]],
    minimum_confidence: float,
) -> dict[str, Any] | None:
    """只选择有原文证据的高置信度固定 taxonomy 分类作为物理主目录。"""

    for category in categories:
        if category.get("name") == "其他" or category.get("source") == "llm_free_path":
            continue
        if str(category.get("status") or "") == "NEEDS_REVIEW":
            continue
        if float(category.get("confidence") or 0) < minimum_confidence:
            continue
        if not category.get("evidence_items"):
            continue
        return category
    return None


def _classified_path(*, category: dict[str, Any], filename: str) -> str:
    """根据后端已校验 taxonomy 路径构造工作副本目录，拒绝任意模型路径。"""

    raw_path = category.get("category_path") or [category.get("name") or "其他"]
    segments: list[str] = []
    for item in raw_path:
        for part in re.split(r"[/\\]+", str(item)):
            safe = _safe_directory_component(part)
            if safe:
                segments.append(safe)
    if not segments:
        return _pending_path(managed_file_id=str(category.get("category_id") or "unknown"), filename=filename)
    return PurePosixPath(*segments, filename).as_posix()


def _pending_path(*, managed_file_id: str, filename: str) -> str:
    """无法可靠分类时使用稳定内部目录，不把低置信度结果伪装成正式分类。"""

    return PurePosixPath("待整理", managed_file_id, filename).as_posix()


def _safe_directory_component(value: str) -> str:
    """清理 taxonomy 显示名中的路径控制字符。"""

    cleaned = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", value).strip(" .")
    return cleaned[:120]
