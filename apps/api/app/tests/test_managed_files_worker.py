"""受管目录 worker 测试。"""

import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.db.models import (
    AgentRun,
    ChangeSet,
    Conversation,
    Document,
    DocumentCategory,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentOrganizationDecision,
    DocumentPage,
    DocumentSearchProfile,
    FilesystemJob,
    FilesystemJobEvent,
    ManagedFile,
    ManagedFileRevision,
    ManagedFileSearchProfile,
    ManagedRoot,
    Message,
    RelevantFileSetItem,
    ToolInvocation,
    User,
    WorkingCopy,
    WorkingCopyRoot,
    utcnow,
)
from app.modules.agent.state import ToolInvocationRecord
from app.modules.classification.freshness import ClassificationFreshness
from app.modules.managed_files.worker import (
    _advance_waiting_search_runs,
    _enqueue_background_materialization_jobs_for_revisions,
    _public_job_error_message,
    process_next_filesystem_job,
    reconcile_waiting_search_runs,
)
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.scanner import ManagedFileScanner
from app.modules.managed_files.service import sync_configured_managed_roots
from app.modules.file_lifecycle.service import FileLifecycleJobProcessor
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.retrieval.relevant_file_sets import RelevantFileSetService
from app.tests.helpers import clear_overrides, client_with_database


def test_failed_deduplicated_job_is_not_reopened_by_automatic_scan():
    """终态失败任务不会因扫描再次命中去重键而无限重试。"""

    _client, session_factory = client_with_database()
    with session_factory() as db:
        queue = FilesystemJobQueue(db)
        job = queue.create_job(
            job_type="IMPORT_WORKING_COPIES",
            queue_name="IMPORT",
            root_id=None,
            created_by=None,
            deduplication_key="failed-import-regression",
            max_attempts=99,
            payload={"managed_file_id": "missing"},
        )
        assert job.max_attempts == 3
        job.status = "FAILED"
        job.attempt_count = 3
        db.commit()
        job_id = job.id

    with session_factory() as db:
        same_job = FilesystemJobQueue(db).create_job(
            job_type="IMPORT_WORKING_COPIES",
            queue_name="IMPORT",
            root_id=None,
            created_by=None,
            deduplication_key="failed-import-regression",
            reuse_completed=True,
            payload={"managed_file_id": "missing"},
        )
        assert same_job.id == job_id
        assert same_job.status == "FAILED"
        assert same_job.attempt_count == 3


def test_graph_bootstrap_failure_retries_only_in_graph_queue(monkeypatch):
    """Neo4j 暂时不可用时必须保留独立 GRAPH 重试，不能泄漏连接异常。"""

    monkeypatch.setenv("NEO4J_SYNC_ENABLED", "true")
    monkeypatch.setenv("GRAPH_PROJECTION_WORKER_ENABLED", "true")
    get_settings.cache_clear()
    _client, session_factory = client_with_database()
    with session_factory() as db:
        job = FilesystemJobQueue(db).create_job(
            job_type="GRAPH_BOOTSTRAP_PROJECTION",
            queue_name="GRAPH",
            root_id=None,
            created_by=None,
            payload={"projection_version": "graph-v2"},
        )
        db.commit()
        job_id = job.id

    def fail_projection(*_args, **_kwargs):
        """模拟 Neo4j 短暂离线，不依赖真实外部图数据库。"""

        raise RuntimeError("bolt://private-host:7687 unavailable")

    monkeypatch.setattr(
        "app.modules.managed_files.worker.GraphProjectionService.sync_all",
        fail_projection,
    )
    with pytest.raises(RuntimeError):
        process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="graph-worker-test",
            queue_names={"GRAPH"},
        )

    with session_factory() as db:
        failed = db.get(FilesystemJob, job_id)
        assert failed.status == "PENDING"
        assert failed.attempt_count == 1
        assert "private-host" not in str(failed.error_message)
        assert "文件导入不受影响" in str(failed.error_message)
    clear_overrides()


def test_completed_preparation_job_resumes_original_search_without_new_message(
    monkeypatch,
):
    """内部准备完成后应更新原 AgentRun，不能留下永久 processing 或新增消息。"""

    client, SessionLocal = client_with_database()
    registered = client.post(
        "/api/auth/register",
        json={
            "username": "search-resume-user",
            "password": "password123",
            "display_name": "search-resume-user",
        },
    )
    db = SessionLocal()
    try:
        user = db.get(User, registered.json()["id"])
        conversation = Conversation(
            id="search-resume-conversation",
            user_id=user.id,
            title="检索续跑测试",
        )
        message = Message(
            id="search-resume-message",
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="找2025年工作总结",
            attachments_json=[],
        )
        job = FilesystemJob(
            job_type="ANALYZE_DOCUMENT_VERSION",
            queue_name="ANALYSIS",
            status="COMPLETED",
            payload_json={},
            result_json={},
        )
        other_job = FilesystemJob(
            job_type="ANALYZE_DOCUMENT_VERSION",
            queue_name="ANALYSIS",
            status="PENDING",
            payload_json={},
            result_json={},
        )
        db.add_all([conversation, message, job, other_job])
        db.flush()
        run = AgentRun(
            conversation_id=conversation.id,
            message_id=message.id,
            user_id=user.id,
            intent="SEARCH_FILES",
            status="WAITING_FOR_ASYNC_JOB",
            graph_state_json={
                "status": "WAITING_FOR_ASYNC_JOB",
                "async_job_ids": [job.id, other_job.id],
            },
        )
        db.add(run)
        db.flush()
        invocation = ToolInvocation(
            agent_run_id=run.id,
            tool_name="hybrid-search",
            input_json={"query": "找2025年工作总结", "document_ids": []},
            output_json={
                "kind": "filesystem_job",
                "source": "search-readiness",
                "job_id": job.id,
            },
            status="PENDING",
        )
        db.add(invocation)
        db.flush()

        def fake_invoke(_registry, name, input_json):
            assert name == "hybrid-search"
            return ToolInvocationRecord(
                tool_name=name,
                input_json=input_json,
                output_json={
                    "kind": "workspace_file_search",
                    "ok": True,
                    "query": "找2025年工作总结",
                    "total_returned": 1,
                    "results": [{"filename": "2025年工作总结.docx"}],
                },
                status="COMPLETED",
            )

        monkeypatch.setattr("app.modules.managed_files.worker.ToolRegistry.invoke", fake_invoke)
        _advance_waiting_search_runs(db=db, completed_job=job)

        assert run.status == "COMPLETED"
        assert run.graph_state_json["async_job_ids"] == []
        assert "2025年工作总结.docx" in run.final_response
        assert db.query(Message).count() == 1
        assert other_job.status == "PENDING"
    finally:
        db.close()
        clear_overrides()


