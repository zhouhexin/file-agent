"""普通文档摘要和分类主题摘要持久化测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    Document,
    DocumentClassificationSummary,
    DocumentExtractionRun,
    DocumentPage,
    DocumentSummary,
    DocumentVersion,
)
from app.modules.classification.summary_service import (
    DocumentSummaryService,
    _build_sentence_candidates,
)


class FakeDualSummaryClient:
    """返回固定双摘要，保护模型输出契约和偶发主题边界。"""

    model = "fake-summary-model"

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.calls = 0

    def complete_json(self, *, system_prompt: str, user_payload: dict) -> dict:
        """返回干部考察主旨，并把科研教学明确标为偶发主题。"""

        self.calls += 1
        assert "分类主题摘要" in system_prompt
        assert "科研工作经历" in user_payload["document_text"]
        return {
            "document_summary": {
                "overview": "本文件报告三名军转干部的组织考察结果。",
                "key_points": [
                    {
                        "text": "文件主要事项是干部考察。",
                        "evidence_refs": [
                            {
                                "page_number": 1,
                                "sheet_name": None,
                                "quote": "关于三名军转干部考察结果的报告",
                            }
                        ],
                    }
                ],
                "section_summaries": [],
                "summary_confidence": 0.95,
            },
            "classification_topic_summary": {
                "document_type": "干部考察结果报告",
                "primary_topic": "三名军转干部组织考察",
                "business_action": "报告干部考察结论",
                "subjects": ["三名军转干部"],
                "organizations": [],
                "time_range": [],
                "keywords": ["军转干部", "考察结果"],
                "secondary_topics": [],
                "incidental_topics": [
                    {
                        "topic": "科研、教学",
                        "reason": "仅属于个人履历",
                        "evidence_refs": [
                            {
                                "page_number": 2,
                                "sheet_name": None,
                                "quote": "科研工作经历和教学成果",
                            }
                        ],
                    }
                ],
                "evidence_refs": [
                    {
                        "page_number": 1,
                        "sheet_name": None,
                        "quote": "关于三名军转干部考察结果的报告",
                    }
                ],
                "summary_confidence": 0.95,
            },
        }


def _db_session():
    """创建包含完整模型表的隔离数据库会话。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_dual_summary_is_persisted_reused_and_excludes_incidental_topics_from_recall():
    """双摘要必须按版本复用，分类召回文本不得重新包含偶发科研教学内容。"""

    db = _db_session()
    client = FakeDualSummaryClient()
    try:
        document = Document(
            id="summary-document",
            user_id="summary-user",
            workspace_id="summary-workspace",
            original_filename="干部考察报告.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=100,
            sha256="a" * 64,
        )
        db.add(document)
        version = DocumentVersion(
            id="summary-version",
            document_id=document.id,
            version_number=1,
            storage_tier="WORKING_COPY",
            storage_path="test/干部考察报告.docx",
            filename=document.original_filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            source_type="IMPORT",
        )
        run = DocumentExtractionRun(
            id="summary-extraction",
            document_id=document.id,
            status="COMPLETED",
            extractor="fake",
        )
        db.add_all([version, run])
        db.add_all(
            [
                DocumentPage(
                    document_id=document.id,
                    extraction_run_id=run.id,
                    page_number=1,
                    text_content="关于三名军转干部考察结果的报告",
                    metadata_json={},
                ),
                DocumentPage(
                    document_id=document.id,
                    extraction_run_id=run.id,
                    page_number=2,
                    text_content="个人履历包含科研工作经历和教学成果。",
                    metadata_json={},
                ),
            ]
        )
        db.flush()
        settings = Settings(
            database_url="sqlite+pysqlite://",
            llm_enabled=True,
            llm_api_key="fake",
            llm_base_url="http://fake",
            llm_chat_model=client.model,
            document_summary_enabled=True,
            document_summary_provider="llm",
            classification_summary_provider="llm",
        )
        service = DocumentSummaryService(db=db, settings=settings, client=client)

        first = service.generate_or_reuse(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=run.id,
            filename=document.original_filename,
        )
        second = service.generate_or_reuse(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=run.id,
            filename=document.original_filename,
        )

        assert first is not None and second is not None
        assert first.reused is False
        assert second.reused is True
        assert client.calls == 1
        assert "军转干部" in first.classification_text
        assert "科研" not in first.classification_text
        assert "教学" not in first.classification_text
        assert db.query(DocumentSummary).count() == 1
        assert db.query(DocumentClassificationSummary).count() == 1
    finally:
        db.close()


def test_default_extractive_provider_uses_jieba_lexrank_without_calling_llm():
    """上传阶段默认必须用本地抽取式摘要，即使全局 LLM 已启用也不能自动外发正文。"""

    db = _db_session()
    client = FakeDualSummaryClient()
    try:
        document = Document(
            id="extractive-document",
            user_id="extractive-user",
            workspace_id="extractive-workspace",
            original_filename="工作安排.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=200,
            sha256="b" * 64,
        )
        version = DocumentVersion(
            id="extractive-version",
            document_id=document.id,
            version_number=1,
            storage_tier="WORKING_COPY",
            storage_path="test/工作安排.docx",
            filename=document.original_filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            source_type="IMPORT",
        )
        run = DocumentExtractionRun(
            id="extractive-run",
            document_id=document.id,
            status="COMPLETED",
            extractor="fake",
        )
        db.add_all([document, version, run])
        db.add(
            DocumentPage(
                document_id=document.id,
                extraction_run_id=run.id,
                page_number=1,
                text_content=(
                    "工作安排\n请各单位按要求报送材料。\n联系人信息见附件。\n"
                    "国家励志奖学金评审面向家庭经济困难学生。\n"
                    "国家励志奖学金申请材料包括成绩证明和困难证明。\n"
                    "国家励志奖学金材料须在规定日期前提交。"
                ),
                metadata_json={},
            )
        )
        db.flush()
        settings = Settings(
            database_url="sqlite+pysqlite://",
            llm_enabled=True,
            llm_api_key="fake",
            llm_base_url="http://fake",
            llm_chat_model=client.model,
        )

        result = DocumentSummaryService(
            db=db,
            settings=settings,
            client=client,
        ).generate_or_reuse(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=run.id,
            filename=document.original_filename,
        )

        assert result is not None
        assert client.calls == 0
        assert result.document_summary.model_provider == "deterministic"
        assert result.document_summary.model_name == "jieba-lexrank-v1"
        assert result.classification_summary.model_provider == "deterministic"
        assert "国家励志奖学金" in result.document_summary.summary_text
        evidence_refs = result.classification_summary.summary_json["evidence_refs"]
        assert evidence_refs
        assert all(item["quote"] in document.pages[0].text_content for item in evidence_refs)
    finally:
        db.close()


def test_extractive_summary_caps_candidates_and_samples_later_sections():
    """超长文档候选句必须固定封顶，同时不能只保留文档开头。"""

    page = DocumentPage(
        document_id="candidate-document",
        extraction_run_id="candidate-run",
        page_number=1,
        text_content="\n".join(f"第{index}节奖学金材料说明。" for index in range(400)),
        metadata_json={},
    )

    candidates, truncated = _build_sentence_candidates(
        full_text=page.text_content,
        pages=[page],
    )

    assert truncated is True
    assert len(candidates) == 160
    assert candidates[0].order == 0
    assert max(candidate.order for candidate in candidates) > 300
