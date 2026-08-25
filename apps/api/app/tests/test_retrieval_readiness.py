"""工作副本检索就绪协调测试。"""

from types import SimpleNamespace

from app.core.config import get_settings
from app.db.models import FilesystemJob, ManagedFile, ManagedFileRevision, ManagedRoot
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.retrieval.readiness import WorkingCopySearchReadinessService
from app.tests.helpers import clear_overrides, client_with_database


def test_managed_only_match_queues_source_analysis_without_public_candidate():
    """只有受管元数据命中时应优先分析源文件，不能抢先复制工作副本。"""

    client, SessionLocal = client_with_database()
    registered = client.post(
        "/api/auth/register",
        json={
            "username": "readiness-user",
            "password": "password123",
            "display_name": "readiness-user",
        },
    )
    db = SessionLocal()
    try:
        root = ManagedRoot(
            root_key="school_files",
            display_name="学校文件",
            container_path="/managed/school-files",
        )
        db.add(root)
        db.flush()
        managed_file = ManagedFile(
            root_id=root.id,
            relative_path="总结/计算机学院2025年工作总结.docx",
            relative_path_hash="readiness-file",
            filename="计算机学院2025年工作总结.docx",
            extension=".docx",
            size_bytes=10,
            fingerprint="readiness-fingerprint",
            status="ACTIVE",
        )
        unrelated_file = ManagedFile(
            root_id=root.id,
            relative_path="总结/人文学院2025年工作总结.docx",
            relative_path_hash="readiness-unrelated-file",
            filename="人文学院2025年工作总结.docx",
            extension=".docx",
            size_bytes=10,
            fingerprint="readiness-unrelated-fingerprint",
            status="ACTIVE",
        )
        noisy_files = [
            ManagedFile(
                root_id=root.id,
                relative_path=f"总结/人文学院2025年工作总结{index:02d}.docx",
                relative_path_hash=f"readiness-noise-{index}",
                filename=f"人文学院2025年工作总结{index:02d}.docx",
                extension=".docx",
                size_bytes=10,
                fingerprint=f"readiness-noise-fingerprint-{index}",
                status="ACTIVE",
            )
            for index in range(45)
        ]
        db.add_all([managed_file, unrelated_file, *noisy_files])
        db.flush()

        service = WorkingCopySearchReadinessService(
            db=db,
            user_id=registered.json()["id"],
            workspace_id=get_shared_workspace_id(db),
        )
        result = service.prepare_after_miss(
            parsed_query=SimpleNamespace(
                year=2025,
                terms=["计算机学院", "工作总结"],
                cleaned="计算机学院工作总结",
            )
        )

        assert result is not None
        assert result["kind"] == "filesystem_job"
        assert result["status"] == "PROCESSING"
        assert "filename" not in result
        assert "managed_file" not in result
        job = db.get(FilesystemJob, result["job_id"])
        assert job is not None
        assert job.job_type == "ANALYZE_MANAGED_FILE_REVISION"
        assert job.queue_name == "SOURCE_ANALYSIS"
        assert job.priority == 10
        assert job.max_attempts == 3
        assert job.attempt_count == 0
        assert (
            db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "ANALYZE_MANAGED_FILE_REVISION")
                .count()
                == 1
            )
        assert db.query(ManagedFileRevision).count() == 1
    finally:
        db.close()
        clear_overrides()


def test_school_possessive_readiness_requires_school_scope_and_topic():
    """学校范围与工作总结主题都命中时，才允许静默准备源文件。"""

    client, SessionLocal = client_with_database()
    registered = client.post(
        "/api/auth/register",
        json={
            "username": "readiness-school-topic-user",
            "password": "password123",
            "display_name": "readiness-school-topic-user",
        },
    )
    db = SessionLocal()
    try:
        root = ManagedRoot(
            root_key="school_files",
            display_name="学校文件",
            container_path="/managed/school-files",
        )
        db.add(root)
        db.flush()
        db.add_all(
            [
                ManagedFile(
                    root_id=root.id,
                    relative_path="总结/学校2025年工作总结.docx",
                    relative_path_hash="readiness-school-summary",
                    filename="学校2025年工作总结.docx",
                    extension=".docx",
                    size_bytes=10,
                    fingerprint="readiness-school-summary-fingerprint",
                    status="ACTIVE",
                ),
                ManagedFile(
                    root_id=root.id,
                    relative_path="通知/学校会议通知.docx",
                    relative_path_hash="readiness-school-notice",
                    filename="学校会议通知.docx",
                    extension=".docx",
                    size_bytes=10,
                    fingerprint="readiness-school-notice-fingerprint",
                    status="ACTIVE",
                ),
            ]
        )
        db.flush()

        result = WorkingCopySearchReadinessService(
            db=db,
            user_id=registered.json()["id"],
            workspace_id=get_shared_workspace_id(db),
        ).prepare_after_miss(
            parsed_query=SimpleNamespace(
                year=None,
                terms=["学校", "工作总结"],
                cleaned="学校的工作总结",
            )
        )

        assert result is not None
        jobs = (
            db.query(FilesystemJob)
            .filter(FilesystemJob.job_type == "ANALYZE_MANAGED_FILE_REVISION")
            .all()
        )
        assert len(jobs) == 1
        assert jobs[0].payload_json["managed_file_revision_id"]
    finally:
        db.close()
        clear_overrides()


