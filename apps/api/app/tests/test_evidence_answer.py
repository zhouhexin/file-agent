"""阶段五证据回答闭环回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    AnswerReference,
    Conversation,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    Document,
    DocumentChunk,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentSummary,
    DocumentVersion,
    EvidenceSpan,
    FilesystemJob,
    ManagedFile,
    ManagedFileAnalysisRun,
    ManagedFileRevision,
    ManagedRoot,
    QAAnswer,
    TrashEntry,
    UploadArchiveRecord,
    User,
    Workspace,
    WorkingCopy,
    utcnow,
)
from app.modules.evidence_answer.service import (
    EvidenceAnswerService,
    _strip_legacy_inline_reference_indexes,
)
from app.modules.evidence_answer.policy import EvidenceQuestionPolicy
from app.modules.agent.user_receipt import UserTaskReceipt
from app.modules.conversations.repository import (
    AttachmentAvailabilityProjection,
    ConversationRepository,
    _evidence_reference_document_ids,
)
from app.modules.file_lifecycle.shared_workspace import (
    SHARED_WORKSPACE_SYSTEM_KEY,
    SHARED_WORKSPACE_TYPE,
)
from app.modules.retrieval.clarification_planner import FileSearchClarificationPlanner
from app.modules.retrieval.clarification_service import FileSearchClarificationService


class FakeEvidenceClient:
    """根据输入证据返回稳定结构化回答。"""

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.calls = 0
        self.payloads: list[dict] = []

    def complete_json(self, *, system_prompt, user_payload):
        """引用第一条真实 evidence_id，避免测试依赖外部模型。"""

        self.calls += 1
        self.payloads.append(user_payload)
        evidence = user_payload["evidence"]
        return {
            "claims": [
                {
                    "text": "申报截止时间是2026年7月31日。",
                    "evidence_ids": [evidence[0]["evidence_id"]],
                }
            ],
            "limitations": [],
            "status": "COMPLETED",
        }


class EchoEvidenceClient:
    """把第一条受控证据原样作为结论，保护源侧首答的引用校验边界。"""

    def complete_json(self, *, system_prompt, user_payload):
        """只引用传入证据，不依赖真实模型或固定业务文本。"""

        evidence = user_payload["evidence"][0]
        return {
            "claims": [
                {
                    "text": evidence["quote"],
                    "evidence_ids": [evidence["evidence_id"]],
                }
            ],
            "limitations": [],
            "status": "COMPLETED",
        }


def _session():
    """创建启用完整 ORM 表的 SQLite 测试会话。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _settings() -> Settings:
    """构造不访问真实部署配置的阶段五设置。"""

    return Settings(
        database_url="postgresql://test:test@localhost/test",
        llm_enabled=True,
        llm_api_key="fake",
        llm_base_url="https://example.invalid",
        llm_chat_model="fake-model",
        evidence_answer_provider="llm",
    )


def _seed(db):
    """创建一个共享活动工作副本、当前索引和可引用证据。"""

    user = User(id="user-1", username="user-1", password_hash="x")
    shared = Workspace(
        id="workspace-shared",
        name="共享工作区",
        workspace_type=SHARED_WORKSPACE_TYPE,
        system_key=SHARED_WORKSPACE_SYSTEM_KEY,
    )
    conversation = Conversation(
        id="conversation-1",
        user_id=user.id,
        workspace_id=shared.id,
        title="阶段五测试",
    )
    document = Document(
        id="document-1",
        user_id=user.id,
        workspace_id=shared.id,
        original_filename="申报通知.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=128,
        sha256="a" * 64,
        status="READY",
        ingest_status="READY",
    )
    version = DocumentVersion(
        id="version-1",
        document_id=document.id,
        version_number=1,
        storage_tier="WORKING_COPY",
        storage_path="待整理/申报通知.docx",
        filename="申报通知.docx",
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        source_type="IMPORT",
    )
    working_copy = WorkingCopy(
        id="working-copy-1",
        working_copy_root_id="working-root-1",
        workspace_id=shared.id,
        managed_file_id="managed-file-1",
        document_id=document.id,
        current_version_id=version.id,
        relative_path="待整理/申报通知.docx",
        relative_path_hash="b" * 64,
        filename="申报通知.docx",
        extension=".docx",
        size_bytes=document.size_bytes,
        content_sha256=document.sha256,
        imported_source_sha256=document.sha256,
        status="ACTIVE",
    )
    extraction = DocumentExtractionRun(
        id="extraction-1",
        document_id=document.id,
        document_version_id=version.id,
        status="COMPLETED",
    )
    index_run = DocumentIndexRun(
        id="index-1",
        document_id=document.id,
        document_version_id=version.id,
        extraction_run_id=extraction.id,
        index_version="document-chunk-index-v2",
        tokenizer="jieba",
        tokenizer_version="test",
        config_hash="c" * 64,
        status="COMPLETED",
        chunk_count=1,
        evidence_count=1,
    )
    chunk = DocumentChunk(
        id="chunk-1",
        index_run_id=index_run.id,
        document_id=document.id,
        document_version_id=version.id,
        extraction_run_id=extraction.id,
        chunk_index=0,
        chunk_type="page",
        text_content="申报截止时间是2026年7月31日。",
        search_text="申报 截止 时间 2026 7 31",
        content_hash="d" * 64,
        location_hash="e" * 64,
        char_count=18,
        token_count=7,
        page_start=1,
        page_end=1,
    )
    evidence = EvidenceSpan(
        id="evidence-1",
        chunk_id=chunk.id,
        document_id=document.id,
        document_version_id=version.id,
        extraction_run_id=extraction.id,
        span_index=0,
        evidence_type="text_quote",
        quote=chunk.text_content,
        start_offset=0,
        end_offset=len(chunk.text_content),
        page_number=1,
        source="document_chunk",
    )
    db.add_all(
        [
            user,
            shared,
            conversation,
            document,
            version,
            working_copy,
            extraction,
            index_run,
            chunk,
            evidence,
        ]
    )
    db.flush()
    return working_copy, version


