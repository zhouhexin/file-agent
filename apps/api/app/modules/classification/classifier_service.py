"""基于持久化正文的文档分类服务。"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import log_event
from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentPage,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.managed_catalog import GlobalManagedCategoryCatalogService
from app.modules.classification.managed_path import match_global_managed_categories
from app.modules.classification.matcher import match_document_text
from app.modules.knowledge_graph.candidate_retriever import retrieve_graph_candidates
from app.modules.knowledge_graph.classification_context import NoOpGraphClassificationContext
from app.modules.knowledge_graph.managed_path_profile import ManagedPathProfileRegistry
from app.modules.knowledge_graph.reranker import GraphClassificationReranker
from app.modules.knowledge_graph.schemas import GraphClassificationResult, GraphSemanticResult
from app.modules.knowledge_graph.semantic_context import NoOpSemanticClassificationContext


class DocumentClassificationService:
    """从 DocumentPage 读取全文并生成 rule-only 分类建议。"""

    def __init__(
        self,
        db: Session | None = None,
        llm_judge: Any = None,
        mode: str = "rule_only",
        graph_context: Any = None,
        graph_top_k: int = 8,
        graph_mode: str = "enabled",
        semantic_context: Any = None,
        managed_catalog_service: GlobalManagedCategoryCatalogService | None = None,
    ) -> None:
        """保存请求级数据库会话；无数据库时仅支持 fallback_text。"""

        self.db = db
        self.llm_judge = llm_judge
        self.mode = mode
        self.graph_context = graph_context or NoOpGraphClassificationContext()
        self.graph_top_k = max(1, min(20, graph_top_k))
        self.graph_mode = graph_mode if graph_mode in {"off", "shadow", "enabled"} else "off"
        self.semantic_context = semantic_context or NoOpSemanticClassificationContext()
        self.graph_reranker = GraphClassificationReranker()
        self.managed_catalog_service = managed_catalog_service or self._default_managed_catalog_service()

    def classify(
        self,
        *,
        document_id: str,
        extraction_run_id: str,
        filename: str = "",
        fallback_text: str = "",
        force_reprocess: bool = False,
    ) -> dict[str, Any]:
        """读取完整页面正文并返回分类结果。

        `fallback_text` 只用于无数据库的内存态测试或异常兜底；生产路径应读取
        `document_pages.text_content`，避免 Graph State 保存全文。
        """

        start = time.perf_counter()
        try:
            catalog = (
                self.managed_catalog_service.load()
                if self.managed_catalog_service is not None
                else None
            )
            taxonomy_key, taxonomy_version = self._taxonomy_identity(catalog=catalog)
            if not force_reprocess:
                cached_categories = self._load_cached_categories(
                    document_id=document_id,
                    taxonomy_key=taxonomy_key,
                    taxonomy_version=taxonomy_version,
                )
                if cached_categories:
                    return {
                        "status": "COMPLETED",
                        "document_id": document_id,
                        "extraction_run_id": extraction_run_id,
                        "categories": cached_categories,
                        "text_source": "classification_cache",
                        "graph_status": "REUSED",
                        "graph_warnings": [],
                        "semantic_status": "REUSED",
                        "semantic_warnings": [],
                        "graph_mode": self.graph_mode,
                        "classification_reused": True,
                        "classifier_version": self.classifier_version,
                    }
            pages = self._load_pages(extraction_run_id=extraction_run_id)
            full_text = "\n".join(page.text_content for page in pages if page.text_content)
            classification_text = full_text or fallback_text
            base_categories = self._classify_with_available_taxonomy(
                filename=filename,
                classification_text=classification_text,
                catalog=catalog,
            )
            graph_result = self._load_graph_candidates(
                document_id=document_id,
                categories=base_categories,
            )
            semantic_result = self._load_semantic_candidates(
                document_id=document_id,
                filename=filename,
                classification_text=classification_text,
            )
            enhanced_categories = self.graph_reranker.rerank(
                categories=base_categories,
                graph_result=graph_result,
                semantic_result=semantic_result,
                limit=self.graph_top_k,
            )
            if self.graph_mode == "shadow":
                categories = base_categories
                self._log_shadow_comparison(
                    document_id=document_id,
                    base_categories=base_categories,
                    enhanced_categories=enhanced_categories,
                )
            elif self.graph_mode == "enabled":
                categories = enhanced_categories
            else:
                categories = base_categories
            categories = self._judge_categories(
                filename=filename,
                classification_text=classification_text,
                rule_categories=categories,
            )
            categories = [
                self._attach_evidence_items(
                    category={
                        **category,
                        "classifier_version": self.classifier_version,
                    },
                    pages=pages,
                    fallback_text=fallback_text,
                )
                for category in categories
            ]
        except Exception as exc:
            log_event(
                "classification.failed",
                level="ERROR",
                document_id=document_id or None,
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=exc.__class__.__name__,
                message=str(exc),
            )
            raise

        log_event(
            "classification.completed",
            document_id=document_id or None,
            status="COMPLETED",
            duration_ms=int((time.perf_counter() - start) * 1000),
            message="文档分类完成",
            category_count=len(categories),
            extraction_run_id=extraction_run_id,
        )
        return {
            "status": "COMPLETED",
            "document_id": document_id,
            "extraction_run_id": extraction_run_id,
            "categories": categories,
            "text_source": "document_pages" if full_text else "fallback",
            "graph_status": graph_result.status,
            "graph_warnings": [] if graph_result.status == "DISABLED" else list(graph_result.warnings),
            "semantic_status": semantic_result.status,
            "semantic_warnings": (
                [] if semantic_result.status == "DISABLED" else list(semantic_result.warnings)
            ),
            "graph_mode": self.graph_mode,
            "classification_reused": False,
            "classifier_version": self.classifier_version,
        }

    def _load_semantic_candidates(
        self,
        *,
        document_id: str,
        filename: str,
        classification_text: str,
    ) -> GraphSemanticResult:
        """在运行时把完整正文交给 Embedding 服务，不写入图状态。"""

        if self.graph_mode == "off":
            return GraphSemanticResult(status="DISABLED", warnings=["GRAPH_MODE_OFF"])
        document = self.db.get(Document, document_id) if self.db is not None and document_id else None
        sha256 = (
            str(document.sha256)
            if document is not None
            else hashlib.sha256(classification_text.encode("utf-8")).hexdigest()
        )
        return self.semantic_context.retrieve(
            document_id=document_id,
            document_version_id=document_id,
            sha256=sha256,
            filename=filename,
            full_text=classification_text,
            limit=self.graph_top_k,
        )

    def _log_shadow_comparison(
        self,
        *,
        document_id: str,
        base_categories: list[dict[str, Any]],
        enhanced_categories: list[dict[str, Any]],
    ) -> None:
        """记录候选 ID 排名差异，不记录正文和来源文件身份。"""

        log_event(
            "classification.graph_shadow.compared",
            document_id=document_id or None,
            status="COMPLETED",
            message="图谱分类 Shadow 对照完成",
            base_category_ids=_category_ids(base_categories),
            enhanced_category_ids=_category_ids(enhanced_categories),
        )

    def _load_graph_candidates(
        self,
        *,
        document_id: str,
        categories: list[dict[str, Any]],
    ) -> GraphClassificationResult:
        """加载只包含候选 ID 的图谱上下文，异常时关闭式降级。"""

        if self.graph_mode == "off":
            return GraphClassificationResult(status="DISABLED", warnings=["GRAPH_MODE_OFF"])
        start = time.perf_counter()
        try:
            result = retrieve_graph_candidates(
                context=self.graph_context,
                categories=categories,
                document_id=document_id,
                # 当前 Document 内容不可变且尚无独立版本表，第一版本以 document_id 兼容版本键。
                document_version_id=document_id or None,
                limit=self.graph_top_k,
            )
        except Exception as exc:
            log_event(
                "classification.graph_query.degraded",
                level="WARNING",
                document_id=document_id or None,
                status="DEGRADED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=exc.__class__.__name__,
                message="图谱分类查询失败，已回退基础分类。",
            )
            return GraphClassificationResult(status="DEGRADED", warnings=["GRAPH_UNAVAILABLE"])
        if result.status == "COMPLETED" and result.candidates:
            log_event(
                "classification.graph_rerank.completed",
                document_id=document_id or None,
                status="COMPLETED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                message="图谱分类候选重排完成",
                candidate_count=len(categories),
                graph_candidate_count=len(result.candidates),
            )
        return result

    def _classify_with_available_taxonomy(
        self,
        *,
        filename: str,
        classification_text: str,
        catalog=None,
    ) -> list[dict[str, Any]]:
        """配置受管分类来源后统一使用全局目录，不静默混入预置业务分类。"""

        if catalog is not None and catalog.configured:
            return match_global_managed_categories(
                filename=filename,
                text=classification_text,
                catalog=catalog,
            )
        taxonomy = load_default_taxonomy()
        return match_document_text(text=f"{filename}\n{classification_text}", taxonomy=taxonomy)

    @property
    def classifier_version(self) -> str:
        """返回会影响分类结果的受控实现版本。"""

        return f"taxonomy-{self.mode}-graph-{self.graph_mode}-v2"

    def _taxonomy_identity(self, *, catalog) -> tuple[str, str]:
        """解析本次分类使用的唯一 taxonomy 身份。"""

        if catalog is not None and catalog.configured:
            return catalog.taxonomy_key, catalog.taxonomy_version
        taxonomy = load_default_taxonomy()
        return taxonomy.key, taxonomy.version

    def _load_cached_categories(
        self,
        *,
        document_id: str,
        taxonomy_key: str,
        taxonomy_version: str,
    ) -> list[dict[str, Any]]:
        """读取同文件、同目录版本和同分类器版本的最近成功建议。"""

        if self.db is None or not document_id:
            return []
        run = (
            self.db.query(DocumentClassificationRun)
            .filter(DocumentClassificationRun.document_id == document_id)
            .filter(DocumentClassificationRun.taxonomy_key == taxonomy_key)
            .filter(DocumentClassificationRun.taxonomy_version == taxonomy_version)
            .filter(DocumentClassificationRun.classifier_version == self.classifier_version)
            .filter(DocumentClassificationRun.status == "COMPLETED")
            .order_by(DocumentClassificationRun.created_at.desc())
            .first()
        )
        if run is None:
            return []
        suggestions = (
            self.db.query(DocumentCategorySuggestion)
            .filter(DocumentCategorySuggestion.classification_run_id == run.id)
            .order_by(
                DocumentCategorySuggestion.rank.asc(),
                DocumentCategorySuggestion.confidence.desc(),
            )
            .all()
        )
        return [
            {
                "name": suggestion.category_name,
                "category_id": suggestion.category_id,
                "category_path": list(suggestion.category_path_json or []),
                "confidence": float(suggestion.confidence or 0),
                "status": suggestion.status,
                "source": suggestion.source,
                "evidence_items": list(suggestion.evidence_json or []),
                "evidence": _evidence_signals(suggestion.evidence_json),
                "candidate_scores": dict(suggestion.candidate_scores_json or {}),
                "semantic_evidence": dict(suggestion.semantic_evidence_json or {}),
                "taxonomy_key": suggestion.taxonomy_key,
                "taxonomy_version": suggestion.taxonomy_version,
                "classifier_version": run.classifier_version,
                "reused_from_suggestion_id": suggestion.id,
            }
            for suggestion in suggestions
        ]

    def _default_managed_catalog_service(self) -> GlobalManagedCategoryCatalogService | None:
        """构造请求级全局受管目录服务；无数据库时继续使用预置 taxonomy。"""

        if self.db is None:
            return None
        settings = get_settings()
        return GlobalManagedCategoryCatalogService(
            db=self.db,
            profile_registry=ManagedPathProfileRegistry.load(
                settings.managed_path_classification_profile_dir
            ),
        )

    def _load_pages(self, *, extraction_run_id: str) -> list[DocumentPage]:
        """按解析运行读取完整页面正文。"""

        if self.db is None or not extraction_run_id:
            return []
        return (
            self.db.query(DocumentPage)
            .filter(DocumentPage.extraction_run_id == extraction_run_id)
            .order_by(
                DocumentPage.page_number.asc().nullslast(),
                DocumentPage.created_at.asc(),
            )
            .all()
        )

    def _attach_evidence_items(
        self,
        *,
        category: dict[str, Any],
        pages: list[DocumentPage],
        fallback_text: str,
    ) -> dict[str, Any]:
        """为分类建议补充可定位原文证据。"""

        if category.get("name") == "其他":
            return {**category, "evidence_items": []}
        existing_items = [item for item in category.get("evidence_items", []) if isinstance(item, dict)]
        if existing_items:
            return {
                **category,
                "evidence_items": [
                    _locate_existing_evidence_item(item=item, pages=pages)
                    for item in existing_items
                ],
            }

        signals = [str(item) for item in category.get("evidence", []) if item]
        evidence_item = _find_text_quote(
            signals=signals,
            pages=pages,
            fallback_text=fallback_text,
            source=str(category.get("source") or "rule"),
        )
        if evidence_item is None:
            return {**category, "status": "NEEDS_REVIEW", "evidence_items": []}
        return {**category, "evidence_items": [evidence_item]}

    def _judge_categories(
        self,
        *,
        filename: str,
        classification_text: str,
        rule_categories: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按配置选择 rule-only、hybrid 或 review-only 分类模式。"""

        if self.llm_judge is None or self.mode == "rule_only":
            return rule_categories
        if self.mode == "review_only" and not _needs_llm_review(rule_categories):
            return rule_categories
        if self.mode not in {"hybrid", "review_only"}:
            return rule_categories
        judged_categories = self.llm_judge.judge(
            filename=filename,
            document_text=classification_text,
            candidates=[category for category in rule_categories if category.get("name") != "其他"],
        )
        return judged_categories or rule_categories


