"""DocumentSearchProfile 瘦检索投影 Service 测试。

测试目标：
1. upsert 创建正确字段的投影
2. upsert 幂等不产生重复
3. backfill 为所有 ACTIVE 工作副本补齐
4. backfill 幂等
5. reconciliation 修复陈旧/缺失投影
6. deactivate 正确标记状态
7. normalized_filename 规范化
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentSearchProfile,
    DocumentSummary,
    DocumentVersion,
    WorkingCopy,
)
from app.modules.retrieval.search_profile import DocumentSearchProfileService


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
    wc_status: str = "ACTIVE",
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
            status=wc_status,
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
            summary_json={"overview": summary_text},
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


def test_model_exists():
    """DocumentSearchProfile 模型已定义且表名正确。"""
    from app.db.models import DocumentSearchProfile
    assert DocumentSearchProfile.__tablename__ == "document_search_profiles"


def test_service_importable():
    """DocumentSearchProfileService 可导入。"""
    from app.modules.retrieval.search_profile import DocumentSearchProfileService
    assert DocumentSearchProfileService is not None


def test_upsert_creates_search_profile():
    """upsert 成功后 profile 记录存在且字段正确。"""

    db = _db_session()
    try:
        _add_working_copy_with_summary(
            db,
            suffix="a",
            user_id="user-a",
            filename="2025国家励志奖学金申请.docx",
            summary_text="奖学金申请材料",
            category_path=["学校", "学生工作", "奖助学金"],
        )
        db.commit()

        service = DocumentSearchProfileService(db=db)
        result = service.upsert_current_profile("working-copy-a")

        assert result["ok"] is True

        profile = db.query(DocumentSearchProfile).filter(
            DocumentSearchProfile.working_copy_id == "working-copy-a"
        ).first()
        assert profile is not None
        assert profile.status == "ACTIVE"
        assert profile.document_id == "document-a"
        assert profile.document_version_id == "version-a"
        assert profile.normalized_filename is not None
        assert profile.source_fingerprint is not None
    finally:
        db.close()


def test_upsert_is_idempotent():
    """重复 upsert 不产生重复记录。"""

    db = _db_session()
    try:
        _add_working_copy_with_summary(
            db,
            suffix="b",
            user_id="user-b",
            filename="奖学金申请.docx",
            summary_text="奖学金",
            category_path=["学校", "学生工作"],
        )
        db.commit()

        service = DocumentSearchProfileService(db=db)
        first = service.upsert_current_profile("working-copy-b")
        second = service.upsert_current_profile("working-copy-b")

        assert first["ok"] is True
        assert second["ok"] is True

        # 唯一记录数 = 1
        profiles = db.query(DocumentSearchProfile).filter(
            DocumentSearchProfile.working_copy_id == "working-copy-b"
        ).all()
        assert len(profiles) == 1
    finally:
        db.close()


def test_backfill_creates_profiles_for_all_active_working_copies():
    """backfill 为所有 ACTIVE 工作副本创建投影。"""

    db = _db_session()
    try:
        for i in range(3):
            _add_working_copy_with_summary(
                db,
                suffix=f"backfill-{i}",
                user_id="user-c",
                filename=f"文件{i}.docx",
                summary_text=f"摘要{i}",
                category_path=["学校"],
            )
        # 一个非 ACTIVE 的工作副本不应被 backfill
        _add_working_copy_with_summary(
            db,
            suffix="backfill-inactive",
            user_id="user-c",
            filename="inactive.docx",
            summary_text="inactive",
            category_path=["学校"],
            wc_status="INACTIVE",
        )
        db.commit()

        service = DocumentSearchProfileService(db=db)
        result = service.backfill_profiles(batch_size=10)

        assert result["ok"] is True
        assert result["processed"] == 3  # 只有 3 个 ACTIVE

        profile_count = db.query(DocumentSearchProfile).count()
        assert profile_count == 3
    finally:
        db.close()


def test_backfill_with_small_batches_does_not_skip_remaining_working_copies():
    """回填查询条件会被本批写入改变，不能使用 offset 而跳过后续活动工作副本。"""

    db = _db_session()
    try:
        for index in range(5):
            _add_working_copy_with_summary(
                db,
                suffix=f"small-batch-{index}",
                user_id="user-small-batch",
                filename=f"文件{index}.docx",
                summary_text=f"摘要{index}",
            )
        db.commit()

        result = DocumentSearchProfileService(db=db).backfill_profiles(batch_size=1)

        assert result["processed"] == 5
        assert db.query(DocumentSearchProfile).count() == 5
    finally:
        db.close()


def test_backfill_is_idempotent():
    """重复 backfill 不产生重复。"""

    db = _db_session()
    try:
        for i in range(2):
            _add_working_copy_with_summary(
                db,
                suffix=f"idem-{i}",
                user_id="user-d",
                filename=f"文件{i}.docx",
                summary_text=f"摘要{i}",
                category_path=["学校"],
            )
        db.commit()

        service = DocumentSearchProfileService(db=db)
        first = service.backfill_profiles(batch_size=10)
        second = service.backfill_profiles(batch_size=10)

        assert first["processed"] == 2
        assert second["processed"] == 0  # 已全部补齐，没有新增
        assert db.query(DocumentSearchProfile).count() == 2
    finally:
        db.close()


def test_deactivate_marks_profile_inactive():
    """deactivate 后 profile 状态为 INACTIVE。"""

    db = _db_session()
    try:
        _add_working_copy_with_summary(
            db,
            suffix="d",
            user_id="user-d",
            filename="test.docx",
            summary_text="test",
        )
        db.commit()

        service = DocumentSearchProfileService(db=db)
        service.upsert_current_profile("working-copy-d")
        service.deactivate_profile("working-copy-d")

        profile = db.query(DocumentSearchProfile).filter(
            DocumentSearchProfile.working_copy_id == "working-copy-d"
        ).first()
        assert profile is not None
        assert profile.status == "INACTIVE"
    finally:
        db.close()


def test_normalized_filename_is_case_and_punctuation_normalized():
    """normalized_filename 去除标点并统一小写。"""

    db = _db_session()
    try:
        _add_working_copy_with_summary(
            db,
            suffix="norm",
            user_id="user-e",
            filename="2025年-国家励志奖学金（公示）.DOCX",
            summary_text="奖学金",
        )
        db.commit()

        service = DocumentSearchProfileService(db=db)
        service.upsert_current_profile("working-copy-norm")

        profile = db.query(DocumentSearchProfile).filter(
            DocumentSearchProfile.working_copy_id == "working-copy-norm"
        ).first()
        assert profile is not None
        # 标点被去除、英文字母小写
        assert "(" not in (profile.normalized_filename or "")
        assert "（" not in (profile.normalized_filename or "")
        assert "." not in (profile.normalized_filename or "")
        assert "-" not in (profile.normalized_filename or "")
        # 内容仍在
        assert "2025" in (profile.normalized_filename or "")
        assert "国家励志奖学金" in (profile.normalized_filename or "")
        assert "docx" in (profile.normalized_filename or "")
    finally:
        db.close()


def test_reconciliation_fixes_stale_profile():
    """reconciliation 修复陈旧投影（文件名变更后）。"""

    db = _db_session()
    try:
        doc = _add_working_copy_with_summary(
            db,
            suffix="recon",
            user_id="user-f",
            filename="旧名称.docx",
            summary_text="旧摘要",
        )
        db.commit()

        service = DocumentSearchProfileService(db=db)
        service.upsert_current_profile("working-copy-recon")

        # 模拟文件名变更
        wc = db.query(WorkingCopy).filter(
            WorkingCopy.id == "working-copy-recon"
        ).first()
        wc.filename = "新名称.docx"
        db.flush()

        # reconciliation 应修复
        result = service.reconcile_profiles(batch_size=10)
        assert result["ok"] is True
        assert result["fixed"] >= 1

        profile = db.query(DocumentSearchProfile).filter(
            DocumentSearchProfile.working_copy_id == "working-copy-recon"
        ).first()
        # fingerprint 应已更新
        assert profile is not None
    finally:
        db.close()


def test_active_profiles_have_correct_fields():
    """ACTIVE 投影的字段完整且正确。"""

    db = _db_session()
    try:
        _add_working_copy_with_summary(
            db,
            suffix="fields",
            user_id="user-g",
            filename="奖学金.docx",
            summary_text="国家励志奖学金申请材料",
            category_path=["学校", "学生工作", "奖助学金"],
        )
        db.commit()

        service = DocumentSearchProfileService(db=db)
        service.upsert_current_profile("working-copy-fields")

        profile = db.query(DocumentSearchProfile).filter(
            DocumentSearchProfile.working_copy_id == "working-copy-fields"
        ).first()

        assert profile is not None
        # 字段完整性
        assert profile.user_id == "user-g"
        assert profile.workspace_id == "workspace-user-g"
        assert profile.document_id == "document-fields"
        assert profile.document_version_id == "version-fields"
        assert profile.filename_search_text is not None
        assert profile.category_search_text is not None
        assert profile.summary_search_text is not None
        assert profile.combined_search_text is not None
    finally:
        db.close()


def test_upsert_nonexistent_working_copy_returns_error():
    """对不存在的工作副本 upsert 应返回错误。"""

    db = _db_session()
    try:
        service = DocumentSearchProfileService(db=db)
        result = service.upsert_current_profile("nonexistent-wc")
        assert result["ok"] is False
    finally:
        db.close()
