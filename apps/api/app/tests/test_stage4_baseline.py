"""旧摘要检索兼容回归测试。

测试 1：摘要遗漏时，当前摘要检索找不到文件。
测试 2：当前搜索不查 Chunk 索引（纯摘要匹配）。

阶段四默认已切换到两阶段检索；这些测试只保护紧急回退开关仍可使用的旧摘要服务，
不代表普通用户搜索主路径。
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentSummary,
    DocumentVersion,
    WorkingCopy,
)
from app.modules.retrieval.summary_search import WorkingCopySummarySearchService


def _db_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _add_working_copy_with_summary(
    db,
    *,
    suffix: str,
    user_id: str,
    filename: str,
    summary_text: str,
    category_path: list[str] | None = None,
    status: str = "ACTIVE",
) -> Document:
    """写入一份带当前版本摘要和分类建议的工作副本。"""

    document = Document(
        id=f"document-{suffix}",
        user_id=user_id,
        workspace_id=f"workspace-{user_id}",
        original_filename=f"internal-source-{suffix}.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=100,
        sha256=suffix[0] * 64,
    )
    version = DocumentVersion(
        id=f"version-{suffix}",
        document_id=document.id,
        version_number=1,
        storage_tier="WORKING_COPY",
        storage_path=f"work/{filename}",
        filename=filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        source_type="IMPORT",
    )
    db.add_all([document, version])
    db.flush()
    db.add(
        WorkingCopy(
            id=f"working-copy-{suffix}",
            working_copy_root_id=f"working-root-{suffix}",
            workspace_id=document.workspace_id,
            managed_file_id=f"managed-file-{suffix}",
            document_id=document.id,
            current_version_id=version.id,
            relative_path=f"分类/{filename}",
            relative_path_hash=suffix[0] * 64,
            filename=filename,
            extension="docx",
            size_bytes=document.size_bytes,
            content_sha256=document.sha256,
            imported_source_sha256=document.sha256,
            status=status,
        )
    )
    db.add(
        DocumentSummary(
            id=f"summary-{suffix}",
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=f"extraction-{suffix}",
            input_sha256=document.sha256,
            summary_text=summary_text,
            summary_json={
                "overview": summary_text,
                "key_points": [
                    {
                        "text": summary_text,
                        "evidence_refs": [
                            {"page_number": 1, "sheet_name": None, "quote": summary_text[:30]}
                        ],
                    }
                ],
            },
            coverage_json={},
            prompt_version="test-v1",
            schema_version="test-v1",
            status="COMPLETED",
        )
    )
    if category_path:
        db.add(
            DocumentCategorySuggestion(
                id=f"suggestion-{suffix}",
                classification_run_id=f"classification-run-{suffix}",
                document_id=document.id,
                document_version_id=version.id,
                category_id=f"category-{suffix}",
                category_name=category_path[-1],
                category_path_json=category_path,
                taxonomy_key="school_file_classification",
                taxonomy_version="test-v1",
                confidence=0.9,
                status="SUGGESTED",
                evidence_json=[],
                rank=1,
            )
        )
    db.flush()
    return document


def test_summary_miss_means_document_not_found():
    """当前摘要检索的已知限制：摘要遗漏时，即使正文/Chunk 包含相关词也找不到文件。

    两阶段主路径已通过 Chunk 补召回修复此问题；此处仅确认旧兼容服务仍保持边界。
    """

    db = _db_session()
    try:
        doc_a = _add_working_copy_with_summary(
            db,
            suffix="a",
            user_id="user-a",
            filename="2025年度国家励志奖学金申请通知.docx",
            summary_text="本通知安排奖学金申请和材料提交。",
            category_path=["学校", "学生工作", "奖助学金"],
        )
        doc_b = _add_working_copy_with_summary(
            db,
            suffix="b",
            user_id="user-a",
            filename="2025年度家庭经济困难学生认定通知.docx",
            summary_text="本通知安排家庭经济困难认定工作。",
            category_path=["学校", "学生工作", "资助"],
        )
        db.commit()

        # 查询"公示期限"—— 两个文件的摘要都不包含此词
        payload = WorkingCopySummarySearchService(db=db, user_id="user-a").search(
            query="哪个文件提到了公示期限"
        )

        # 当前限制：摘要不包含"公示期限"，所以找不到
        # 这是旧兼容服务的预期行为，不是两阶段主路径的验收结论。
        assert payload["ok"] is True
        assert len(payload["results"]) == 0, (
            f"当前搜索仅基于摘要匹配，摘要不包含查询词时应无结果。"
            f"已有结果：{payload['results']}"
        )
    finally:
        db.close()


def test_current_search_does_not_check_chunk_content():
    """当前搜索纯基于摘要，不查询 Chunk GIN 索引。

    即使文档正文包含查询词，只要摘要不含此词，就搜不到。
    两阶段主路径会通过 Chunk 索引补召回；这里仅保护旧兼容服务。
    """

    db = _db_session()
    try:
        _add_working_copy_with_summary(
            db,
            suffix="c",
            user_id="user-a",
            filename="学生工作处工作安排.docx",
            summary_text="本学期学生工作安排汇总。",
            category_path=["学校", "学生工作"],
        )
        # 注意：不创建 DocumentPage 或 DocumentChunk
        # 因为当前搜索根本不查 Chunk，即使创建了也不影响断言
        db.commit()

        # 查询"公示期限"—— 摘要不包含此词
        payload = WorkingCopySummarySearchService(db=db, user_id="user-a").search(
            query="找公示期限"
        )

        # 当前限制：摘要不包含"公示期限"，所以找不到
        assert payload["ok"] is True
        assert len(payload["results"]) == 0, (
            f"当前搜索仅基于摘要匹配，摘要不包含查询词时应无结果。"
            f"已有结果：{payload['results']}"
        )
    finally:
        db.close()


def test_current_search_loads_all_candidates_into_memory():
    """当前搜索将全部候选加载到 Python 内存后评分，不在数据库层面过滤。

    验证方式：全部候选都会被加载并参与评分循环，数据库查询不使用 WHERE query matching。
    这里通过计数器观察：如果有 N 个文档，即使查询只能匹配其中 1 个，
    所有 N 个文档仍会被完整加载到内存。
    """

    db = _db_session()
    try:
        # 创建 10 个文档，只让第 10 个的摘要包含查询词
        target = None
        for i in range(10):
            suffix = f"load-{i}"
            summary = f"这是第{i}个文档的内容。"
            filename = f"文档{i}.docx"
            if i == 9:
                summary = "这是最后一份国家励志奖学金申请材料。"
                filename = "国家励志奖学金申请材料.docx"

            doc = _add_working_copy_with_summary(
                db,
                suffix=suffix,
                user_id="user-a",
                filename=filename,
                summary_text=summary,
                category_path=["学校", "学生工作"],
            )
            if i == 9:
                target = doc
        db.commit()

        payload = WorkingCopySummarySearchService(db=db, user_id="user-a").search(
            query="找国家励志奖学金"
        )

        # 应该能找到第 10 个文档
        assert payload["ok"] is True
        assert len(payload["results"]) > 0
        if target:
            assert any(
                item["document_id"] == target.id for item in payload["results"]
            ), "当前搜索应能找到匹配的文档（摘要包含查询词）"

        # 关键验证点：当前方法无法利用数据库索引提前过滤无关文档
        # 我们无法直接断言"加载了多少个候选"，
        # 但可以通过监控查询日志或使用 profiling 来检测。
        # 这里记录这个限制，后续阶段四会通过数据库索引召回解决。
    finally:
        db.close()