def test_evidence_answer_persists_validated_references_and_reuses_cache():
    """真实证据回答必须落库引用，完全相同的活动版本请求可以复用缓存。"""

    db = _session()
    working_copy, _ = _seed(db)
    client = FakeEvidenceClient()
    service = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=client,
    )

    first = service.answer(
        question="这份文件的申报截止时间是什么？",
        document_ids=["document-1"],
    )
    second = service.answer(
        question="这份文件的申报截止时间是什么？",
        document_ids=["document-1"],
    )

    assert first["status"] == "COMPLETED"
    # 普通回执不显示内部 [1] 索引，但必须让用户看到最终结论实际引用的原文与页码。
    assert first["answer"] == "申报截止时间是2026年7月31日。"
    assert first["references"] == [
        {
            "document_id": "document-1",
            "document_version_id": "version-1",
            "working_copy_id": working_copy.id,
            "filename": "申报通知.docx",
            "category_labels": [],
            "availability": "AVAILABLE",
            "availability_message": "文件可用",
            "can_open": True,
            "can_restore": False,
            "reference_indexes": [1],
            "evidence_items": [
                {
                    "quote": "申报截止时间是2026年7月31日。",
                    "page_number": 1,
                    "sheet_name": None,
                    "cell_range": None,
                }
            ],
        }
    ]
    assert second["cached"] is True
    assert client.calls == 1
    assert db.query(QAAnswer).count() == 1
    reference = db.query(AnswerReference).one()
    assert reference.evidence_span_id == "evidence-1"
    assert reference.working_copy_id == working_copy.id


def test_deleted_explicit_document_never_reads_old_evidence():
    """附件历史引用指向回收站文件时只能提示恢复，不能复用旧正文回答。"""

    db = _session()
    working_copy, version = _seed(db)
    working_copy.status = "TRASHED"
    db.add(
        TrashEntry(
            id="trash-1",
            workspace_id=working_copy.workspace_id,
            working_copy_id=working_copy.id,
            document_version_id=version.id,
            entry_type="DELETE",
            original_relative_path=working_copy.relative_path,
            trash_relative_path="trash/申报通知.docx",
            status="ACTIVE",
            deleted_by="user-1",
            deleted_at=utcnow(),
            retention_until=utcnow(),
        )
    )
    db.flush()

    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    ).answer(
        question="这份文件的申报截止时间是什么？",
        document_ids=["document-1"],
    )

    assert result["kind"] == "trash_restore_selection"
    assert result["status"] == "NEEDS_CONFIRMATION"
    assert result["answer"] == ""
    assert db.query(QAAnswer).count() == 0


def test_invalid_numeric_claim_is_removed_instead_of_being_displayed():
    """模型生成证据外数字时必须拒绝该结论，不能只校验引用 ID 存在。"""

    class InvalidNumberClient(FakeEvidenceClient):
        """返回引用存在但数字不受证据支持的错误结论。"""

        def complete_json(self, *, system_prompt, user_payload):
            """故意把截止日期改为不存在的日期。"""

            self.calls += 1
            return {
                "claims": [
                    {
                        "text": "申报截止时间是2026年8月31日。",
                        "evidence_ids": [user_payload["evidence"][0]["evidence_id"]],
                    }
                ],
                "limitations": [],
                "status": "COMPLETED",
            }

    db = _session()
    _seed(db)
    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=InvalidNumberClient(),
    ).answer(
        question="这份文件的申报截止时间是什么？",
        document_ids=["document-1"],
    )

    assert result["status"] == "NO_EVIDENCE"
    assert "2026年8月31日" not in result["answer"]
    assert db.query(QAAnswer).count() == 0


