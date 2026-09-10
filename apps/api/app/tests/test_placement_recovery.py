"""D4/T23-T24 分类落位断点恢复测试。"""

from __future__ import annotations

import pytest

from app.db.models import ClassificationPlacementOperation, WorkingCopy
from app.modules.classification.placement_reconciler import ClassificationPlacementReconciler
from app.modules.classification.placement_service import ClassificationPlacementService
from app.modules.retrieval.search_profile import DocumentSearchProfileService
from app.tests.test_classification_placement import _command, _seed, _session, _submit


def test_t23_filesystem_applied_database_failure_recovers_without_second_move(monkeypatch, tmp_path):
    """FS_APPLIED 已提交、事务 B 失败后，恢复只补数据库事实且不再移动文件。"""

    db = _session()
    try:
        user, _copy, storage = _seed(db, monkeypatch, tmp_path)
        service, receipt = _submit(
            db,
            user,
            _command(revision=1, key="recover-after-db-failure", target_category_id="college.finance"),
            storage,
        )

        def fail_projection(*_args, **_kwargs):
            raise RuntimeError("injected projection database failure")

        monkeypatch.setattr(DocumentSearchProfileService, "upsert_current_profile", fail_projection)
        with pytest.raises(RuntimeError, match="injected projection"):
            service.execute(operation_id=receipt.operation_id, execution_token="worker-old")

        operation = db.get(ClassificationPlacementOperation, receipt.operation_id)
        assert operation.state == "RETRYABLE_FAILED"
        assert storage.working_copy_path("shared/working/其他/财务材料.txt").exists() is False
        assert storage.working_copy_path("shared/working/学院/财务管理/财务材料.txt").exists()
        assert db.get(WorkingCopy, _copy.id).relative_path == "其他/财务材料.txt"

        monkeypatch.undo()
        result = ClassificationPlacementReconciler(
            db,
            service=ClassificationPlacementService(db, storage=storage),
        ).reconcile(operation_id=receipt.operation_id, execution_token="worker-new")

        assert result["status"] == "COMMITTED"
        assert db.get(ClassificationPlacementOperation, receipt.operation_id).state == "COMMITTED"
        assert db.get(WorkingCopy, _copy.id).relative_path == "学院/财务管理/财务材料.txt"
    finally:
        db.close()