def test_idle_worker_reconciles_search_run_after_completed_event_was_missed(
    monkeypatch,
):
    """worker 重启后应按数据库终态续跑旧查询，不能让消息永久显示正在处理。"""

    client, SessionLocal = client_with_database()
    registered = client.post(
        "/api/auth/register",
        json={
            "username": "search-reconcile-user",
            "password": "password123",
            "display_name": "search-reconcile-user",
        },
    )
    with SessionLocal() as db:
        user = db.get(User, registered.json()["id"])
        conversation = Conversation(
            id="search-reconcile-conversation",
            user_id=user.id,
            title="检索补偿测试",
        )
        message = Message(
            id="search-reconcile-message",
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="找未来五年规划",
            attachments_json=[],
        )
        job = FilesystemJob(
            id="search-reconcile-job",
            job_type="ANALYZE_DOCUMENT_VERSION",
            queue_name="ANALYSIS",
            status="COMPLETED",
            payload_json={},
            result_json={},
        )
        db.add_all([conversation, message, job])
        db.flush()
        run = AgentRun(
            id="search-reconcile-run",
            conversation_id=conversation.id,
            message_id=message.id,
            user_id=user.id,
            intent="SEARCH_FILES",
            status="WAITING_FOR_ASYNC_JOB",
            graph_state_json={
                "status": "WAITING_FOR_ASYNC_JOB",
                "async_job_ids": [job.id],
                "result_summary": {
                    "filesystem_job": {"source": "search-readiness"}
                },
            },
        )
        db.add(run)
        db.flush()
        db.add(
            ToolInvocation(
                agent_run_id=run.id,
                tool_name="hybrid-search",
                input_json={"query": "找未来五年规划", "document_ids": []},
                output_json={
                    "kind": "filesystem_job",
                    "source": "search-readiness",
                    "job_id": job.id,
                },
                status="PENDING",
            )
        )
        db.commit()

    def fake_invoke(_registry, name, input_json):
        """补偿续跑使用原检索输入，并返回稳定文件结果。"""

        return ToolInvocationRecord(
            tool_name=name,
            input_json=input_json,
            output_json={
                "kind": "workspace_file_search",
                "ok": True,
                "query": input_json["query"],
                "total_returned": 1,
                "results": [{"filename": "未来五年规划.docx"}],
            },
            status="COMPLETED",
        )

    monkeypatch.setattr(
        "app.modules.managed_files.worker.ToolRegistry.invoke",
        fake_invoke,
    )

    assert reconcile_waiting_search_runs(session_factory=SessionLocal) == 1

    with SessionLocal() as db:
        run = db.get(AgentRun, "search-reconcile-run")
        assert run.status == "COMPLETED"
        assert run.graph_state_json["async_job_ids"] == []
        assert "未来五年规划.docx" in run.final_response
        assert db.query(Message).count() == 1
    clear_overrides()


def test_completed_job_does_not_take_over_unrelated_waiting_agent_run():
    """普通异步任务完成时不能被检索续跑逻辑误判为 hybrid-search。"""

    client, SessionLocal = client_with_database()
    registered = client.post(
        "/api/auth/register",
        json={
            "username": "unrelated-waiting-run-user",
            "password": "password123",
            "display_name": "unrelated-waiting-run-user",
        },
    )
    db = SessionLocal()
    try:
        user = db.get(User, registered.json()["id"])
        conversation = Conversation(
            id="unrelated-waiting-conversation",
            user_id=user.id,
            title="普通异步任务",
        )
        message = Message(
            id="unrelated-waiting-message",
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="对目录中的文件进行分类",
            attachments_json=[],
        )
        job = FilesystemJob(
            job_type="SCAN_MANAGED_ROOT",
            queue_name="SCAN",
            status="COMPLETED",
            payload_json={},
            result_json={},
        )
        db.add_all([conversation, message, job])
        db.flush()
        run = AgentRun(
            conversation_id=conversation.id,
            message_id=message.id,
            user_id=user.id,
            intent="CLASSIFY_FILES",
            status="WAITING_FOR_ASYNC_JOB",
            graph_state_json={
                "status": "WAITING_FOR_ASYNC_JOB",
                "async_job_ids": [job.id],
            },
        )
        db.add(run)
        db.flush()

        _advance_waiting_search_runs(db=db, completed_job=job)

        assert run.status == "WAITING_FOR_ASYNC_JOB"
        assert run.graph_state_json["async_job_ids"] == [job.id]
        assert run.final_response is None
    finally:
        db.close()
        clear_overrides()


def test_worker_processes_scan_job_and_persists_files(tmp_path: Path, capsys):
    """worker 应能领取扫描任务、执行扫描并输出不含路径的控制台状态。"""

    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        managed_dir = tmp_path / "student-affairs"
        managed_dir.mkdir()
        (managed_dir / "notice.pdf").write_text("demo", encoding="utf-8")

        root = ManagedRoot(
            root_key="student_affairs",
            display_name="学工收件箱",
            container_path=str(managed_dir),
        )
        db.add(root)
        db.flush()
        job = FilesystemJob(
            job_type="SCAN_MANAGED_ROOT",
            root_id=root.id,
            status="PENDING",
            payload_json={"root_key": root.root_key},
            result_json={},
        )
        db.add(job)
        db.commit()

        processed_job_id = process_next_filesystem_job(session_factory=SessionLocal, worker_id="worker-test")

        assert processed_job_id == job.id

        refreshed_job = db.get(FilesystemJob, job.id)
        assert refreshed_job is not None
        assert refreshed_job.status == "COMPLETED"
        assert refreshed_job.result_json["files_discovered"] == 1

        managed_file = db.query(ManagedFile).filter(ManagedFile.root_id == root.id).one_or_none()
        assert managed_file is not None
        assert managed_file.relative_path == "notice.pdf"
        console_output = capsys.readouterr().out
        assert "任务开始" in console_output
        assert "任务完成" in console_output
        assert "job_type=SCAN_MANAGED_ROOT" in console_output
        assert "files_discovered=1" in console_output
        # 源侧索引优先模式先入队只读分析，READY 后才允许续接全量工作副本同步。
        assert "source_analysis_jobs=" in console_output
        assert "import_jobs=" not in console_output
        assert str(managed_dir) not in console_output
    finally:
        db.close()
        clear_overrides()


