"""基于完整 document_pages 正文的 LLM 文档总结服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.db.models import DocumentPage
from app.modules.llm.client import LLMResponseError


DOCUMENT_SUMMARY_SYSTEM_PROMPT = """你是文件智能体的文档阅读助手。
请严格基于用户提供的文件正文，用中文完成总结、讲解或回答用户问题，不要编造正文中不存在的信息。
如果正文为空，请说明无法基于原文回答。直接输出面向用户的中文文本，不要输出 JSON。"""


@dataclass(slots=True)
class _DocumentText:
    """一次可交给 LLM 的文档正文。"""

    document_id: str
    filename: str
    text: str


class LLMDocumentSummaryService:
    """读取持久化全文并调用 LLM 生成总结或讲解。"""

    def __init__(
        self,
        *,
        db: Session | None = None,
        client: Any = None,
        enabled: bool = False,
        small_document_limit: int = 12000,
        chunk_size: int = 8000,
    ) -> None:
        """保存数据库、LLM 客户端和分块阈值。"""

        self.db = db
        self.client = client
        self.enabled = enabled
        self.small_document_limit = small_document_limit
        self.chunk_size = chunk_size

    def summarize_documents(
        self,
        *,
        document_results: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        user_message: str,
    ) -> str | None:
        """基于完整正文生成总结；未启用 LLM 时返回 None 交给调用方兜底。"""

        if not self.enabled or self.client is None:
            return None

        documents = self._load_document_texts(
            document_results=document_results,
            tool_results=tool_results,
        )
        documents = [document for document in documents if document.text.strip()]
        if not documents:
            return None

        try:
            total_chars = sum(len(document.text) for document in documents)
            if total_chars <= self.small_document_limit:
                return self._complete_summary(
                    mode="document_summary",
                    user_message=user_message,
                    documents=documents,
                )
            return self._summarize_large_documents(
                documents=documents,
                user_message=user_message,
            )
        except LLMResponseError:
            # 总结输出是用户体验层能力；模型格式异常时交给 Graph 使用确定性回执兜底，不能让请求变成 500。
            return None

    def _load_document_texts(
        self,
        *,
        document_results: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
    ) -> List[_DocumentText]:
        """优先按 extraction_run_id 从 document_pages 读取全文。"""

        run_by_document_id = {
            str(result.get("document_id") or ""): str(result.get("extraction_run_id") or "")
            for result in tool_results
            if result.get("document_id") and result.get("extraction_run_id")
        }
        page_texts = self._load_page_texts_by_run_id(list(run_by_document_id.values()))
        fallback_texts = self._fallback_texts_from_tool_results(tool_results)

        documents: List[_DocumentText] = []
        for result in document_results:
            if result.get("extraction_status") != "COMPLETED":
                continue
            document_id = str(result.get("document_id") or "")
            extraction_run_id = run_by_document_id.get(document_id, "")
            text = page_texts.get(extraction_run_id, "") or fallback_texts.get(document_id, "")
            documents.append(
                _DocumentText(
                    document_id=document_id,
                    filename=str(result.get("filename") or document_id or "未知文件"),
                    text=text,
                )
            )
        return documents

    def _load_page_texts_by_run_id(self, extraction_run_ids: List[str]) -> Dict[str, str]:
        """按解析运行读取完整页面正文，避免使用 text_preview。"""

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
        texts: Dict[str, List[str]] = {}
        for page in pages:
            if page.text_content:
                texts.setdefault(page.extraction_run_id, []).append(page.text_content)
        return {run_id: "\n".join(parts) for run_id, parts in texts.items()}

    def _fallback_texts_from_tool_results(self, tool_results: List[Dict[str, Any]]) -> Dict[str, str]:
        """无数据库测试路径使用 Tool 结果中的正文或预览兜底。"""

        texts: Dict[str, str] = {}
        for result in tool_results:
            document_id = str(result.get("document_id") or "")
            if not document_id:
                continue
            page_texts = []
            for page in result.get("pages", []):
                if not isinstance(page, dict):
                    continue
                page_texts.append(str(page.get("text") or page.get("text_content") or page.get("text_preview") or ""))
            texts[document_id] = "\n".join(item for item in page_texts if item)
        return texts

    def _summarize_large_documents(self, *, documents: List[_DocumentText], user_message: str) -> str:
        """大文件先分块总结，再汇总分块摘要。"""

        partial_summaries: List[str] = []
        for document in documents:
            chunks = _split_text(document.text, self.chunk_size)
            for chunk_index, chunk in enumerate(chunks, start=1):
                partial_summaries.append(
                    self._complete_summary(
                        mode="chunk_summary",
                        user_message=user_message,
                        documents=[
                            _DocumentText(
                                document_id=document.document_id,
                                filename=f"{document.filename} 第 {chunk_index} 段",
                                text=chunk,
                            )
                        ],
                    )
                )

        return self._complete_with_payload(
            system_prompt=DOCUMENT_SUMMARY_SYSTEM_PROMPT,
            user_payload={
                "mode": "merge_summary",
                "user_request": user_message,
                "partial_summaries": partial_summaries,
            },
        )

    def _complete_summary(
        self,
        *,
        mode: str,
        user_message: str,
        documents: List[_DocumentText],
    ) -> str:
        """调用 LLM 生成单次总结。"""

        return self._complete_with_payload(
            system_prompt=DOCUMENT_SUMMARY_SYSTEM_PROMPT,
            user_payload={
                "mode": mode,
                "user_request": user_message,
                "documents": [
                    {
                        "document_id": document.document_id,
                        "filename": document.filename,
                        "text": document.text,
                    }
                    for document in documents
                ],
            },
        )

    def _complete_with_payload(self, *, system_prompt: str, user_payload: Dict[str, Any]) -> str:
        """优先使用普通文本调用；旧测试或旧客户端缺少该方法时保留 JSON 兼容路径。"""

        if hasattr(self.client, "complete_text"):
            text = str(self.client.complete_text(system_prompt=system_prompt, user_payload=user_payload) or "").strip()
            return text or "LLM 未返回可用总结。"

        parsed = self.client.complete_json(system_prompt=system_prompt, user_payload=user_payload)
        return _summary_from_parsed(parsed)


def _split_text(text: str, chunk_size: int) -> List[str]:
    """把长文本切成固定上限的片段。"""

    safe_chunk_size = max(chunk_size, 1)
    return [text[index : index + safe_chunk_size] for index in range(0, len(text), safe_chunk_size)]


def _summary_from_parsed(parsed: Dict[str, Any]) -> str:
    """从 LLM JSON 中读取 summary 字段。"""

    summary = str(parsed.get("summary") or "").strip()
    return summary or "LLM 未返回可用总结。"
