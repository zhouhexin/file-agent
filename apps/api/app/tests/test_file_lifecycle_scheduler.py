"""受管目录周期全量扫描调度保护测试。"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import get_settings
from app.db.models import FilesystemJob, ManagedRoot, utcnow
from app.modules.file_lifecycle.scheduler import _periodic_managed_root_scan_due
from app.tests.helpers import clear_overrides, client_with_database


def test_periodic_scan_waits_for_active_or_recent_scan(monkeypatch, tmp_path) -> None:
    """周期调度不得让大目录扫描连续运行，但历史扫描过期后仍可恢复兜底对账。"""

    monkeypatch.setenv("MANAGED_ROOT_FULL_SCAN_MIN_INTERVAL_SECONDS", "3600")
    get_settings.cache_clear()
    _client, session_factory = client_with_database()
    db = session_factory()
    try:
        root_dir = tmp_path / "managed-root"
        root_dir.mkdir()
        root = ManagedRoot(
            root_key="scheduled_root",
            display_name="周期扫描测试目录",
            container_path=str(root_dir),
        )
        db.add(root)
        db.flush()

        completed = FilesystemJob(
            job_type="SCAN_MANAGED_ROOT",
            queue_name="SCAN",
            root_id=root.id,
            status="COMPLETED",
            payload_json={},
            result_json={},
            finished_at=utcnow(),
        )
        db.add(completed)
        db.flush()
        assert _periodic_managed_root_scan_due(db=db, root_id=root.id) is False

        completed.finished_at = utcnow() - timedelta(seconds=3601)
        db.flush()
        assert _periodic_managed_root_scan_due(db=db, root_id=root.id) is True

        active = FilesystemJob(
            job_type="SCAN_MANAGED_ROOT",
            queue_name="SCAN",
            root_id=root.id,
            status="RUNNING",
            payload_json={},
            result_json={},
        )
        db.add(active)
        db.flush()
        assert _periodic_managed_root_scan_due(db=db, root_id=root.id) is False
    finally:
        db.close()
        get_settings.cache_clear()
        clear_overrides()