def test_scanner_reports_unavailable_managed_root_instead_of_silent_empty_scan(tmp_path: Path):
    """错误的受管目录配置必须显式失败，不能伪装为发现 0 个文件。"""

    _client, session_factory = client_with_database()
    db = session_factory()
    try:
        root = ManagedRoot(
            root_key="missing_root",
            display_name="不存在的目录",
            container_path=str(tmp_path / "missing-root"),
        )
        db.add(root)
        db.flush()

        with pytest.raises(FileNotFoundError, match="受管原始目录不存在"):
            ManagedFileScanner(db).scan_root(root)
    finally:
        db.close()
        clear_overrides()


def test_scan_job_error_message_distinguishes_path_failure_from_internal_failure():
    """扫描内部回归不得再误报成目录权限问题，同时不能向普通响应泄露异常正文。"""

    job = FilesystemJob(job_type="SCAN_MANAGED_ROOT", queue_name="SCAN", status="RUNNING")
    unavailable = _public_job_error_message(
        job=job,
        error=FileNotFoundError("C:/private/path"),
    )
    internal = _public_job_error_message(
        job=job,
        error=NameError("secret internal detail"),
    )

    assert "目录不可访问" in unavailable
    assert "扫描失败" in internal
    assert "目录不可访问" not in internal
    assert "secret internal detail" not in internal


def test_scan_publishes_source_analysis_jobs_by_batch_before_full_root_completion(monkeypatch, tmp_path: Path, capsys):
    """大目录扫描必须按批提交源侧分析，不能在修订 READY 前并发复制。"""

    managed_dir = tmp_path / "incremental-root"
    managed_dir.mkdir()
    for index in range(3):
        (managed_dir / f"notice-{index}.txt").write_text(f"第 {index} 份测试通知", encoding="utf-8")
    monkeypatch.setenv("MANAGED_ROOT_SCAN_BATCH_SIZE", "1")
    monkeypatch.setenv("MANAGED_ROOT_SCAN_BATCH_MAX_SECONDS", "60")
    get_settings.cache_clear()
    client, session_factory = client_with_database()
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "incremental-scan-user",
                "password": "password123",
                "display_name": "incremental-scan-user",
            },
        )
        assert registered.status_code == 200
        with session_factory() as db:
            root = ManagedRoot(
                root_key="incremental_root",
                display_name="增量扫描目录",
                container_path=str(managed_dir),
            )
            db.add(root)
            db.flush()
            job = FilesystemJob(
                job_type="SCAN_MANAGED_ROOT",
                queue_name="SCAN",
                root_id=root.id,
                status="PENDING",
                payload_json={"root_key": root.root_key},
                result_json={},
            )
            db.add(job)
            db.commit()
            job_id = job.id

        processed = process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="incremental-scan-worker",
            queue_names={"SCAN"},
        )
        assert processed == job_id

        with session_factory() as db:
            completed_scan = db.get(FilesystemJob, job_id)
            source_analysis_jobs = (
                db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "ANALYZE_MANAGED_FILE_REVISION")
                .order_by(FilesystemJob.created_at.asc())
                .all()
            )
            assert completed_scan is not None
            assert completed_scan.result_json["batches_committed"] == 3
            assert len(source_analysis_jobs) == 3
            assert all(
                item.queue_name == "SOURCE_ANALYSIS" and item.status == "PENDING"
                for item in source_analysis_jobs
            )
            assert (
                db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "IMPORT_WORKING_COPIES")
                .count()
                == 0
            )

        console_output = capsys.readouterr().out
        assert console_output.count("扫描批次已提交") == 3
    finally:
        get_settings.cache_clear()
        clear_overrides()


