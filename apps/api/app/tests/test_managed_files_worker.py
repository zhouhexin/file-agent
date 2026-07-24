"""受管目录 worker 测试。"""

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.db.models import (
    AgentRun,
    ChangeSet,
    DocumentCategorySuggestion,
    FilesystemJob,
    ManagedFile,
    ManagedRoot,
    WorkingCopy,
    WorkingCopyRoot,
)
from app.modules.managed_files.worker import _public_job_error_message, process_next_filesystem_job
from app.modules.managed_files.scanner import ManagedFileScanner
from app.modules.managed_files.service import sync_configured_managed_roots
from app.modules.file_lifecycle.service import FileLifecycleJobProcessor
from app.tests.helpers import clear_overrides, client_with_database


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
        assert "import_jobs=" in console_output
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


def test_scan_publishes_import_jobs_by_batch_before_full_root_completion(monkeypatch, tmp_path: Path, capsys):
    """大目录扫描必须按批提交 IMPORT 任务，不能等整轮扫描完成后才统一入队。"""

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
            import_jobs = (
                db.query(FilesystemJob)
                .filter(FilesystemJob.job_type == "IMPORT_WORKING_COPIES")
                .order_by(FilesystemJob.created_at.asc())
                .all()
            )
            assert completed_scan is not None
            assert completed_scan.result_json["batches_committed"] == 3
            assert len(import_jobs) == 3
            assert all(item.queue_name == "IMPORT" and item.status == "PENDING" for item in import_jobs)

        console_output = capsys.readouterr().out
        assert console_output.count("扫描批次已提交") == 3
    finally:
        get_settings.cache_clear()
        clear_overrides()


def test_scan_requeues_completed_import_when_local_working_copy_is_missing(
    monkeypatch,
    tmp_path: Path,
):
    """共享开发库已有记录但本机文件缺失时，扫描必须重新物化同一工作副本。"""

    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    source = managed_dir / "科研通知.txt"
    source.write_text("科研项目材料提交要求", encoding="utf-8")
    working_dir = tmp_path / "working"
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(working_dir))
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("GRAPH_CLASSIFICATION_ENABLED", "false")
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
        first_import_job_id = process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="repair-import-worker",
            queue_names={"IMPORT"},
        )
        assert first_import_job_id is not None

        with session_factory() as db:
            working_copy = db.query(WorkingCopy).one()
            working_root = db.get(WorkingCopyRoot, working_copy.working_copy_root_id)
            assert working_root is not None
            physical_path = working_dir / working_root.relative_storage_path / working_copy.relative_path
            assert physical_path.read_text(encoding="utf-8") == "科研项目材料提交要求"
            physical_path.unlink()
            second_scan = FilesystemJob(
                job_type="SCAN_MANAGED_ROOT",
                queue_name="SCAN",
                root_id=root_id,
                status="PENDING",
                payload_json={"root_key": "school_files", "reason": "repair-test"},
                result_json={},
            )
            db.add(second_scan)
            db.commit()

        assert process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="repair-scan-worker",
            queue_names={"SCAN"},
        )
        repaired_job_id = process_next_filesystem_job(
            session_factory=session_factory,
            worker_id="repair-import-worker",
            queue_names={"IMPORT"},
        )

        assert repaired_job_id == first_import_job_id
        assert physical_path.read_text(encoding="utf-8") == "科研项目材料提交要求"
        with session_factory() as db:
            repaired_job = db.get(FilesystemJob, repaired_job_id)
            assert repaired_job is not None
            assert repaired_job.status == "COMPLETED"
            assert repaired_job.result_json["physical_copy_repaired"] is True
            assert db.query(WorkingCopy).count() == 1
    finally:
        get_settings.cache_clear()
        clear_overrides()


def test_reconciliation_requeues_completed_scan_for_reused_parent_job(tmp_path: Path):
    """同一受管根在下一次启动对账时必须重新扫描，不能复用已完成子扫描后静默跳过。"""

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

        # scheduler 重用父任务后，同一 child deduplication key 也必须被重置为待执行。
        assert processor.process(parent) is True
        second_scan = db.get(FilesystemJob, first_scan_id)
        assert second_scan is not None
        assert second_scan.status == "PENDING"
        assert second_scan.queue_name == "SCAN"
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