def test_unknown_evidence_id_is_rejected():
    """模型引用本次证据包之外的 ID 时必须拒绝整条结论。"""

    class UnknownEvidenceClient(FakeEvidenceClient):
        """返回不存在的 Evidence ID。"""

        def complete_json(self, *, system_prompt, user_payload):
            """模拟模型伪造引用。"""

            self.calls += 1
            return {
                "claims": [
                    {
                        "text": "申报截止时间是2026年7月31日。",
                        "evidence_ids": ["evidence-not-in-package"],
                    }
                ],
                "limitations": [],
                "status": "COMPLETED",
            }

    db = _session()
    _seed(db)
    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=UnknownEvidenceClient(),
    ).answer(
        question="这份文件的申报截止时间是什么？",
        document_ids=["document-1"],
    )

    assert result["status"] == "NO_EVIDENCE"
    assert result["references"] == []
    assert db.query(AnswerReference).count() == 0


def test_claim_with_reversed_negation_is_rejected():
    """引用文字中的否定关系不能被模型改写为肯定结论。"""

    class ReversedNegationClient(FakeEvidenceClient):
        """把“不需要提交”错误改写成“需要提交”。"""

        def complete_json(self, *, system_prompt, user_payload):
            """返回词项高度相似但语义相反的结论。"""

            self.calls += 1
            return {
                "claims": [
                    {
                        "text": "申请人需要提交纸质材料。",
                        "evidence_ids": [user_payload["evidence"][0]["evidence_id"]],
                    }
                ],
                "limitations": [],
                "status": "COMPLETED",
            }

    db = _session()
    _seed(db)
    evidence = db.query(EvidenceSpan).one()
    chunk = db.query(DocumentChunk).one()
    evidence.quote = "申请人不需要提交纸质材料。"
    chunk.text_content = evidence.quote
    db.flush()

    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=ReversedNegationClient(),
    ).answer(
        question="申请人是否需要提交纸质材料？",
        document_ids=["document-1"],
    )

    assert result["status"] == "NO_EVIDENCE"
    assert "申请人需要提交纸质材料" not in result["answer"]


def test_index_pending_is_not_reported_as_no_matching_fact():
    """当前版本索引仍在构建时必须返回 INDEX_PENDING，且不得调用模型。"""

    db = _session()
    _seed(db)
    db.query(DocumentIndexRun).one().status = "RUNNING"
    db.flush()
    client = FakeEvidenceClient()

    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=client,
    ).answer(
        question="这份文件的申报截止时间是什么？",
        document_ids=["document-1"],
    )

    assert result["status"] == "NO_EVIDENCE"
    assert result["index_status"] == "INDEX_PENDING"
    # 普通用户只看到统一的暂时不可读提示，索引阶段保留在内部审计字段中。
    assert "暂时无法读取" in result["answer"]
    assert "索引" not in result["answer"]
    assert client.calls == 0


def test_uploaded_document_id_resolves_to_completed_active_working_copy():
    """worker 完成导入后，上传附件 ID 必须沿归档血缘解析到活动工作副本。"""

    db = _session()
    _seed(db)
    working_document = db.get(Document, "document-1")
    assert working_document is not None
    upload_document = Document(
        id="upload-document-1",
        user_id="user-1",
        workspace_id="workspace-shared",
        original_filename="申报通知.docx",
        content_type=working_document.content_type,
        size_bytes=working_document.size_bytes,
        sha256=working_document.sha256,
        status="UPLOADED",
        ingest_status="INGESTED",
    )
    upload_version = DocumentVersion(
        id="upload-version-1",
        document_id=upload_document.id,
        version_number=1,
        storage_tier="UPLOAD",
        storage_path="quarantine/申报通知.docx",
        filename=upload_document.original_filename,
        content_type=upload_document.content_type,
        size_bytes=upload_document.size_bytes,
        sha256=upload_document.sha256,
        source_type="UPLOAD",
    )
    archive = UploadArchiveRecord(
        upload_document_version_id=upload_version.id,
        managed_file_id="managed-file-1",
        content_sha256=upload_document.sha256,
        status="ARCHIVED",
    )
    db.add_all([upload_document, upload_version, archive])
    db.flush()
    client = FakeEvidenceClient()

    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=client,
    ).answer(
        question="这份文件的申报截止时间是什么？",
        document_ids=[upload_document.id],
    )

    assert result["status"] == "COMPLETED"
    assert result["references"][0]["document_id"] == "document-1"
    assert client.calls == 1


def test_explicit_filename_locks_single_active_working_copy_without_workspace_recall():
    """用户写出完整文件名时只能读取该活动副本，不能扩大到共享目录候选。"""

    db = _session()
    _seed(db)
    service = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=EchoEvidenceClient(),
    )

    # 若实现误退回语义召回，这个断言会直接暴露单文件范围被扩大。
    service._recall_active_working_copies = lambda _question: pytest.fail("不应执行共享目录召回")
    result = service.answer(question="请完整总结申报通知.docx")

    assert result["status"] == "COMPLETED"
    assert result["answer"] == "申报截止时间是2026年7月31日。"
    assert [item["filename"] for item in result["references"]] == ["申报通知.docx"]