@pytest.mark.parametrize(
    ("filename", "content", "expected_decision"),
    [
        (
            "学校会议纪要研究决定议题.txt",
            "学校会议纪要。会议围绕议题进行研究，研究决定通过有关事项。",
            "AUTO_ORGANIZED",
        ),
        (
            "普通材料.txt",
            "这是一份没有明确业务主题的普通材料。",
            "NEEDS_REVIEW",
        ),
    ],
)
def test_scan_waits_for_source_analysis_before_materializing_working_copy(
    monkeypatch,
    tmp_path: Path,
    filename: str,
    content: str,
    expected_decision: str,
):
    """源侧正文分类完成后才物化，并按统一门槛落入主分类或中性路径。"""

    from app.modules.file_rename.uploaded_suggestion_service import (
        UploadedRenameSuggestionService,
    )

    original_suggest = UploadedRenameSuggestionService.suggest_for_initial_import
    rename_reuse_observations: list[dict[str, bool]] = []
    expected_working_filename = (
        "2026_受管目录会议纪要.txt"
        if expected_decision == "AUTO_ORGANIZED"
        else filename
    )

    def deterministic_initial_suggestion(
        service,
        *,
        document,
        reuse_persisted_extraction_only=False,
    ):
        """保留真实命名解析，只固定门禁结果以验证首次发布是否采用新名称。"""

        suggestion, extraction = original_suggest(
            service,
            document=document,
            reuse_persisted_extraction_only=reuse_persisted_extraction_only,
        )
        rename_reuse_observations.append(
            {
                "strict_reuse": reuse_persisted_extraction_only,
                "reused": bool(extraction and extraction.get("reused")),
                "used_persisted_pages": bool(
                    suggestion.get("rename_candidate_parsers")
                ),
            }
        )
        return {
            **suggestion,
            "status": (
                "READY" if expected_decision == "AUTO_ORGANIZED" else "NO_CHANGE"
            ),
            "proposed_filename": expected_working_filename,
            "warnings": [],
            "errors": [],
        }, extraction

    monkeypatch.setattr(
        UploadedRenameSuggestionService,
        "suggest_for_initial_import",
        deterministic_initial_suggestion,
    )

    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    source = managed_dir / filename
    source.write_text(content, encoding="utf-8")
    working_dir = tmp_path / "working"
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(working_dir))
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("GRAPH_CLASSIFICATION_ENABLED", "false")
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "false")
    monkeypatch.setenv("AUTO_CLASSIFICATION_FALLBACK_MARGIN", "0.01")
    get_settings.cache_clear()
    client, session_factory = client_with_database()
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "working-copy-repair-user",
                "password": "password123",
                "display_name": "working-copy-repair-user",
            },
        )
        assert registered.status_code == 200
        with session_factory() as db:
            root = ManagedRoot(
                root_key="school_files",
                display_name="school_files",
                container_path=str(managed_dir),
            )
            db.add(root)
            db.flush()
            first_scan = FilesystemJob(
                job_type="SCAN_MANAGED_ROOT",
                queue_name="SCAN",
                root_id=root.id,
                status="PENDING",
                payload_json={"root_key": root.root_key},
                result_json={},
            )
            db.add(first_scan)
            db.commit()
            root_id = root.id

        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="repair-scan-worker",
            queue_names={"SCAN"},
        )
        with session_factory() as db:
            source_jobs = (
                db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "ANALYZE_MANAGED_FILE_REVISION")
                .all()
            )
            assert len(source_jobs) == 1
            assert source_jobs[0].queue_name == "SOURCE_ANALYSIS"
            assert db.query(WorkingCopy).count() == 0
            assert (
                db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "IMPORT_WORKING_COPIES")
                .count()
                == 0
            )

        # 分析完成后必须自动续接 MATERIALIZE；复制阶段复用源侧结果，原件保持不变。
        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="source-analysis-worker",
            queue_names={"SOURCE_ANALYSIS"},
        )
        with session_factory() as db:
            completed_source_job = (
                db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "ANALYZE_MANAGED_FILE_REVISION")
                .one()
            )
            assert completed_source_job.result_json.get("status") == "READY", (
                completed_source_job.result_json
            )
            classification_result = completed_source_job.result_json.get("classification") or {}
            assert classification_result.get("agent_run_id")
            assert classification_result.get("changeset_id")
            assert db.query(DocumentCategorySuggestion).count() >= 1
            materialization = (
                db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "MATERIALIZE_WORKING_COPY")
                .one()
            )
            assert materialization.status == "PENDING"
            assert materialization.priority == 100
            # 模拟后台 worker 已经领取任务后用户才命中该文件：运行中任务不能
            # 改写 payload，但相关集合仍必须在任务完成后按 revision_id 收敛。
            materialization.status = "RUNNING"
            revision_id = materialization.payload_json["managed_file_revision_id"]
            materialization_result = RelevantFileSetService(db=db).persist_and_enqueue(
                workspace_id=get_shared_workspace_id(db),
                user_id=registered.json()["id"],
                conversation_id=None,
                agent_run_id=None,
                query="科研项目材料",
                results=[
                    {
                        "resource_type": "MANAGED_SOURCE",
                        "managed_file_revision_id": revision_id,
                        "relevance_tier": "RELATED",
                    }
                ],
            )
            assert materialization_result is not None
            relevant_item = db.query(RelevantFileSetItem).one()
            assert relevant_item.status == "MATERIALIZING"
            assert "relevant_file_set_id" not in materialization.payload_json

            assert FileLifecycleJobProcessor(db).process(materialization) is True
            db.flush()
            db.refresh(relevant_item)
            assert relevant_item.status == "MATERIALIZED"
            working_copy = db.query(WorkingCopy).one()
            assert relevant_item.working_copy_id == working_copy.id
            working_root = db.get(WorkingCopyRoot, working_copy.working_copy_root_id)
            assert working_root is not None
            physical_copy = (
                working_dir / working_root.relative_storage_path / working_copy.relative_path
            )
            decision = db.query(DocumentOrganizationDecision).filter_by(
                working_copy_id=working_copy.id
            ).one()
            target_suggestions = db.query(DocumentCategorySuggestion).filter_by(
                document_id=working_copy.document_id
            ).all()
            assert working_copy.status == "ACTIVE"
            assert working_copy.filename == expected_working_filename
            assert db.get(Document, working_copy.document_id).original_filename == filename
            assert rename_reuse_observations == [
                {
                    "strict_reuse": True,
                    "reused": True,
                    "used_persisted_pages": True,
                }
            ]
            assert decision.decision == expected_decision
            if expected_decision == "AUTO_ORGANIZED":
                relation = db.query(DocumentCategory).filter_by(
                    working_copy_id=working_copy.id
                ).one()
                assert working_copy.relative_path == (
                    f"学校/行政综合管理类/会议纪要/{expected_working_filename}"
                )
                assert relation.status == "AUTO_APPLIED"
                assert relation.relation_role == "PRIMARY"
            else:
                assert working_copy.relative_path.startswith(".internal/neutral/")
                assert working_copy.relative_path.endswith(
                    f"/{expected_working_filename}"
                )
                assert db.query(DocumentCategory).filter_by(
                    working_copy_id=working_copy.id
                ).count() == 0
            assert target_suggestions
            assert physical_copy.read_text(encoding="utf-8") == content
            assert source.read_text(encoding="utf-8") == content
    finally:
        get_settings.cache_clear()
        clear_overrides()


