"""基于持久化正文的文档分类服务。"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import DocumentPage
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.managed_path import match_managed_path_categories
from app.modules.classification.matcher import match_document_text
from app.modules.managed_files.repository import ManagedFileRepository
from app.modules.knowledge_graph.candidate_retriever import retrieve_graph_candidates
from app.modules.knowledge_graph.classification_context import NoOpGraphClassificationContext
from app.modules.knowledge_graph.reranker import GraphClassificationReranker
from app.modules.knowledge_graph.schemas import GraphClassificationResult


class DocumentClassificationService:
    """从 DocumentPage 读取全文并生成 rule-only 分类建议。"""

    def __init__(
        self,
        db: Session | None = None,
        llm_judge: Any = None,
        mode: str = "rule_only",
        graph_context: Any = None,
        graph_top_k: int = 8,
    ) -> None:
        """保存请求级数据库会话；无数据库时仅支持 fallback_text。"""

        self.db = db
        self.llm_judge = llm_judge
        self.mode = mode
        self.graph_context = graph_context or NoOpGraphClassificationContext()
        self.graph_top_k = max(1, min(20, graph_top_k))
        self.graph_reranker = GraphClassificationReranker()

    def classify(
        self,
        *,
        document_id: str,
        extraction_run_id: str,
        filename: str = "",
        fallback_text: str = "",
    ) -> dict[str, Any]:
        """读取完整页面正文并返回分类结果。

        `fallback_text` 只用于无数据库的内存态测试或异常兜底；生产路径应读取
        `document_pages.text_content`，避免 Graph State 保存全文。
        """

        start = time.perf_counter()
        try:
            pages = self._load_pages(extraction_run_id=extraction_run_id)
            full_text = "\n".join(page.text_content for page in pages if page.text_content)
            classification_text = full_text or fallback_text
            categories = self._classify_with_available_taxonomy(
                filename=filename,
                classification_text=classification_text,
            )
            graph_result = self._load_graph_candidates(
                document_id=document_id,
                categories=categories,
            )
            categories = self.graph_reranker.rerank(
                categories=categories,
                graph_result=graph_result,
                limit=self.graph_top_k,
            )
            categories = self._judge_categories(
                filename=filename,
                classification_text=classification_text,
                rule_categories=categories,
            )
            categories = [
                self._attach_evidence_items(
                    category=category,
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
        }

    def _load_graph_candidates(
        self,
        *,
        document_id: str,
        categories: list[dict[str, Any]],
    ) -> GraphClassificationResult:
        """加载只包含候选 ID 的图谱上下文，异常时关闭式降级。"""

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
    ) -> list[dict[str, Any]]:
        """优先使用受管目录子目录分类，缺失时回退到预置 taxonomy。"""

        managed_rows = self._load_managed_path_categories()
        if managed_rows:
            return match_managed_path_categories(
                filename=filename,
                text=classification_text,
                category_rows=managed_rows,
            )
        taxonomy = load_default_taxonomy()
        return match_document_text(text=f"{filename}\n{classification_text}", taxonomy=taxonomy)

    def _load_managed_path_categories(self) -> list[tuple[str, str, str, int]]:
        """读取 `PATH_AS_CATEGORY` 受管目录中的动态分类路径。"""

        if self.db is None:
            return []
        return ManagedFileRepository(self.db).list_category_paths()

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
