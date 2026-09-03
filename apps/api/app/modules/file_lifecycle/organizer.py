"""工作副本异步分析阶段的自动整理决策。

本模块只生成命名建议、分类建议和轻量审计结果，不执行文件系统写入。分类是逻辑标签，
不得由 LLM 或本模块生成物理目标路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Document, DocumentClassificationSummary, DocumentVersion, ManagedFile
from app.modules.classification.runtime_factory import ClassificationRuntimeFactory
from app.modules.file_lifecycle.storage import FileLifecycleStorageService


@dataclass(slots=True)
class InitialOrganizationDecision:
    """一次首次工作副本整理的确定性输出。"""

    filename: str
    extraction_result: dict[str, Any] | None
    categories: list[dict[str, Any]]
    primary_category: dict[str, Any] | None
    document_summary_id: str | None
    classification_summary_id: str | None
    summary_status: str
    rename_status: str
    rename_metadata: dict[str, Any]
    summary_metadata: dict[str, Any]

    def document_result(
        self,
        *,
        document_id: str,
        document_version_id: str,
    ) -> dict[str, Any]:
        """转换为现有分类持久化和逐文件审计可消费的轻量结构。"""

        extraction = self.extraction_result or {}
        proposed_filename = str(self.rename_metadata.get("proposed_filename") or "").strip()
        rename_completed = bool(
            self.rename_status == "READY"
            and proposed_filename
            and proposed_filename != self.filename
        )
        return {
            "document_id": document_id,
            "document_version_id": document_version_id,
            "filename": proposed_filename if rename_completed else self.filename,
            "original_filename": self.filename,
            "renamed_filename": proposed_filename if rename_completed else self.filename,
            "rename_status": (
                "COMPLETED"
                if rename_completed
                else "NO_CHANGE"
                if self.rename_status == "NO_CHANGE"
                else "NEEDS_REVIEW"
            ),
            "processing_status": (
                "COMPLETED"
                if extraction.get("status") == "COMPLETED"
                else "FAILED"
            ),
            "extraction_status": extraction.get("status") or "FAILED",
            "extraction_run_id": extraction.get("extraction_run_id"),
            "extractor": extraction.get("extractor"),
            "categories": self.categories,
            "document_summary_id": self.document_summary_id,
            "classification_summary_id": self.classification_summary_id,
            "summary_status": self.summary_status,
            "year": self.rename_metadata.get("year"),
            # 自动改名后的确定结果使用独立字段返回；兼容字段不再表达“尚未执行”。
            "rename_suggestion": None,
            "document_type": self.summary_metadata.get("document_type"),
            "keywords": list(self.summary_metadata.get("keywords") or []),
            "entities": list(self.summary_metadata.get("entities") or []),
            "source": "initial-working-copy-organization",
            "warnings": list(extraction.get("warnings") or []),
            "errors": [extraction.get("error")] if extraction.get("error") else [],
        }


class InitialWorkingCopyOrganizer:
    """为首次工作副本发布生成解析、分类和标准化命名决策。

    本服务只生成结构化决策；文件系统发布仍由生命周期服务集中执行。可信名称会用于
    上传后的首次工作副本发布，低可信名称保留原名并进入待复核。
    """

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
        """生成不直接写文件系统的分类和命名分析结果。"""

        if not self.settings.initial_working_copy_organization_enabled:
            filename = FileLifecycleStorageService.sanitize_filename(managed_file.filename)
            return InitialOrganizationDecision(
                filename=filename,
                extraction_result=None,
                categories=[],
                primary_category=None,
                document_summary_id=None,
                classification_summary_id=None,
                summary_status="DISABLED",
                rename_status="DISABLED",
                rename_metadata={},
                summary_metadata={},
            )

        # 延迟导入避免重命名 OperationPlan 服务反向引用生命周期审计造成模块循环。
        from app.modules.file_rename.uploaded_suggestion_service import UploadedRenameSuggestionService

        rename_suggestion, extraction_result = UploadedRenameSuggestionService(
            db=self.db,
            user_id=self.user_id,
        ).suggest_for_initial_import(document=document)
        # 高置信度建议也不能替代用户确认。这里必须固定为原上传名，防止正文中偶然
        # 出现的年份或标题（例如表格历史条目）直接改变用户实际可见的工作副本名称。
        filename = FileLifecycleStorageService.sanitize_filename(managed_file.filename)
        classification_result: dict[str, Any] = {}
        if extraction_result and extraction_result.get("status") == "COMPLETED":
            try:
                classification_result = ClassificationRuntimeFactory(self.settings).create(
                    db=self.db,
                    user_id=self.user_id,
                ).classify(
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
        return InitialOrganizationDecision(
            filename=filename,
            extraction_result=extraction_result,
            categories=categories,
            primary_category=primary,
            document_summary_id=classification_result.get("document_summary_id"),
            classification_summary_id=classification_result.get("classification_summary_id"),
            summary_status=str(classification_result.get("summary_status") or "FULL_TEXT_FALLBACK"),
            rename_status=str(rename_suggestion.get("status") or "FAILED"),
            rename_metadata=rename_metadata_for_initial_organization(rename_suggestion),
            summary_metadata=_summary_metadata(
                db=self.db,
                classification_summary_id=classification_result.get("classification_summary_id"),
            ),
        )


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


def rename_metadata_for_initial_organization(suggestion: dict[str, Any]) -> dict[str, Any]:
    """提取用户回执需要的命名依据，文种不参与文件名模板。"""

    def field_value(key: str) -> str | None:
        value = suggestion.get(key)
        if not isinstance(value, dict):
            return None
        resolved = str(value.get("value") or "").strip()
        return resolved or None

    return {
        "document_date": field_value("document_date"),
        "year": field_value("year"),
        "document_number": field_value("document_number"),
        "title": field_value("title"),
        "proposed_filename": suggestion.get("proposed_filename"),
        "template_key": suggestion.get("template_key"),
        "resume_name": suggestion.get("resume_name"),
        "warnings": list(suggestion.get("warnings") or []),
        "errors": list(suggestion.get("errors") or []),
    }


def _summary_metadata(
    *,
    db: Session,
    classification_summary_id: str | None,
) -> dict[str, Any]:
    """从持久化分类摘要提取少量元数据，正文和完整摘要不得进入运行回执。"""

    if not classification_summary_id:
        return {}
    summary = db.get(DocumentClassificationSummary, classification_summary_id)
    if summary is None:
        return {}
    payload = dict(summary.summary_json or {})
    entities = [
        *[str(item) for item in payload.get("subjects", []) if item],
        *[str(item) for item in payload.get("organizations", []) if item],
    ]
    return {
        "document_type": str(payload.get("document_type") or "") or None,
        "keywords": [str(item) for item in payload.get("keywords", []) if item][:8],
        "entities": list(dict.fromkeys(entities))[:20],
    }