def _find_text_quote(
    *,
    signals: list[str],
    pages: list[DocumentPage],
    fallback_text: str,
    source: str = "rule",
) -> dict[str, Any] | None:
    """从页面正文中定位第一个能支撑分类的证据片段。"""

    for signal in signals:
        for page in pages:
            quote = _quote_around_signal(text=page.text_content, signal=signal)
            if quote:
                return {
                    "type": "text_quote",
                    "page_number": page.page_number,
                    "sheet_name": page.sheet_name,
                    "quote": quote,
                    "signals": [signal],
                    "source": source,
                }
        quote = _quote_around_signal(text=fallback_text, signal=signal)
        if quote:
            return {
                "type": "text_quote",
                "page_number": None,
                "sheet_name": None,
                "quote": quote,
                "signals": [signal],
                "source": source,
            }
    return None


def _evidence_signals(evidence_items: list | None) -> list[str]:
    """从持久化定位证据恢复兼容 UI 使用的信号词。"""

    signals: list[str] = []
    for item in evidence_items or []:
        if not isinstance(item, dict):
            continue
        signals.extend(str(signal) for signal in item.get("signals", []) if signal)
    return list(dict.fromkeys(signals))


def _category_ids(categories: list[dict[str, Any]], limit: int = 5) -> list[str]:
    """提取 Shadow 日志使用的候选分类 ID。"""

    result: list[str] = []
    for category in categories[:limit]:
        category_id = str(category.get("category_id") or "")
        if category_id:
            result.append(category_id)
    return result


def _quote_around_signal(*, text: str, signal: str) -> str:
    """截取包含信号词的短原文片段。"""

    if not text or not signal:
        return ""
    index = text.find(signal)
    if index < 0:
        return ""
    start = max(0, index - 24)
    end = min(len(text), index + len(signal) + 24)
    return text[start:end].strip()


def _locate_existing_evidence_item(*, item: dict[str, Any], pages: list[DocumentPage]) -> dict[str, Any]:
    """把已有 quote 反查到页码或 Sheet。"""

    quote = str(item.get("quote") or "")
    if not quote:
        return item
    for page in pages:
        if quote in page.text_content:
            return {**item, "page_number": page.page_number, "sheet_name": page.sheet_name}
    return item


def _needs_llm_review(categories: list[dict[str, Any]]) -> bool:
    """判断规则结果是否需要 LLM 复核。"""

    if not categories:
        return True
    if categories[0].get("name") == "其他":
        return True
    return any(float(category.get("confidence") or 0) < 0.7 for category in categories[:3])
