"""受管源目录解析与分类分离开关的回归测试。"""

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.base import Base
from app.db.models import (
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    FilesystemJob,
    ManagedFile,
    ManagedFileRevision,
    ManagedRoot,
)
from app.modules.managed_files import source_analysis
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.worker import process_next_filesystem_job


def _session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_parse_only_mode_keeps_extraction_and_index_but_writes_no_classification(
    monkeypatch, tmp_path
):
    """关闭分类时，受管源文件仍可解析和索引，且不得触及任何分类入口。"""

    monkeypatch.setenv("MANAGED_SOURCE_CLASSIFICATION_ENABLED", "false")
    get_settings.cache_clear()
    source_path = tmp_path / "奖学金申请表.txt"
    source_path.write_text("2025 年奖学金申请材料", encoding="utf-8")
    stat = source_path.stat()
    db = _session()
    try:
        root = ManagedRoot(
            id="parse-only-root",
            root_key="parse-only-root",
            display_name="parse-only-root",
            container_path=str(tmp_path),
            enabled=True,
            created_by="parse-only-user",
        )
        managed_file = ManagedFile(
            id="parse-only-file",
            root_id=root.id,
            relative_path=source_path.name,
            relative_path_hash="parse-only-path",
            filename=source_path.name,
            extension=".txt",
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            fingerprint="parse-only-fingerprint",
            status="ACTIVE",
        )
        revision = ManagedFileRevision(
            id="parse-only-revision",
            managed_file_id=managed_file.id,
            revision_number=1,
            size_bytes=stat.st_size,
            modified_at=managed_file.modified_at,
            quick_fingerprint=managed_file.fingerprint,
            status="ANALYSIS_PENDING",
            analysis_status="PENDING",
            is_current=True,
        )
        db.add_all([root, managed_file, revision])
        db.flush()

        indexed: list[str] = []

        def fail_if_classification_runs(*_args, **_kwargs):
            raise AssertionError("解析期关闭分类后不应调用分类相关能力")

        monkeypatch.setattr(
            source_analysis.ManagedPurposePackageInferenceService,
            "resolve",
            fail_if_classification_runs,
        )
        monkeypatch.setattr(
            source_analysis.ClassificationRuntimeFactory,
            "create",
            fail_if_classification_runs,
        )
        monkeypatch.setattr(
            source_analysis.DocumentIndexService,
            "build",
            lambda _service, **kwargs: indexed.append(str(kwargs["document_id"]))
            or {"ok": True, "status": "COMPLETED", "index_run_id": "parse-only-index"},
        )

        service = source_analysis.ManagedSourceAnalysisService(
            db=db,
            settings=get_settings(),
        )
        result = service.analyze(revision_id=revision.id, user_id="parse-only-user")

        assert result["status"] == "READY"
        assert result["classification"] is None
        assert result["classification_skipped"] is True
        assert indexed == [result["document_id"]]
        assert db.query(DocumentClassificationRun).count() == 0
        assert db.query(DocumentCategorySuggestion).count() == 0

        refresh = service.refresh_classification(
            revision_id=revision.id,
            user_id="parse-only-user",
        )
        assert refresh["classification_skipped"] is True
        assert refresh["reused_extraction"] is True
    finally:
        db.close()
        get_settings.cache_clear()


def test_queued_classification_refresh_is_completed_without_execution_when_disabled(
    monkeypatch,
):
    """切换到仅解析模式后，旧的分类刷新任务不得继续写入分类建议。"""

    monkeypatch.setenv("MANAGED_SOURCE_CLASSIFICATION_ENABLED", "false")
    get_settings.cache_clear()
    db = _session()
    try:
        job = FilesystemJobQueue(db).create_job(
            job_type="REFRESH_MANAGED_SOURCE_CLASSIFICATION",
            queue_name="SOURCE_ANALYSIS",
            root_id=None,
            created_by="parse-only-user",
            deduplication_key="parse-only-refresh-job",
            payload={"managed_file_revision_id": "parse-only-revision"},
        )
        db.commit()

        session_factory = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)
        processed = process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="parse-only-worker",
            queue_names={"SOURCE_ANALYSIS"},
        )

        assert processed == job.id
        refreshed = db.get(FilesystemJob, job.id)
        assert refreshed is not None
        db.refresh(refreshed)
        assert refreshed.status == "COMPLETED"
        assert refreshed.result_json["classification_skipped"] is True
    finally:
        db.close()
        get_settings.cache_clear()
