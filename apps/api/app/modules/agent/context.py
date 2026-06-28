"""Agent 运行前的文件上下文加载。"""

from __future__ import annotations

from typing import Any, Dict, List

from app.db.models import Document, DocumentInsight, DocumentPage


class AgentContextLoader:
    """按当前用户加载 AgentRun 需要的文件和洞察上下文。"""

    def __init__(self, db: Any = None) -> None:
        """保存请求级数据库会话；内存态测试可以不传数据库。"""

        self.db = db

    def load_documents(self, *, user_id: str, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """加载附件对应的 Document 和 document_insights 摘要。"""

        document_ids = [str(item["document_id"]) for item in attachments if item.get("document_id")]
        if not document_ids:
            return []
        if self.db is None:
            return [{"document_id": document_id} for document_id in document_ids]

        documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        insights = {
            insight.document_id: insight
            for insight in (
                self.db.query(DocumentInsight)
                .filter(DocumentInsight.document_id.in_([document.id for document in documents]))
                .all()
                if documents
                else []
            )
        }
        return [
            {
                "document_id": document.id,
                "filename": document.original_filename,
                "content_type": document.content_type,
                "status": document.status,
                "ingest_status": document.ingest_status,
                "keywords": (insights.get(document.id).keywords_json if insights.get(document.id) else []),
                "labels": (insights.get(document.id).labels_json if insights.get(document.id) else []),
                "summary": (insights.get(document.id).summary if insights.get(document.id) else ""),
            }
            for document in documents
        ]

    def load_extraction_texts(self, *, extraction_run_ids: List[str]) -> Dict[str, str]:
        """按解析运行读取已持久化的完整页面正文，用作分类依据。"""

        if self.db is None or not extraction_run_ids:
            return {}
        pages = (
            self.db.query(DocumentPage)
            .filter(DocumentPage.extraction_run_id.in_(extraction_run_ids))
            .order_by(
                DocumentPage.extraction_run_id.asc(),
                DocumentPage.page_number.asc().nullslast(),
                DocumentPage.created_at.asc(),
            )
            .all()
        )
        texts: Dict[str, list[str]] = {}
        for page in pages:
            texts.setdefault(page.extraction_run_id, []).append(page.text_content)
        return {run_id: "\n".join(parts) for run_id, parts in texts.items()}