def test_ready_revision_without_current_classification_queues_one_refresh_job(
    monkeypatch,
):
    """READY 修订缺少当前分类时应先刷新，并按运行时身份保持幂等。"""

    monkeypatch.setenv("MATERIALIZE_ALL_MANAGED_FILES", "true")
    monkeypatch.setenv("MATERIALIZE_WORKING_COPY_BACKGROUND_PRIORITY", "100")
    get_settings.cache_clear()
    client, session_factory = client_with_database()
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "background-materialize-user",
                "password": "password123",
                "display_name": "background-materialize-user",
            },
        )
        assert registered.status_code == 200
        with session_factory() as db:
            root = ManagedRoot(
                root_key="background_root",
                display_name="后台同步目录",
                container_path="/managed/background-root",
                created_by=registered.json()["id"],
            )
            db.add(root)
            db.flush()
            managed_file = ManagedFile(
                root_id=root.id,
                relative_path="通知/科研通知.txt",
                relative_path_hash="background-materialize-path",
                filename="科研通知.txt",
                extension=".txt",
                size_bytes=24,
                fingerprint="background-materialize-fingerprint",
                content_sha256="a" * 64,
                status="ACTIVE",
            )
            db.add(managed_file)
            db.flush()
            revision = ManagedFileRevision(
                managed_file_id=managed_file.id,
                revision_number=1,
                size_bytes=24,
                quick_fingerprint=managed_file.fingerprint,
                content_sha256=managed_file.content_sha256,
                status="READY",
                analysis_status="READY",
                is_current=True,
            )
            db.add(revision)
            db.flush()

            first_ids = _enqueue_background_materialization_jobs_for_revisions(
                db=db,
                revisions=[revision],
                created_by=registered.json()["id"],
            )
            second_ids = _enqueue_background_materialization_jobs_for_revisions(
                db=db,
                revisions=[revision],
                created_by=registered.json()["id"],
            )

            materialization_jobs = (
                db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "MATERIALIZE_WORKING_COPY")
                .all()
            )
            refresh_jobs = (
                db.query(FilesystemJob)
                .filter(
                    FilesystemJob.job_type
                    == "REFRESH_MANAGED_SOURCE_CLASSIFICATION"
                )
                .all()
            )
            assert first_ids == second_ids == []
            assert materialization_jobs == []
            assert len(refresh_jobs) == 1
            assert refresh_jobs[0].queue_name == "SOURCE_ANALYSIS"
            assert refresh_jobs[0].payload_json["managed_file_revision_id"] == revision.id
            assert refresh_jobs[0].payload_json["taxonomy_version"]
            assert refresh_jobs[0].payload_json["classifier_version"]
    finally:
        get_settings.cache_clear()
    clear_overrides()


def test_stale_source_classification_refresh_reuses_extraction_before_materialization(
    monkeypatch,
    tmp_path: Path,
):
    """任意旧分类身份必须先复用正文刷新，再允许工作副本发布。"""

    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    filename = "学校会议纪要研究决定议题.txt"
    source = managed_dir / filename
    source.write_text(
        "学校会议纪要。会议围绕议题进行研究，研究决定通过有关事项。",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("GRAPH_CLASSIFICATION_ENABLED", "false")
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "true")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "true")
    monkeypatch.setenv("AUTO_CLASSIFICATION_SHADOW_MODE", "false")
    monkeypatch.setenv("AUTO_CLASSIFICATION_FALLBACK_MARGIN", "0.01")
    get_settings.cache_clear()
    client, session_factory = client_with_database()
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "stale-classification-user",
                "password": "password123",
                "display_name": "stale-classification-user",
            },
        )
        user_id = registered.json()["id"]
        with session_factory() as db:
            root = ManagedRoot(
                root_key="stale_classification_root",
                display_name="stale_classification_root",
                container_path=str(managed_dir),
                created_by=user_id,
            )
            db.add(root)
            db.flush()
            db.add(
                FilesystemJob(
                    job_type="SCAN_MANAGED_ROOT",
                    queue_name="SCAN",
                    root_id=root.id,
                    status="PENDING",
                    payload_json={"root_key": root.root_key},
                    result_json={},
                )
            )
            db.commit()

        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="stale-scan-worker",
            queue_names={"SCAN"},
        )
        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="stale-source-worker",
            queue_names={"SOURCE_ANALYSIS"},
        )
        with session_factory() as db:
            extraction_count = db.query(DocumentExtractionRun).count()
            run = db.query(DocumentClassificationRun).one()
            run.taxonomy_version = "arbitrary-old-version"
            db.commit()

        # 物化入口再次检查身份；过期时不得先复制到 neutral。
        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="stale-materialize-guard",
            queue_names={"MATERIALIZE"},
        )
        with session_factory() as db:
            assert db.query(WorkingCopy).count() == 0
            materialization = db.query(FilesystemJob).filter_by(
                job_type="MATERIALIZE_WORKING_COPY"
            ).one()
            assert materialization.result_json["status"] == (
                "DEFERRED_CLASSIFICATION_REFRESH"
            )
            assert db.query(FilesystemJob).filter_by(
                job_type="REFRESH_MANAGED_SOURCE_CLASSIFICATION",
                status="PENDING",
            ).count() == 1

        def fail_if_source_is_reparsed(**_kwargs):
            """分类版本刷新不得重新进入源文件解析器。"""

            raise AssertionError("classification refresh unexpectedly reparsed source")

        monkeypatch.setattr(
            "app.modules.managed_files.source_analysis._extract_managed_source_document",
            fail_if_source_is_reparsed,
        )
        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="stale-refresh-worker",
            queue_names={"SOURCE_ANALYSIS"},
        )
        with session_factory() as db:
            assert db.query(DocumentExtractionRun).count() == extraction_count
            assert db.query(DocumentClassificationRun).count() == 2
            refresh_job = db.query(FilesystemJob).filter_by(
                job_type="REFRESH_MANAGED_SOURCE_CLASSIFICATION"
            ).one()
            assert refresh_job.result_json["reused_extraction"] is True
            materialization = db.query(FilesystemJob).filter_by(
                job_type="MATERIALIZE_WORKING_COPY"
            ).one()
            assert materialization.status == "PENDING"

        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="stale-materialize-worker",
            queue_names={"MATERIALIZE"},
        )
        with session_factory() as db:
            working_copy = db.query(WorkingCopy).one()
            assert working_copy.relative_path.startswith(
                "学校/行政综合管理类/会议纪要/"
            )
            assert working_copy.relative_path.endswith(f"/{working_copy.filename}")
            assert db.get(Document, working_copy.document_id).original_filename == filename
            assert source.read_text(encoding="utf-8").startswith("学校会议纪要")
            assert not working_copy.relative_path.startswith(".internal/neutral/")
    finally:
        get_settings.cache_clear()
        clear_overrides()