def test_plain_summary_reuses_current_version_persisted_summary_evidence():
    """普通总结优先复用当前版本已有摘要引用，不能默认重新读取整份正文。"""

    db = _session()
    _seed(db)
    db.add(
        DocumentSummary(
            document_id="document-1",
            document_version_id="version-1",
            extraction_run_id="extraction-1",
            input_sha256="f" * 64,
            summary_text="申报截止时间是2026年7月31日。",
            summary_json={
                "overview": "申报截止时间是2026年7月31日。",
                "key_points": [
                    {
                        "text": "申报截止时间是2026年7月31日。",
                        "evidence_refs": [
                            {
                                "page_number": 1,
                                "sheet_name": None,
                                "quote": "申报截止时间是2026年7月31日。",
                            }
                        ],
                    }
                ],
                "section_summaries": [],
                "summary_confidence": 0.8,
            },
            coverage_json={},
            model_provider="deterministic",
            model_name="jieba-lexrank",
            prompt_version="summary-v1",
            schema_version="summary-v1",
            status="COMPLETED",
        )
    )
    db.flush()
    service = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    )
    service._load_evidence = lambda **_kwargs: pytest.fail("普通总结不应读取完整正文证据")

    result = service.answer(question="总结申报通知.docx")

    assert result["status"] == "COMPLETED"
    assert db.query(QAAnswer).one().answer_mode == "FOCUSED"
    assert result["references"][0]["document_id"] == "document-1"


def test_detailed_summary_bypasses_persisted_overview_and_reads_full_evidence():
    """用户要求完整、详细或章节覆盖时必须读取正文，不能只复述后台概览摘要。"""

    db = _session()
    _seed(db)
    service = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    )
    service._load_persisted_summary_evidence = lambda **_kwargs: pytest.fail(
        "完整总结不能只读取持久化概览"
    )

    result = service.answer(question="完整总结申报通知.docx，覆盖每个章节")

    assert result["status"] == "COMPLETED"
    assert db.query(QAAnswer).one().answer_mode == "FULL_SUMMARY"


def test_fuzzy_summary_scope_requires_selection_even_for_one_candidate():
    """没有完整文件名时召回结果只是候选，即使唯一也要先让用户确认。"""

    db = _session()
    working_copy, version = _seed(db)
    client = FakeEvidenceClient()
    service = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=client,
    )
    service._recall_active_working_copies = lambda _question: [(working_copy, version)]

    result = service.answer(question="总结申报材料")

    assert result["kind"] == "file_selection"
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert result["choices"][0]["document_id"] == "document-1"
    assert client.calls == 0


def test_explicit_filename_overrides_inferred_context_attachment_scope():
    """完整文件名必须覆盖会话模糊推断出的附件，不能把候选正文混入总结。"""

    db = _session()
    _seed(db)
    service = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    )

    # 模拟会话上下文曾把“申报通知”作为一个历史附件传入，但用户本轮写的是
    # 不同的完整文件名。旧逻辑会直接读取该附件；新逻辑只能先显示相似文件选择卡。
    result = service.answer(
        question="完整总结申报通告.docx，覆盖每个章节",
        document_ids=["document-1"],
    )

    assert result["kind"] == "file_selection"
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert result["answer"] == ""
    assert result["choices"][0]["filename"] == "申报通知.docx"


def test_unmatched_explicit_filename_returns_similar_selection_without_answering():
    """完整文件名未命中只能展示相似文件单选，不能拿候选正文直接回答。"""

    db = _session()
    _seed(db)
    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    ).answer(question="总结申报通告.docx")

    assert result["kind"] == "file_selection"
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert result["answer"] == ""
    assert result["choices"][0]["filename"] == "申报通知.docx"


def test_file_selection_includes_current_category_suggestions_and_relative_directory():
    """同名候选卡只展示当前版本分类与工作副本相对目录，不能混入历史分类或绝对路径。"""

    db = _session()
    working_copy, version = _seed(db)
    working_copy.relative_path = "shared/财务处/劳务费发放/申报通知.docx"
    current_run = DocumentClassificationRun(
        id="classification-current",
        document_id="document-1",
        agent_run_id="classification-agent-run",
        taxonomy_key="school-file",
        taxonomy_version="v2",
        status="COMPLETED",
    )
    current_suggestion = DocumentCategorySuggestion(
        id="suggestion-current",
        classification_run_id=current_run.id,
        document_id="document-1",
        document_version_id=version.id,
        category_id="finance-labor-payment",
        category_name="劳务费发放",
        category_path_json=["学校", "财务", "劳务费发放"],
        taxonomy_key="school-file",
        taxonomy_version="v2",
        status="SUGGESTED",
        rank=1,
    )
    # 即使更晚写入了历史版本建议，当前选择卡也必须按 current_version_id 过滤。
    stale_run = DocumentClassificationRun(
        id="classification-stale",
        document_id="document-1",
        agent_run_id="classification-agent-run",
        taxonomy_key="school-file",
        taxonomy_version="v1",
        status="COMPLETED",
    )
    stale_suggestion = DocumentCategorySuggestion(
        id="suggestion-stale",
        classification_run_id=stale_run.id,
        document_id="document-1",
        document_version_id="version-history",
        category_id="stale-category",
        category_name="历史错误分类",
        category_path_json=["历史错误分类"],
        taxonomy_key="school-file",
        taxonomy_version="v1",
        status="SUGGESTED",
        rank=1,
    )
    db.add_all([current_run, current_suggestion, stale_run, stale_suggestion])
    db.flush()

    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    )._create_file_selection(
        rows=[(working_copy, version)],
        question="读取申报通知.docx",
        message="请选择文件。",
    )

    choice = result["choices"][0]
    assert choice["suggested_category_labels"] == ["学校 / 财务 / 劳务费发放"]
    assert choice["directory_path"] == "shared/财务处/劳务费发放"
    assert "/Users/" not in choice["directory_path"]


