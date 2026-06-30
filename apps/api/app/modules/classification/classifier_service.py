"""基于持久化正文的文档分类服务。"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import DocumentPage
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.matcher import match_document_text


class DocumentClassificationService:
    """从 DocumentPage 读取全文并生成 rule-only 分类建议。"""

    def __init__(self, db: Session | None = None) -> None:
        """保存请求级数据库会话；无数据库时仅支持 fallback_text。"""

        self.db = db

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
            taxonomy = load_default_taxonomy()
            categories = match_document_text(text=f"{filename}\n{classification_text}", taxonomy=taxonomy)
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
        }

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

        signals = [str(item) for item in category.get("evidence", []) if item]
        evidence_item = _find_text_quote(signals=signals, pages=pages, fallback_text=fallback_text)
        if evidence_item is None:
            return {**category, "status": "NEEDS_REVIEW", "evidence_items": []}
        return {**category, "evidence_items": [evidence_item]}


def _find_text_quote(
    *,
    signals: list[str],
    pages: list[DocumentPage],
    fallback_text: str,
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
                    "source": "rule",
                }
        quote = _quote_around_signal(text=fallback_text, signal=signal)
        if quote:
            return {
                "type": "text_quote",
                "page_number": None,
                "sheet_name": None,
                "quote": quote,
                "signals": [signal],
                "source": "rule",
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
