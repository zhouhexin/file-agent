"""验证受管文件失败任务的显式、可审计重试边界。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import (
    Base,
    FilesystemJob,
    FilesystemJobEvent,
    ManagedFile,
    ManagedFileRevision,
    ManagedRoot,
)
from app.scripts.retry_failed_managed_jobs import retry_failed_managed_jobs


def _database_session():
    """创建独立数据库，避免重试测试污染其他用例。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_retry_failed_managed_jobs_reopens_only_safe_root_jobs():
    """显式重试只处理目标根，并跳过缺失源和未就绪物化任务。"""

    db = _database_session()
    try:
        root = ManagedRoot(
            root_key="workdata",
            display_name="受管根",
            container_path="/managed/workdata",
        )
        other_root = ManagedRoot(
            root_key="other",
            display_name="其他根",
            container_path="/managed/other",
        )
        db.add_all([root, other_root])
        db.flush()
        managed_file = ManagedFile(
            root_id=root.id,
            relative_path="学院/材料.xlsx",
            filename="材料.xlsx",
            extension=".xlsx",
        )
        other_file = ManagedFile(
            root_id=other_root.id,
            relative_path="其他/文件.xlsx",
            filename="文件.xlsx",
            extension=".xlsx",
        )
        db.add_all([managed_file, other_file])
        db.flush()
        ready_revision = ManagedFileRevision(
            managed_file_id=managed_file.id,
            revision_number=1,
            quick_fingerprint="ready",
            status="READY",
            analysis_status="READY",
            is_current=True,
        )
        pending_revision = ManagedFileRevision(
            managed_file_id=managed_file.id,
            revision_number=2,
            quick_fingerprint="pending",
            status="ANALYSIS_PENDING",
            analysis_status="PENDING",
            is_current=False,
        )
        other_revision = ManagedFileRevision(
            managed_file_id=other_file.id,
            revision_number=1,
            quick_fingerprint="other",
            status="READY",
            analysis_status="READY",
            is_current=True,
        )
        db.add_all([ready_revision, pending_revision, other_revision])
        db.flush()

        retryable_analysis = FilesystemJob(
            job_type="ANALYZE_MANAGED_FILE_REVISION",
            queue_name="SOURCE_ANALYSIS",
            root_id=root.id,
            status="FAILED",
            deduplication_key=f"analysis:{ready_revision.id}",
            payload_json={"managed_file_revision_id": ready_revision.id},
            error_message="受管原始文件分析失败",
            attempt_count=3,
        )
        retryable_materialization = FilesystemJob(
            job_type="MATERIALIZE_WORKING_COPY",
            queue_name="MATERIALIZE",
            status="FAILED",
            deduplication_key=f"materialize:{ready_revision.id}",
            payload_json={"managed_file_revision_id": ready_revision.id},
            error_message="变更审计写入失败",
            attempt_count=3,
        )
        missing_source = FilesystemJob(
            job_type="ANALYZE_MANAGED_FILE_REVISION",
            queue_name="SOURCE_ANALYSIS",
            root_id=root.id,
            status="FAILED",
            deduplication_key=f"missing:{ready_revision.id}",
            payload_json={"managed_file_revision_id": ready_revision.id},
            error_message="relative_path 指向的文件不存在。",
        )
        premature_materialization = FilesystemJob(
            job_type="MATERIALIZE_WORKING_COPY",
            queue_name="MATERIALIZE",
            status="FAILED",
            deduplication_key=f"materialize:{pending_revision.id}",
            payload_json={"managed_file_revision_id": pending_revision.id},
            error_message="缺少已完成分析的当前原始文件修订",
        )
        unrelated = FilesystemJob(
            job_type="ANALYZE_MANAGED_FILE_REVISION",
            queue_name="SOURCE_ANALYSIS",
            root_id=other_root.id,
            status="FAILED",
            deduplication_key=f"analysis:{other_revision.id}",
            payload_json={"managed_file_revision_id": other_revision.id},
            error_message="其他根失败",
        )
        db.add_all(
            [
                retryable_analysis,
                retryable_materialization,
                missing_source,
                premature_materialization,
                unrelated,
            ]
        )
        db.commit()

        preview = retry_failed_managed_jobs(
            db=db,
            root_key="workdata",
            apply=False,
        )
        assert preview["candidate_count"] == 2
        assert preview["skipped_counts"] == {
            "SOURCE_ANALYSIS_NOT_READY": 1,
            "SOURCE_MISSING": 1,
        }
        assert retryable_analysis.status == "FAILED"

        result = retry_failed_managed_jobs(
            db=db,
            root_key="workdata",
            apply=True,
        )
        assert result["retried_count"] == 2
        assert set(result["job_ids"]) == {
            retryable_analysis.id,
            retryable_materialization.id,
        }
        assert retryable_analysis.status == "PENDING"
        assert retryable_materialization.status == "PENDING"
        assert retryable_analysis.attempt_count == 0
        assert retryable_analysis.payload_json["explicit_retry_batch_id"] == result[
            "retry_batch_id"
        ]
        assert missing_source.status == "FAILED"
        assert premature_materialization.status == "FAILED"
        assert unrelated.status == "FAILED"
        assert (
            db.query(FilesystemJobEvent)
            .filter(
                FilesystemJobEvent.details_json[
                    "retry_batch_id"
                ].as_string()
                == result["retry_batch_id"]
            )
            .count()
            == 2
        )
    finally:
        db.close()