def test_unmatched_explicit_filename_without_similar_file_requests_reupload():
    """没有相似活动副本时要求重新附加，禁止回退全库检索并混入其他文件。"""

    db = _session()
    _seed(db)
    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    ).answer(question="总结完全不存在的材料.pdf")

    assert result["status"] == "NO_EVIDENCE"
    assert result["references"] == []
    assert "重新附加文件" in result["answer"]


def test_full_summary_marks_partial_instead_of_silently_truncating_batches():
    """全文超过调用安全上限时只能返回 PARTIAL，不能声称已经完整总结。"""

    db = _session()
    _seed(db)
    chunk = db.query(DocumentChunk).one()
    second_chunk = DocumentChunk(
        id="chunk-2",
        index_run_id=chunk.index_run_id,
        document_id=chunk.document_id,
        document_version_id=chunk.document_version_id,
        extraction_run_id=chunk.extraction_run_id,
        chunk_index=1,
        chunk_type="page",
        text_content="后续章节" * 3000,
        search_text="后续 章节",
        content_hash="2" * 64,
        location_hash="3" * 64,
        char_count=12000,
        token_count=6000,
        page_start=2,
        page_end=2,
    )
    second_evidence = EvidenceSpan(
        id="evidence-2",
        chunk_id=second_chunk.id,
        document_id=chunk.document_id,
        document_version_id=chunk.document_version_id,
        extraction_run_id=chunk.extraction_run_id,
        span_index=0,
        evidence_type="text_quote",
        quote=second_chunk.text_content,
        start_offset=0,
        end_offset=len(second_chunk.text_content),
        page_number=2,
        source="document_chunk",
    )
    db.add_all([second_chunk, second_evidence])
    db.query(DocumentIndexRun).one().evidence_count = 2
    db.query(DocumentIndexRun).one().chunk_count = 2
    db.flush()
    client = FakeEvidenceClient()
    settings = _settings().model_copy(
        update={
            "evidence_answer_max_input_chars": 10_000,
            "evidence_answer_max_calls": 1,
        }
    )

    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=settings,
        client=client,
    ).answer(
        question="完整总结这份文件",
        document_ids=["document-1"],
    )

    assert result["status"] == "PARTIAL"
    assert any("只覆盖" in value for value in result["limitations"])
    assert client.calls == 1


def test_plain_and_full_summary_use_different_reading_depths():
    """普通概览与明确全文总结必须使用不同深度，不能错误复用同一缓存。"""

    db = _session()
    _seed(db)
    client = FakeEvidenceClient()
    service = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=client,
    )

    first = service.answer(question="总结申报通知.docx")
    second = service.answer(question="请完整总结申报通知.docx，覆盖每个章节")

    assert first["status"] == "COMPLETED"
    assert second["cached"] is False
    assert second["answer"] == first["answer"]
    assert client.calls == 2
    assert "完整总结" in client.payloads[1]["question"]


def test_legacy_reference_cleanup_removes_only_persisted_reference_indexes():
    """历史回答的 [1] 要隐藏，但正文中的年份 [2023] 不能被误删。"""

    text = "依据〔2023〕4号文件执行。[1]"

    assert _strip_legacy_inline_reference_indexes(
        text,
        reference_indexes={1},
    ) == "依据〔2023〕4号文件执行。"


