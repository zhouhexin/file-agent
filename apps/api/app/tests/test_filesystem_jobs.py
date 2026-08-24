"""文件系统异步任务队列测试。"""

from app.db.models import FilesystemJob, FilesystemJobEvent, ManagedRoot, User
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.worker import _JobLeaseHeartbeat
from app.tests.helpers import clear_overrides, client_with_database


def test_filesystem_job_queue_claims_pending_job():
    """worker 应能领取 PENDING 扫描任务并标记为 RUNNING。"""

    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        root = ManagedRoot(root_key="student_affairs", display_name="学工收件箱", container_path="/managed/student-affairs")
        db.add(root)
        db.flush()
        queue = FilesystemJobQueue(db)
        job = queue.create_job(job_type="SCAN_MANAGED_ROOT", root_id=root.id, created_by=None, payload={})
        db.commit()

        claimed = queue.claim_next(worker_id="worker-1")

        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == "RUNNING"
        assert claimed.locked_by == "worker-1"
        assert db.query(FilesystemJobEvent).filter(FilesystemJobEvent.job_id == job.id).count() >= 1
    finally:
        db.close()
        clear_overrides()


def test_job_lease_heartbeat_renews_with_independent_session():
    """长推理必须在主业务事务之外续租，避免同一任务被重复领取。"""

    _client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        job = FilesystemJobQueue(db).create_job(
            job_type="STRUCTURED_IMAGE_EXTRACTION",
            queue_name="STRUCTURED_EXTRACTION",
            root_id=None,
            created_by=None,
            payload={},
        )
        db.commit()
        claimed = FilesystemJobQueue(db).claim_next(
            worker_id="structured-worker",
            queue_names={"STRUCTURED_EXTRACTION"},
        )
        assert claimed is not None
        db.commit()
        job_id = claimed.id
        previous_expiry = claimed.lease_expires_at
    finally:
        db.close()

    heartbeat = _JobLeaseHeartbeat(
        session_factory=SessionLocal,
        job_id=job_id,
        worker_id="structured-worker",
    )
    assert heartbeat._renew_once() is True

    db = SessionLocal()
    try:
        renewed = db.get(FilesystemJob, job_id)
        assert renewed.heartbeat_at is not None
        assert renewed.lease_expires_at >= previous_expiry
    finally:
        db.close()
        clear_overrides()


def test_promote_pending_job_does_not_reset_attempts_or_revive_failure():
    """检索提升优先级不能绕过三次重试上限或复活失败任务。"""

    _client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        queue = FilesystemJobQueue(db)
        pending = queue.create_job(
            job_type="IMPORT_WORKING_COPIES",
            root_id=None,
            created_by=None,
            payload={},
            priority=100,
        )
        pending.attempt_count = 2
        queue.promote_pending_job(job=pending, priority=10)
        assert pending.priority == 10
        assert pending.attempt_count == 2
        assert pending.max_attempts == 3

        pending.status = "FAILED"
        pending.attempt_count = 3
        queue.promote_pending_job(job=pending, priority=1)
        assert pending.status == "FAILED"
        assert pending.attempt_count == 3
        assert pending.priority == 10
    finally:
        db.close()
        clear_overrides()


def test_admin_scan_api_creates_pending_job(monkeypatch):
    """管理员触发扫描时只创建异步任务，不同步遍历文件系统。"""

    monkeypatch.setenv("MANAGED_ROOT_STUDENT_AFFAIRS", "/managed/student-affairs")
    client, SessionLocal = client_with_database()
    register_response = client.post(
        "/api/auth/register",
        json={"username": "scan-admin", "password": "password123", "display_name": "scan-admin"},
    )
    login_response = client.post("/api/auth/login", json={"username": "scan-admin", "password": "password123"})
    token = login_response.json()["access_token"]
    db = SessionLocal()
    try:
        user = db.get(User, register_response.json()["id"])
        user.role = "admin"
        db.commit()
    finally:
        db.close()
    root_response = client.post(
        "/api/admin/managed-roots",
        headers={"Authorization": f"Bearer {token}"},
        json={"root_key": "student_affairs", "display_name": "学工收件箱"},
    )

    response = client.post(
        f"/api/admin/managed-roots/{root_response.json()['id']}/scan",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "PENDING"
    clear_overrides()