def test_metadata_only_image_analysis_materializes_searchable_working_copy(
    monkeypatch,
    tmp_path: Path,
):
    """OCR 技术失败图片仍应完成源索引、工作副本物化和目录元数据投影。"""

    managed_dir = tmp_path / "managed"
    event_dir = managed_dir / "20170606大数据联合实验室授牌" / "照片"
    event_dir.mkdir(parents=True)
    source = event_dir / "IMG_0198.JPG"
    source.write_bytes(b"image-without-ocr-runtime")
    working_dir = tmp_path / "working"
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(working_dir))
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MATERIALIZE_ALL_MANAGED_FILES", "true")
    get_settings.cache_clear()
    client, session_factory = client_with_database()
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "metadata-image-user",
                "password": "password123",
                "display_name": "metadata-image-user",
            },
        )
        assert registered.status_code == 200
        with session_factory() as db:
            root = ManagedRoot(
                root_key="image_event_root",
                display_name="image_event_root",
                container_path=str(managed_dir),
                created_by=registered.json()["id"],
            )
            db.add(root)
            db.flush()
            scan = FilesystemJob(
                job_type="SCAN_MANAGED_ROOT",
                queue_name="SCAN",
                root_id=root.id,
                status="PENDING",
                payload_json={"root_key": root.root_key},
                result_json={},
            )
            db.add(scan)
            db.commit()

        monkeypatch.setattr(
            "app.modules.managed_files.source_analysis._extract_managed_source_document",
            lambda **_kwargs: {
                "ok": False,
                "status": "FAILED",
                "extractor": "ocr",
                "error": {
                    "code": "OCR_ENGINE_NOT_AVAILABLE",
                    "message": "runtime failure",
                },
                "pages": [],
            },
        )
        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="metadata-image-scan",
            queue_names={"SCAN"},
        )
        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="metadata-image-analysis",
            queue_names={"SOURCE_ANALYSIS"},
        )
        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="metadata-image-materialize",
            queue_names={"MATERIALIZE"},
        )

        with session_factory() as db:
            revision = db.query(ManagedFileRevision).one()
            assert revision.status == "READY"
            source_profile = db.query(ManagedFileSearchProfile).one()
            assert "授牌" in source_profile.search_text
            working_copy = db.query(WorkingCopy).one()
            working_profile = db.query(DocumentSearchProfile).one()
            assert working_profile.working_copy_id == working_copy.id
            assert "授牌" in str(working_profile.metadata_search_text)
            pages = db.query(DocumentPage).filter(
                DocumentPage.document_id == working_copy.document_id
            ).all()
            assert len(pages) == 1
            assert pages[0].text_content == ""
            assert pages[0].metadata_json["image_text_status"] == "OCR_FAILED"
            assert db.query(DocumentIndexRun).filter(
                DocumentIndexRun.document_version_id == working_copy.current_version_id
            ).count() == 0
            working_root = db.get(WorkingCopyRoot, working_copy.working_copy_root_id)
            physical_copy = (
                working_dir / working_root.relative_storage_path / working_copy.relative_path
            )
            assert physical_copy.read_bytes() == source.read_bytes()
    finally:
        get_settings.cache_clear()
        clear_overrides()


def test_source_analysis_completion_automatically_queues_background_materialization(
    monkeypatch,
):
    """SOURCE_ANALYSIS 完成钩子必须自动续接全量同步，无需等待用户搜索。"""

    monkeypatch.setenv("MATERIALIZE_ALL_MANAGED_FILES", "true")
    get_settings.cache_clear()
    client, session_factory = client_with_database()
    try:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "analysis-hook-user",
                "password": "password123",
                "display_name": "analysis-hook-user",
            },
        )
        assert registered.status_code == 200
        with session_factory() as db:
            root = ManagedRoot(
                root_key="analysis_hook_root",
                display_name="分析完成钩子目录",
                container_path="/managed/analysis-hook-root",
                created_by=registered.json()["id"],
            )
            db.add(root)
            db.flush()
            managed_file = ManagedFile(
                root_id=root.id,
                relative_path="材料/分析完成.txt",
                relative_path_hash="analysis-hook-path",
                filename="分析完成.txt",
                extension=".txt",
                size_bytes=12,
                fingerprint="analysis-hook-fingerprint",
                status="ACTIVE",
            )
            db.add(managed_file)
            db.flush()
            revision = ManagedFileRevision(
                managed_file_id=managed_file.id,
                revision_number=1,
                size_bytes=12,
                quick_fingerprint=managed_file.fingerprint,
                status="ANALYSIS_PENDING",
                analysis_status="PENDING",
                is_current=True,
            )
            db.add(revision)
            db.flush()
            source_job = FilesystemJobQueue(db).create_job(
                job_type="ANALYZE_MANAGED_FILE_REVISION",
                queue_name="SOURCE_ANALYSIS",
                root_id=root.id,
                created_by=registered.json()["id"],
                payload={
                    "managed_file_revision_id": revision.id,
                    "user_id": registered.json()["id"],
                },
            )
            db.commit()
            revision_id = revision.id
            source_job_id = source_job.id

        def fake_analyze(self, *, revision_id: str, user_id: str | None = None):
            """用确定性状态替代真实解析，只验证完成钩子的队列边界。"""

            revision = self.db.get(ManagedFileRevision, revision_id)
            assert revision is not None
            revision.status = "READY"
            revision.analysis_status = "READY"
            revision.content_sha256 = "b" * 64
            return {"status": "READY", "revision_id": revision_id}

        monkeypatch.setattr(
            "app.modules.managed_files.worker.ManagedSourceAnalysisService.analyze",
            fake_analyze,
        )
        monkeypatch.setattr(
            "app.modules.managed_files.worker.inspect_managed_source_classification",
            lambda **_kwargs: ClassificationFreshness.CURRENT,
        )
        processed = process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="source-analysis-hook-worker",
            queue_names={"SOURCE_ANALYSIS"},
        )
        assert processed == source_job_id

        with session_factory() as db:
            completed = db.get(FilesystemJob, source_job_id)
            materialization = (
                db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "MATERIALIZE_WORKING_COPY")
                .one()
            )
            assert completed is not None
            assert completed.status == "COMPLETED"
            assert completed.result_json["materialization_job_ids"] == [
                materialization.id
            ]
            assert materialization.priority == 100
            assert materialization.payload_json["managed_file_revision_id"] == revision_id
    finally:
        get_settings.cache_clear()
        clear_overrides()


