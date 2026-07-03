"""LLM 文档总结服务测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import DocumentExtractionRun, DocumentPage
from app.modules.llm.document_summary import LLMDocumentSummaryService


def _db_session():
    """创建隔离内存数据库会话。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class FakeLLMClient:
    """记录 LLM 请求并返回固定 JSON。"""

    def __init__(self):
        """初始化调用记录。"""

        self.calls: list[dict] = []

    def complete_json(self, *, system_prompt: str, user_payload: dict) -> dict:
        """记录调用参数，返回可断言的总结内容。"""

        self.calls.append({"system_prompt": system_prompt, "user_payload": user_payload})
        if user_payload["mode"] == "merge_summary":
            return {"summary": "最终总结：已合并分块内容。"}
        return {"summary": f"总结：{user_payload['documents'][0]['text'][:20]}"}


class TextOnlyLLMClient:
    """模拟只返回普通文本的总结模型。"""

    def __init__(self):
        """初始化文本调用记录。"""

        self.calls: list[dict] = []

    def complete_text(self, *, system_prompt: str, user_payload: dict) -> str:
        """记录调用参数，返回非 JSON 的自然语言总结。"""

        self.calls.append({"system_prompt": system_prompt, "user_payload": user_payload})
        return "这是一段普通中文总结，不是 JSON。"


def test_summary_service_accepts_plain_text_llm_response():
    """总结、讲解类请求应允许 LLM 返回普通文本，不能因非 JSON 响应中断 AgentRun。"""

    db = _db_session()
    client = TextOnlyLLMClient()
    try:
        run = DocumentExtractionRun(id="run-text", document_id="doc-text", status="COMPLETED", extractor="plain-text")
        db.add(run)
        db.add(
            DocumentPage(
                document_id="doc-text",
                extraction_run_id="run-text",
                page_number=1,
                text_content="这是一份关于文件智能体 OCR 和分类流程的说明。",
                metadata_json={},
            )
        )
        db.flush()

        service = LLMDocumentSummaryService(db=db, client=client, enabled=True)
        summary = service.summarize_documents(
            document_results=[
                {
                    "document_id": "doc-text",
                    "filename": "说明.txt",
                    "extraction_status": "COMPLETED",
                }
            ],
            tool_results=[{"document_id": "doc-text", "extraction_run_id": "run-text", "pages": []}],
            user_message="总结这个文件",
        )

        assert summary == "这是一段普通中文总结，不是 JSON。"
        assert client.calls[0]["user_payload"]["mode"] == "document_summary"
    finally:
        db.close()


def test_summary_service_reads_full_document_pages_not_preview():
    """总结服务必须读取 document_pages 全文，不能只使用 Tool 返回的 text_preview。"""

    db = _db_session()
    client = FakeLLMClient()
    try:
        run = DocumentExtractionRun(id="run-full", document_id="doc-full", status="COMPLETED", extractor="plain-text")
        db.add(run)
        db.add(
            DocumentPage(
                document_id="doc-full",
                extraction_run_id="run-full",
                page_number=1,
                text_content="完整正文：这里包含岗位锻炼安排、考核要求和组织保障。",
                metadata_json={},
            )
        )
        db.flush()

        service = LLMDocumentSummaryService(db=db, client=client, enabled=True)
        summary = service.summarize_documents(
            document_results=[
                {
                    "document_id": "doc-full",
                    "filename": "青年教师岗位锻炼.docx",
                    "extraction_status": "COMPLETED",
                }
            ],
            tool_results=[
                {
                    "document_id": "doc-full",
                    "extraction_run_id": "run-full",
                    "pages": [{"text_preview": "短预览"}],
                }
            ],
            user_message="总结这份文件",
        )

        payload = client.calls[0]["user_payload"]
        assert "完整正文" in payload["documents"][0]["text"]
        assert "短预览" not in payload["documents"][0]["text"]
        assert "总结：" in summary
    finally:
        db.close()


def test_summary_service_chunks_large_documents_and_merges():
    """大文件必须先分块总结，再把分块摘要交给 LLM 汇总。"""

    db = _db_session()
    client = FakeLLMClient()
    try:
        run = DocumentExtractionRun(id="run-large", document_id="doc-large", status="COMPLETED", extractor="plain-text")
        db.add(run)
        db.add(
            DocumentPage(
                document_id="doc-large",
                extraction_run_id="run-large",
                page_number=1,
                text_content="A" * 25,
                metadata_json={},
            )
        )
        db.flush()

        service = LLMDocumentSummaryService(db=db, client=client, enabled=True, small_document_limit=10, chunk_size=10)
        summary = service.summarize_documents(
            document_results=[
                {
                    "document_id": "doc-large",
                    "filename": "large.txt",
                    "extraction_status": "COMPLETED",
                }
            ],
            tool_results=[{"document_id": "doc-large", "extraction_run_id": "run-large", "pages": []}],
            user_message="讲解这个文件内容",
        )

        modes = [call["user_payload"]["mode"] for call in client.calls]
        assert modes == ["chunk_summary", "chunk_summary", "chunk_summary", "merge_summary"]
        assert client.calls[-1]["user_payload"]["partial_summaries"] == [
            "总结：AAAAAAAAAA",
            "总结：AAAAAAAAAA",
            "总结：AAAAA",
        ]
        assert summary == "最终总结：已合并分块内容。"
    finally:
        db.close()
