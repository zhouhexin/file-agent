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
)
from app.modules.managed_files.worker import process_next_filesystem_job
from app.tests.helpers import clear_overrides, client_with_database


def test_worker_processes_scan_job_and_persists_files(tmp_path: Path):
    """worker 应能领取扫描任务、执行扫描并把结果写入 managed_files。"""

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

    response = client.post(
        "/api/conversations/managed-classification-worker-conv/messages",
        headers=headers,
        json={"content": "对党办下文件进行分类", "attachments": []},
    )

    assert response.status_code == 200
    initial_run = response.json()["agent_run"]
    assert initial_run["status"] == "WAITING_FOR_ASYNC_JOB"
    job_id = initial_run["tool_results"][0]["job_id"]

    processed_job_id = process_next_filesystem_job(
        session_factory=SessionLocal,
        worker_id="classification-worker-test",
    )

    assert processed_job_id == job_id
    db = SessionLocal()
    try:
        job = db.get(FilesystemJob, job_id)
        run = db.get(AgentRun, initial_run["agent_run_id"])
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