def test_same_name_different_content_persists_selection_before_answering():
    """同名不同内容候选必须持久化选择，选择后计划只能读取用户选中的 Document。"""

    db = _session()
    first_copy, _ = _seed(db)
    second_document = Document(
        id="document-2",
        user_id="user-1",
        workspace_id=first_copy.workspace_id,
        original_filename=first_copy.filename,
        content_type="application/pdf",
        size_bytes=256,
        sha256="f" * 64,
        status="READY",
        ingest_status="READY",
    )
    second_version = DocumentVersion(
        id="version-2",
        document_id=second_document.id,
        version_number=1,
        storage_tier="WORKING_COPY",
        storage_path=f"其他/{first_copy.filename}",
        filename=first_copy.filename,
        content_type=second_document.content_type,
        size_bytes=second_document.size_bytes,
        sha256=second_document.sha256,
        source_type="IMPORT",
    )
    second_copy = WorkingCopy(
        id="working-copy-2",
        working_copy_root_id="working-root-1",
        workspace_id=first_copy.workspace_id,
        managed_file_id="managed-file-2",
        document_id=second_document.id,
        current_version_id=second_version.id,
        relative_path=f"其他/{first_copy.filename}",
        relative_path_hash="1" * 64,
        filename=first_copy.filename,
        extension=".docx",
        size_bytes=second_document.size_bytes,
        content_sha256=second_document.sha256,
        imported_source_sha256=second_document.sha256,
        status="ACTIVE",
    )
    db.add_all([second_document, second_version, second_copy])
    db.flush()
    service = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    )

    # 即使用户给的是完整文件名，也不能将两个同名副本合并总结。
    exact_selection = service.answer(question="完整总结申报通知.docx")
    assert exact_selection["kind"] == "file_selection"
    assert len(exact_selection["choices"]) == 2
    exact_clarification = FileSearchClarificationService(db).resolve(
        clarification_id=exact_selection["clarification_id"],
        user_id="user-1",
        option_id=exact_selection["choices"][0]["option_id"],
    )
    exact_plan = FileSearchClarificationPlanner(exact_clarification).plan()
    assert exact_plan.steps[0].input[
        "document_selection_clarification_id"
    ] == exact_selection["clarification_id"]

    # 用户已经选定一份同名文件后，原问题必须直接续跑，不能再次扩张为两个同名候选。
    resumed = service.answer(**exact_plan.steps[0].input)
    assert resumed["kind"] == "evidence_answer"
    assert resumed["status"] != "NEEDS_CLARIFICATION"

    selection = service._same_name_ambiguity(
        [(first_copy, db.get(DocumentVersion, "version-1")), (second_copy, second_version)],
        [],
        question="申报通知要求什么时候提交？",
    )

    assert selection is not None
    assert selection["kind"] == "file_selection"
    assert len(selection["choices"]) == 2
    clarification = FileSearchClarificationService(db).resolve(
        clarification_id=selection["clarification_id"],
        user_id="user-1",
        option_id=selection["choices"][1]["option_id"],
    )
    plan = FileSearchClarificationPlanner(clarification).plan()
    assert plan.intent == "EVIDENCE_ANSWER"
    assert plan.steps[0].tool_name == "evidence-answer"
    assert plan.steps[0].input["document_ids"] == ["document-2"]
    assert plan.steps[0].input[
        "document_selection_clarification_id"
    ] == selection["clarification_id"]


def test_evidence_answer_rejects_forged_document_selection_scope():
    """LLM 或客户端伪造选择凭据时不得把任意文件范围提升为已确认范围。"""

    db = _session()
    _seed(db)
    result = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    ).answer(
        question="总结申报通知.docx",
        document_ids=["document-1"],
        document_selection_clarification_id="forged-clarification",
    )

    assert result["ok"] is False
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "INVALID_DOCUMENT_SELECTION"


def test_document_selection_can_resume_original_summary_with_multiple_files():
    """用户可以选择多份候选，续跑时必须保留原总结意图和全部稳定文件 ID。"""

    db = _session()
    _seed(db)
    record = FileSearchClarificationService(db).create(
        conversation_id="conversation-1",
        user_id="user-1",
        agent_run_id=None,
        original_query="总结这些工作总结",
        core_phrase="工作总结",
        relation_mode="DOCUMENT_SELECTION",
        options=[
            {
                "id": "document-1",
                "label": "第一份工作总结.docx",
                "document_id": "document-1",
            },
            {
                "id": "document-2",
                "label": "第二份工作总结.docx",
                "document_id": "document-2",
            },
        ],
    )

    selection = FileSearchClarificationService(db).resolve(
        clarification_id=record.id,
        user_id="user-1",
        option_ids=["document-1", "document-2"],
    )
    plan = FileSearchClarificationPlanner(selection).plan()

    assert selection.document_ids == ("document-1", "document-2")
    assert plan.intent == "EVIDENCE_ANSWER"
    assert plan.steps[0].input["document_ids"] == ["document-1", "document-2"]
    assert plan.steps[0].input["question"] == "总结这些工作总结"
    assert plan.steps[0].input["answer_mode"] == "AUTO"
    assert plan.steps[0].input[
        "document_selection_clarification_id"
    ] == record.id