def test_reconciliation_creates_new_generation_after_completed_scan(tmp_path: Path):
    """下一次协调必须创建新扫描代次，同时保留已完成任务的审计终态。"""

    _client, session_factory = client_with_database()
    db = session_factory()
    try:
        root_dir = tmp_path / "startup-root"
        root_dir.mkdir()
        root = ManagedRoot(root_key="startup_root", display_name="启动同步目录", container_path=str(root_dir))
        db.add(root)
        db.flush()
        parent = FilesystemJob(
            job_type="RECONCILE_MANAGED_ROOT",
            root_id=root.id,
            status="RUNNING",
            payload_json={},
            result_json={},
        )
        db.add(parent)
        db.flush()

        processor = FileLifecycleJobProcessor(db)
        assert processor.process(parent) is True
        first_scan_id = parent.result_json["scan_job_id"]
        first_scan = db.get(FilesystemJob, first_scan_id)
        assert first_scan is not None
        first_scan.status = "COMPLETED"
        parent.status = "RUNNING"
        db.flush()

        # scheduler 重用父任务后必须生成新代次，不能重置旧任务或丢失审计历史。
        assert processor.process(parent) is True
        second_scan_id = parent.result_json["scan_job_id"]
        second_scan = db.get(FilesystemJob, second_scan_id)
        assert second_scan is not None
        assert second_scan.id != first_scan_id
        assert second_scan.status == "PENDING"
        assert second_scan.queue_name == "SCAN"
        assert second_scan.payload_json["scan_generation"] == 2
        assert db.get(FilesystemJob, first_scan_id).status == "COMPLETED"
        assert parent.result_json["scan_reused"] is False
    finally:
        db.close()
        clear_overrides()


def test_reconciliation_creates_new_generation_after_failed_scan(tmp_path: Path):
    """历史扫描失败后下一轮仍可继续，但不得重开或覆盖旧失败任务。"""

    _client, session_factory = client_with_database()
    db = session_factory()
    try:
        root_dir = tmp_path / "failed-scan-root"
        root_dir.mkdir()
        root = ManagedRoot(
            root_key="failed_scan_root",
            display_name="失败恢复目录",
            container_path=str(root_dir),
        )
        db.add(root)
        db.flush()
        parent = FilesystemJob(
            job_type="RECONCILE_MANAGED_ROOT",
            root_id=root.id,
            status="RUNNING",
            payload_json={},
            result_json={},
        )
        db.add(parent)
        db.flush()

        processor = FileLifecycleJobProcessor(db)
        assert processor.process(parent) is True
        first_scan_id = parent.result_json["scan_job_id"]
        first_scan = db.get(FilesystemJob, first_scan_id)
        first_scan.status = "FAILED"
        first_scan.attempt_count = 3
        first_scan.error_message = "确定性测试失败"
        parent.status = "RUNNING"
        db.flush()

        assert processor.process(parent) is True
        second_scan_id = parent.result_json["scan_job_id"]
        second_scan = db.get(FilesystemJob, second_scan_id)
        assert second_scan_id != first_scan_id
        assert second_scan.status == "PENDING"
        assert second_scan.payload_json["scan_generation"] == 2
        preserved_failure = db.get(FilesystemJob, first_scan_id)
        assert preserved_failure.status == "FAILED"
        assert preserved_failure.attempt_count == 3
        assert preserved_failure.error_message == "确定性测试失败"
    finally:
        db.close()
        clear_overrides()


def test_reconciliation_reuses_active_scan_without_duplicate(tmp_path: Path):
    """已有待执行扫描时协调只关联该任务，不能产生并发重复扫描。"""

    _client, session_factory = client_with_database()
    db = session_factory()
    try:
        root_dir = tmp_path / "active-scan-root"
        root_dir.mkdir()
        root = ManagedRoot(
            root_key="active_scan_root",
            display_name="单活扫描目录",
            container_path=str(root_dir),
        )
        db.add(root)
        db.flush()
        parent = FilesystemJob(
            job_type="RECONCILE_MANAGED_ROOT",
            root_id=root.id,
            status="RUNNING",
            payload_json={},
            result_json={},
        )
        db.add(parent)
        db.flush()

        processor = FileLifecycleJobProcessor(db)
        assert processor.process(parent) is True
        first_scan_id = parent.result_json["scan_job_id"]
        parent.status = "RUNNING"
        db.flush()

        assert processor.process(parent) is True
        assert parent.result_json["scan_job_id"] == first_scan_id
        assert parent.result_json["scan_generation"] == 1
        assert parent.result_json["scan_reused"] is True
        assert (
            db.query(FilesystemJob)
            .filter(
                FilesystemJob.root_id == root.id,
                FilesystemJob.job_type == "SCAN_MANAGED_ROOT",
            )
            .count()
            == 1
        )
    finally:
        db.close()
        clear_overrides()


