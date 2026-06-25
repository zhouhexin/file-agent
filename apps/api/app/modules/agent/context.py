"""Agent 运行前的文件上下文加载。"""

from __future__ import annotations

from typing import Any, Dict, List

from app.db.models import Document, DocumentInsight


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
