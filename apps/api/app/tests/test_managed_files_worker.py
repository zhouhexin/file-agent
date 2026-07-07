"""受管目录 worker 测试。"""

from pathlib import Path

from app.db.models import FilesystemJob, ManagedFile, ManagedRoot
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