@pytest.mark.parametrize(
    ("question", "question_type", "answer_mode"),
    [
        ("这份通知什么时候截止？", "DATE_FACT", "FOCUSED"),
        ("文件文号是什么？", "DOCUMENT_NUMBER", "FOCUSED"),
        ("第六条规定了什么？", "CLAUSE", "FOCUSED"),
        ("完整总结这份文件", "SUMMARY", "FULL_SUMMARY"),
        ("总结申报通知.docx", "SUMMARY", "FOCUSED"),
        ("总结申报通知.docx，覆盖每个章节", "SUMMARY", "FULL_SUMMARY"),
        ("简要总结申报通知.docx", "SUMMARY", "FOCUSED"),
        ("请总结一下申报通知.docx", "SUMMARY", "FOCUSED"),
        ("比较这两份方案的差异", "COMPARE", "FOCUSED"),
        ("汇总表格中的总金额", "TABLE_CALCULATION", "FOCUSED"),
        ("联网搜索实时天气", "UNSUPPORTED", "FOCUSED"),
    ],
)
def test_evidence_question_policy_is_deterministic(
    question: str,
    question_type: str,
    answer_mode: str,
):
    """问题策略必须由确定性规则选择，不把日期、条款或计算交给模型猜测。"""

    decision = EvidenceQuestionPolicy().decide(question=question)

    assert decision.question_type == question_type
    assert decision.answer_mode == answer_mode
    assert decision.deterministic_calculation_required is (
        question_type == "TABLE_CALCULATION"
    )


def test_recalled_candidate_expands_same_name_active_working_copies():
    """文件级 Top-K 只召回一个同名文件时，也必须补齐同名候选并等待用户选择。"""

    db = _session()
    first_copy, first_version = _seed(db)
    second_document = Document(
        id="document-2",
        user_id="user-1",
        workspace_id=first_copy.workspace_id,
        original_filename=first_copy.filename,
        content_type="application/pdf",
        size_bytes=256,
        sha256="f" * 64,
        status="READY",
        ingest_status="READY",
    )
    second_version = DocumentVersion(
        id="version-2",
        document_id=second_document.id,
        version_number=1,
        storage_tier="WORKING_COPY",
        storage_path=f"其他/{first_copy.filename}",
        filename=first_copy.filename,
        content_type=second_document.content_type,
        size_bytes=second_document.size_bytes,
        sha256=second_document.sha256,
        source_type="IMPORT",
    )
    second_copy = WorkingCopy(
        id="working-copy-2",
        working_copy_root_id="working-root-1",
        workspace_id=first_copy.workspace_id,
        managed_file_id="managed-file-2",
        document_id=second_document.id,
        current_version_id=second_version.id,
        relative_path=f"其他/{first_copy.filename}",
        relative_path_hash="1" * 64,
        filename=first_copy.filename,
        extension=".docx",
        size_bytes=second_document.size_bytes,
        content_sha256=second_document.sha256,
        imported_source_sha256=second_document.sha256,
        status="ACTIVE",
    )
    db.add_all([second_document, second_version, second_copy])
    db.flush()
    service = EvidenceAnswerService(
        db=db,
        user_id="user-1",
        conversation_id="conversation-1",
        settings=_settings(),
        client=FakeEvidenceClient(),
    )

    expanded = service._expand_same_name_rows([(first_copy, first_version)])

    assert {working_copy.id for working_copy, _ in expanded} == {
        "working-copy-1",
        "working-copy-2",
    }


def test_history_refresh_marks_evidence_file_as_trashed():
    """历史回答引用后来进入回收站时必须禁用查看，不能继续沿用 AVAILABLE。"""

    receipt = UserTaskReceipt(
        task_id="run-1",
        task_status="completed",
        response_type="evidence_answer",
        evidence_answer_result={
            "answer_id": "answer-1",
            "status": "COMPLETED",
            "answer": "结论。[1]",
            "limitations": [],
            "files": [
                {
                    "document_id": "document-1",
                    "filename": "申报通知.docx",
                    "availability": "AVAILABLE",
                    "reference_indexes": [1],
                }
            ],
            "cached": False,
        },
    )
    refreshed = ConversationRepository._refresh_evidence_file_availability(
        receipt=receipt,
        availability_map={
            "document-1": AttachmentAvailabilityProjection(
                working_copy_id="working-copy-1",
                working_copy_status="TRASHED",
                file_availability="TRASHED",
                availability_message="已删除（在回收站，可恢复）",
                can_open=False,
                can_restore=True,
            )
        },
    )

    file = refreshed.evidence_answer_result["files"][0]
    assert file["availability"] == "TRASHED"
    assert file["can_open"] is False
    assert file["can_restore"] is True


def test_history_collects_document_ids_from_evidence_tool_only():
    """历史状态刷新只能读取 evidence-answer 已持久化引用中的文件 ID。"""

    result = SimpleNamespace(
        tool_invocations=[
            SimpleNamespace(
                tool_name="evidence-answer",
                output_json={
                    "references": [
                        {"document_id": "document-1"},
                        {"document_id": "document-1"},
                        {"document_id": "document-2"},
                    ]
                },
            ),
            SimpleNamespace(
                tool_name="other-tool",
                output_json={"references": [{"document_id": "document-hidden"}]},
            ),
        ]
    )

    assert _evidence_reference_document_ids([result]) == {
        "document-1",
        "document-2",
    }