def test_terminal_failed_source_analysis_is_not_reopened_by_search():
    """用户重复检索不能重新激活已达到终态的源侧分析。"""

    client, SessionLocal = client_with_database()
    registered = client.post(
        "/api/auth/register",
        json={
            "username": "readiness-failed-user",
            "password": "password123",
            "display_name": "readiness-failed-user",
        },
    )
    db = SessionLocal()
    try:
        root = ManagedRoot(
            root_key="school_files",
            display_name="学校文件",
            container_path="/managed/school-files",
        )
        db.add(root)
        db.flush()
        managed_file = ManagedFile(
            root_id=root.id,
            relative_path="总结/2025年工作总结.docx",
            relative_path_hash="readiness-failed-file",
            filename="2025年工作总结.docx",
            extension=".docx",
            size_bytes=10,
            fingerprint="readiness-failed-fingerprint",
            status="ACTIVE",
        )
        db.add(managed_file)
        db.flush()
        workspace_id = get_shared_workspace_id(db)
        revision = ManagedFileRevision(
            managed_file_id=managed_file.id,
            revision_number=1,
            size_bytes=10,
            quick_fingerprint=managed_file.fingerprint,
            status="FAILED",
            analysis_status="FAILED",
            is_current=True,
        )
        db.add(revision)
        db.flush()
        failed = FilesystemJob(
            job_type="ANALYZE_MANAGED_FILE_REVISION",
            queue_name="SOURCE_ANALYSIS",
            root_id=root.id,
            created_by=registered.json()["id"],
            deduplication_key=(
                f"managed-source-analysis:{revision.id}"
            ),
            status="FAILED",
            priority=100,
            attempt_count=3,
            max_attempts=3,
            payload_json={},
            result_json={},
        )
        db.add(failed)
        db.flush()

        result = WorkingCopySearchReadinessService(
            db=db,
            user_id=registered.json()["id"],
            workspace_id=workspace_id,
        ).prepare_after_miss(
            parsed_query=SimpleNamespace(
                year=2025,
                terms=["工作总结"],
                cleaned="工作总结",
            )
        )

        assert result is None
        assert failed.status == "FAILED"
        assert failed.attempt_count == 3
    finally:
        db.close()
        clear_overrides()


def test_hybrid_search_returns_only_internal_source_analysis_receipt_on_unanalyzed_managed_match(
    monkeypatch,
):
    """尚未源侧分析的受管文件不得伪装成结果，只能进入通用异步回执。"""

    monkeypatch.setenv("TWO_STAGE_RETRIEVAL_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    registered = client.post(
        "/api/auth/register",
        json={
            "username": "readiness-tool-user",
            "password": "password123",
            "display_name": "readiness-tool-user",
        },
    )
    db = SessionLocal()
    try:
        root = ManagedRoot(
            root_key="school_files",
            display_name="学校文件",
            container_path="/managed/school-files",
        )
        db.add(root)
        db.flush()
        db.add(
            ManagedFile(
                root_id=root.id,
                relative_path="总结/计算机学院2025年工作总结.docx",
                relative_path_hash="readiness-tool-file",
                filename="计算机学院2025年工作总结.docx",
                extension=".docx",
                size_bytes=10,
                fingerprint="readiness-tool-fingerprint",
                status="ACTIVE",
            )
        )
        db.flush()
        registry = ToolRegistry(db=db, user_id=registered.json()["id"])
        registry.set_run_context(
            conversation_id="readiness-tool-conversation",
            agent_run_id="readiness-tool-run",
        )

        record = registry.invoke(
            "hybrid-search",
            {"query": "找2025年计算机学院的工作总结", "document_ids": []},
        )
        output = record.output_json

        assert output["kind"] == "filesystem_job"
        assert output["status"] == "PROCESSING"
        assert record.status == "PENDING"
        assert output.get("results") is None
        assert "filename" not in output
        assert "managed_file" not in output
    finally:
        db.close()
        get_settings.cache_clear()
        clear_overrides()
