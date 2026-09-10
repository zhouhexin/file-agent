"""分类落位中断后的同操作恢复入口。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import ClassificationPlacementOperation
from app.modules.classification.placement_service import (
    ClassificationPlacementService,
    PlacementServiceError,
)


class ClassificationPlacementReconciler:
    """只恢复已有冻结操作，绝不重新分类、换目标或创建新的用户授权。"""

    def __init__(self, db: Session, *, service: ClassificationPlacementService | None = None) -> None:
        self.db = db
        self.service = service or ClassificationPlacementService(db)

    def reconcile(self, *, operation_id: str, execution_token: str | None = None) -> dict:
        operation = self.db.get(ClassificationPlacementOperation, operation_id)
        if operation is None:
            raise PlacementServiceError("PLACEMENT_NOT_FOUND", "分类落位操作不存在")
        if operation.state == "COMMITTED":
            return dict(operation.result_json or {})
        if operation.state not in {"PREPARED", "EXECUTING", "FS_APPLIED", "RETRYABLE_FAILED", "RECONCILING"}:
            raise PlacementServiceError("PLACEMENT_NOT_RETRYABLE", "当前操作不能恢复")
        return self.service.execute(operation_id=operation_id, execution_token=execution_token)