def test_ready_managed_source_answers_without_working_copy_and_queues_materialization():
    """已完成源侧分析的首次内容问答必须直接回答，并物化该相关源文件。"""

    db = _session()
    user = User(id="source-user", username="source-user", password_hash="x")
    shared = Workspace(
        id="source-workspace",
        name="共享工作区",
        workspace_type=SHARED_WORKSPACE_TYPE,
        system_key=SHARED_WORKSPACE_SYSTEM_KEY,
    )
    conversation = Conversation(
        id="source-conversation",
        user_id=user.id,
        workspace_id=shared.id,
        title="源侧证据测试",
    )
    root = ManagedRoot(
        id="source-root",
        root_key="source-root",
        display_name="源目录",
        container_path="/managed/source-root",
        enabled=True,
    )
    managed_file = ManagedFile(
        id="source-file",
        root_id=root.id,
        relative_path="劳务/劳务费发放说明.txt",
        relative_path_hash="source-path-hash",
        filename="劳务费发放说明.txt",
        extension=".txt",
        size_bytes=32,
        fingerprint="source-fingerprint",
        content_sha256="f" * 64,
        status="ACTIVE",
    )
    source_document = Document(
        id="source-document",
        user_id=user.id,
        workspace_id=None,
        original_filename=managed_file.filename,
        content_type="text/plain",
        size_bytes=32,
        sha256="f" * 64,
        status="MANAGED_SOURCE_ANALYSIS",
        ingest_status="INDEXED",
    )
    source_version = DocumentVersion(
        id="source-version",
        document_id=source_document.id,
        version_number=1,
        storage_tier="MANAGED_SOURCE",
        storage_path="managed-source://source-revision",
        filename=managed_file.filename,
        content_type="text/plain",
        size_bytes=32,
        sha256="f" * 64,
        source_type="MANAGED_SOURCE_ANALYSIS",
        source_managed_file_id=managed_file.id,
        source_managed_file_revision_id="source-revision",
    )
    revision = ManagedFileRevision(
        id="source-revision",
        managed_file_id=managed_file.id,
        revision_number=1,
        size_bytes=32,
        quick_fingerprint=managed_file.fingerprint,
        content_sha256="f" * 64,
        status="READY",
        analysis_status="READY",
        is_current=True,
        analysis_document_id=source_document.id,
        analysis_document_version_id=source_version.id,
    )
    extraction = DocumentExtractionRun(
        id="source-extraction",
        document_id=source_document.id,
        document_version_id=source_version.id,
        status="COMPLETED",
    )
    index_run = DocumentIndexRun(
        id="source-index",
        document_id=source_document.id,
        document_version_id=source_version.id,
        extraction_run_id=extraction.id,
        index_version="document-chunk-index-v2",
        tokenizer="jieba",
        tokenizer_version="test",
        config_hash="s" * 64,
        status="COMPLETED",
        chunk_count=1,
        evidence_count=1,
    )
    chunk = DocumentChunk(
        id="source-chunk",
        index_run_id=index_run.id,
        document_id=source_document.id,
        document_version_id=source_version.id,
        extraction_run_id=extraction.id,
        chunk_index=0,
        chunk_type="text",
        text_content="劳务费按照审核后的名单发放。",
        search_text="劳务费 审核 名单 发放",
        content_hash="t" * 64,
        location_hash="u" * 64,
        char_count=14,
        token_count=4,
        page_start=1,
        page_end=1,
    )
    evidence = EvidenceSpan(
        id="source-evidence",
        chunk_id=chunk.id,
        document_id=source_document.id,
        document_version_id=source_version.id,
        extraction_run_id=extraction.id,
        span_index=0,
        evidence_type="text_quote",
        quote=chunk.text_content,
        start_offset=0,
        end_offset=len(chunk.text_content),
        page_number=1,
        source="document_chunk",
    )
    analysis = ManagedFileAnalysisRun(
        id="source-analysis",
        managed_file_revision_id=revision.id,
        status="COMPLETED",
        extraction_run_id=extraction.id,
        index_run_id=index_run.id,
        parser_name="plain-text",
        parser_version="test",
    )
    db.add_all(
        [
            user,
            shared,
            conversation,
            root,
            managed_file,
            source_document,
            source_version,
            revision,
            extraction,
            index_run,
            chunk,
            evidence,
            analysis,
        ]
    )
    db.flush()

    result = EvidenceAnswerService(
        db=db,
        user_id=user.id,
        conversation_id=conversation.id,
        settings=_settings(),
        client=EchoEvidenceClient(),
    ).answer(
        question="劳务费发放说明.txt 总结文件内容",
        document_ids=[source_document.id],
    )

    assert result["status"] == "COMPLETED"
    assert result["references"][0]["availability"] == "SOURCE_ANALYZED"
    assert result["references"][0]["can_open"] is False
    materialization_jobs = db.query(FilesystemJob).filter(
        FilesystemJob.job_type == "MATERIALIZE_WORKING_COPY"
    ).all()
    assert len(materialization_jobs) == 1
    assert materialization_jobs[0].payload_json["managed_file_revision_id"] == revision.id
    db.close()