def test_worker_completes_async_managed_file_classification(monkeypatch, tmp_path: Path):
    """大批量分类 Job 必须回写 AgentRun、分类建议和 ChangeSet。"""

    managed_dir = tmp_path / "downloads"
    target_dir = managed_dir / "党办"
    target_dir.mkdir(parents=True)
    (target_dir / "职称材料一.txt").write_text("教师职称申报材料", encoding="utf-8")
    (target_dir / "职称材料二.txt").write_text("教师职称评定材料", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MANAGED_ROOT_DOWNLOADS", str(managed_dir))
    monkeypatch.setenv("MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT", "1")
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    register = client.post(
        "/api/auth/register",
        json={
            "username": "managed-classification-worker-user",
            "password": "password123",
            "display_name": "managed-classification-worker-user",
        },
    )
    login = client.post(
        "/api/auth/login",
        json={"username": "managed-classification-worker-user", "password": "password123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    db = SessionLocal()
    try:
        # 模拟 RECONCILE worker 已先完成索引；聊天分类入口不得同步扫描目录。
        for root in sync_configured_managed_roots(db, scan=False):
            ManagedFileScanner(db).scan_root(root)
        db.commit()
    finally:
        db.close()

    response = client.post(
        "/api/conversations/managed-classification-worker-conv/messages",
        headers=headers,
        json={"content": "对党办下文件进行分类", "attachments": []},
    )

    assert response.status_code == 200
    initial_run = response.json()["task_result"]
    assert initial_run["task_status"] == "processing"
    job_id = initial_run["pending_job_ids"][0]

    processed_job_id = process_next_filesystem_job(
        session_factory=SessionLocal,
        worker_id="classification-worker-test",
    )

    assert processed_job_id == job_id
    db = SessionLocal()
    try:
        job = db.get(FilesystemJob, job_id)
        run = db.get(AgentRun, initial_run["task_id"])
        assert job is not None
        assert job.status == "COMPLETED"
        assert job.progress_current == 2
        assert job.progress_total == 2
        assert job.result_json["completed_count"] == 2
        assert run is not None
        assert run.status == "COMPLETED"
        assert len((run.graph_state_json or {}).get("document_results", [])) == 2
        assert db.query(DocumentCategorySuggestion).count() >= 2
        assert db.query(ChangeSet).filter(ChangeSet.agent_run_id == run.id).count() == 1
        assert register.status_code == 200
    finally:
        db.close()
        get_settings.cache_clear()
        clear_overrides()


def test_worker_hides_internal_error_details_for_user_classification_jobs(monkeypatch):
    """普通用户可查询的分类 Job 不能暴露服务器路径或底层异常文本。"""

    _client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        job = FilesystemJob(
            job_type="CLASSIFY_MANAGED_FILES",
            status="PENDING",
            payload_json={"user_id": "user-1", "agent_run_id": "missing-run"},
            result_json={},
            created_by="user-1",
        )
        db.add(job)
        db.commit()
        job_id = job.id

        def fail_job(*, db, job):
            raise RuntimeError("/srv/private/data/secret.docx connection password=unsafe")

        monkeypatch.setattr("app.modules.managed_files.worker._process_job", fail_job)

        with pytest.raises(RuntimeError):
            process_next_filesystem_job(
                session_factory=SessionLocal,
                worker_id="classification-worker-failure-test",
            )

        db.expire_all()
        failed_job = db.get(FilesystemJob, job_id)
        assert failed_job is not None
        assert failed_job.status == "FAILED"
        assert failed_job.error_message == "受管文件后台分类失败，请稍后重试或联系管理员。"
        assert "/srv/private" not in failed_job.error_message
        assert "password" not in failed_job.error_message
    finally:
        db.close()
        clear_overrides()


def test_lifecycle_worker_persists_attempts_stops_retrying_and_logs_traceback(
    monkeypatch,
    tmp_path: Path,
):
    """生命周期任务失败必须累计真实次数，达到上限后停止并留下可关联堆栈。

    数据库只保存脱敏文案和 error_reference；底层异常堆栈仅进入服务器 JSONL，
    防止普通任务状态接口泄露路径、密码或内部实现细节。
    """

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    _client, session_factory = client_with_database()
    with session_factory() as db:
        job = FilesystemJob(
            job_type="IMPORT_WORKING_COPIES",
            queue_name="IMPORT",
            status="PENDING",
            payload_json={},
            result_json={},
            max_attempts=3,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    def fail_job(*, db, job):
        raise RuntimeError("内部导入失败 password=unsafe")

    monkeypatch.setattr("app.modules.managed_files.worker._process_job", fail_job)

    try:
        error_references: list[str] = []
        for expected_attempt in range(1, 4):
            with pytest.raises(RuntimeError, match="内部导入失败"):
                process_next_filesystem_job(
                    session_factory=session_factory,
                    worker_id="retry-regression-worker",
                    queue_names={"IMPORT"},
                )

            with session_factory() as db:
                persisted_job = db.get(FilesystemJob, job_id)
                assert persisted_job is not None
                assert persisted_job.attempt_count == expected_attempt
                expected_status = "PENDING" if expected_attempt < 3 else "FAILED"
                assert persisted_job.status == expected_status
                latest_event = (
                    db.query(FilesystemJobEvent)
                    .filter(FilesystemJobEvent.job_id == job_id)
                    .order_by(FilesystemJobEvent.created_at.desc())
                    .first()
                )
                assert latest_event is not None
                assert latest_event.details_json["attempt_count"] == expected_attempt
                assert latest_event.details_json["max_attempts"] == 3
                assert latest_event.details_json["error_code"] == "RuntimeError"
                error_reference = latest_event.details_json["error_reference"]
                assert error_reference.startswith("req_")
                error_references.append(error_reference)

                if expected_attempt < 3:
                    # 测试中立即放开下一次领取，不等待生产环境的退避时间。
                    persisted_job.available_at = utcnow()
                    db.commit()

        log_records: list[dict] = []
        for path in sorted((tmp_path / "logs").glob("file-agent-*.log")):
            log_records.extend(
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            )
        failure_logs = [
            item
            for item in log_records
            if item.get("event") == "filesystem.worker.failed"
            and item.get("job_id") == job_id
        ]
        assert len(failure_logs) == 3
        assert {item["error_reference"] for item in failure_logs} == set(error_references)
        assert all("fail_job" in item["exception_traceback"] for item in failure_logs)
        assert all("password=<redacted>" in item["exception_traceback"] for item in failure_logs)
        assert all("unsafe" not in item["exception_traceback"] for item in failure_logs)
    finally:
        get_settings.cache_clear()
        clear_overrides()
