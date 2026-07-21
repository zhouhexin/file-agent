"""工作副本摘要优先检索测试。"""

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
    """创建隔离数据库，保护用户边界和摘要版本约束。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _add_working_copy(
    db,
    *,
    suffix: str,
    user_id: str,
    filename: str,
    overview: str,
    category_path: list[str],
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
            status="ACTIVE",
        )
    )
    db.add(
        DocumentSummary(
            id=f"summary-{suffix}",
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=f"extraction-{suffix}",
            input_sha256=document.sha256,
            summary_text=overview,
            summary_json={
                "overview": overview,
                "key_points": [
                    {
                        "text": overview,
                        "evidence_refs": [
                            {"page_number": 1, "sheet_name": None, "quote": overview[:30]}
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


def test_search_uses_final_filename_summary_and_category_without_crossing_user_boundary():
    """对话检索必须命中整理后的文件，并且绝不返回其他用户的同主题文件。"""

    db = _db_session()
    try:
        scholarship = _add_working_copy(
            db,
            suffix="a",
            user_id="user-a",
            filename="2025年度国家励志奖学金活动评审通知.docx",
            overview="本通知安排2025年度国家励志奖学金评审活动和申请材料提交。",
            category_path=["学校", "学生工作", "奖助学金"],
        )
        _add_working_copy(
            db,
            suffix="b",
            user_id="user-a",
            filename="2025年度军转干部考察结果报告.docx",
            overview="本报告记录三名军转干部的组织考察结论。",
            category_path=["学校", "党委相关", "组织"],
        )
        _add_working_copy(
            db,
            suffix="c",
            user_id="user-b",
            filename="国家励志奖学金内部名单.docx",
            overview="其他用户的国家励志奖学金名单。",
            category_path=["学校", "学生工作", "奖助学金"],
        )
        db.commit()

        payload = WorkingCopySummarySearchService(db=db, user_id="user-a").search(
            query="找我去年活动相关的奖学金材料"
        )

        assert payload["ok"] is True
        assert payload["results"]
        assert payload["results"][0]["document_id"] == scholarship.id
        assert payload["results"][0]["filename"] == "2025年度国家励志奖学金活动评审通知.docx"
        assert payload["results"][0]["category_path"] == ["学校", "学生工作", "奖助学金"]
        assert all(item["document_id"] != "document-c" for item in payload["results"])
        assert "internal-source" not in str(payload)
        assert payload["results"][0]["evidence_refs"][0]["page_number"] == 1
    finally:
        db.close()


def test_search_can_be_restricted_to_backend_resolved_document_ids():
    """附件或会话范围由后端解析后，检索不得越过给定 document_ids。"""

    db = _db_session()
    try:
        first = _add_working_copy(
            db,
            suffix="d",
            user_id="user-a",
            filename="奖学金申请通知.docx",
            overview="奖学金申请安排。",
            category_path=["学校", "学生工作"],
        )
        second = _add_working_copy(
            db,
            suffix="e",
            user_id="user-a",
            filename="奖学金评审办法.docx",
            overview="奖学金评审办法。",
            category_path=["学校", "学生工作"],
        )
        db.commit()

        payload = WorkingCopySummarySearchService(db=db, user_id="user-a").search(
            query="查找奖学金文件",
            document_ids=[second.id],
        )

        assert [item["document_id"] for item in payload["results"]] == [second.id]
        assert first.id not in [item["document_id"] for item in payload["results"]]
    finally:
        db.close()

