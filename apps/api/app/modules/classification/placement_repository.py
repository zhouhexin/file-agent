"""分类落位操作、幂等占用和目标路径保留的持久化仓库。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    ClassificationPlacementOperation,
    WorkingCopy,
    WorkingCopyPathReservation,
)
from app.modules.classification.placement_schemas import (
    PlacementAuthorizationContext,
    PlacementCommand,
)


ACTIVE_PLACEMENT_STATES = {
    "PREPARED",
    "EXECUTING",
    "FS_APPLIED",
    "RETRYABLE_FAILED",
    "RECONCILING",
}


class PlacementRepositoryError(ValueError):
    """仓库级并发、幂等或冻结事实冲突。"""

    def __init__(self, code: str, message: str, *, operation_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id


@dataclass(frozen=True, slots=True)
class PlacementOperationDraft:
    """路径和 taxonomy 均经服务层验证后的单文件冻结事实。"""

    working_copy: WorkingCopy
    source_sha256: str
    source_identity: dict[str, Any]
    before_primary_relation_id: str | None
    target_relative_path: str
    target_category_id: str
    taxonomy_key: str
    taxonomy_digest: str
    policy_version: str
    target_filename: str
    container_segments: list[str] = field(default_factory=list)
    decision_snapshot: dict[str, Any] = field(default_factory=dict)


class ClassificationPlacementRepository:
    """封装同键重试、活动写占用和规范路径占用。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def prepare(
        self,
        *,
        command: PlacementCommand,
        authorization: PlacementAuthorizationContext,
        draft: PlacementOperationDraft,
        operation_plan_id: str | None = None,
        authorization_source: str = "EXPLICIT_REQUEST",
    ) -> tuple[ClassificationPlacementOperation, bool]:
        """创建 PREPARED 操作；相同键同摘要返回原记录，不产生第二次写任务。"""

        working_copy = draft.working_copy
        working_copy_id = str(command.working_copy_id)
        if working_copy.id != working_copy_id:
            raise PlacementRepositoryError(
                "WORKING_COPY_SCOPE_MISMATCH",
                "命令对象与已校验工作副本不一致",
            )
        if working_copy.workspace_id != str(authorization.workspace_id):
            raise PlacementRepositoryError(
                "WORKSPACE_SCOPE_MISMATCH",
                "工作副本不在当前授权工作区",
            )
        digest = command.request_digest()
        if digest != authorization.request_digest:
            raise PlacementRepositoryError(
                "AUTHORIZATION_SNAPSHOT_MISMATCH",
                "命令摘要与授权快照不一致",
            )

        existing = self._idempotent_operation(
            authorization=authorization,
            command=command,
        )
        if existing is not None:
            if existing.request_digest != digest:
                raise PlacementRepositoryError(
                    "IDEMPOTENCY_CONFLICT",
                    "相同幂等键已用于不同参数",
                    operation_id=existing.id,
                )
            return existing, False

        active = (
            self.db.query(ClassificationPlacementOperation)
            .filter(
                ClassificationPlacementOperation.working_copy_id == working_copy_id,
                ClassificationPlacementOperation.state.in_(ACTIVE_PLACEMENT_STATES),
            )
            .with_for_update()
            .one_or_none()
        )
        if active is not None:
            raise PlacementRepositoryError(
                "PLACEMENT_IN_PROGRESS",
                "该工作副本已有未结束的写操作",
                operation_id=active.id,
            )

        operation = ClassificationPlacementOperation(
            workspace_id=working_copy.workspace_id,
            actor_user_id=str(authorization.actor_user_id),
            working_copy_id=working_copy.id,
            client_id=authorization.client_id,
            request_id=authorization.request_id,
            idempotency_key=command.idempotency_key,
            request_digest=digest,
            operation_type=command.action.value,
            operation_plan_id=operation_plan_id,
            authorization_source=authorization_source,
            expected_revision=command.expected_revision,
            expected_document_version_id=str(command.expected_document_version_id),
            source_sha256=draft.source_sha256,
            source_identity_json=dict(draft.source_identity),
            before_primary_relation_id=draft.before_primary_relation_id,
            before_relative_path=working_copy.relative_path,
            target_relative_path=draft.target_relative_path,
            target_category_id=draft.target_category_id,
            taxonomy_key=draft.taxonomy_key,
            taxonomy_version=command.taxonomy_version,
            taxonomy_digest=draft.taxonomy_digest,
            policy_version=draft.policy_version,
            target_filename=draft.target_filename,
            container_segments_json=list(draft.container_segments),
            decision_snapshot_json=dict(draft.decision_snapshot),
            authorization_snapshot_json=authorization.model_dump(mode="json"),
            state="PREPARED",
        )
        try:
            with self.db.begin_nested():
                self.db.add(operation)
                self.db.flush()
        except IntegrityError as exc:
            concurrent = self._idempotent_operation(
                authorization=authorization,
                command=command,
            )
            if concurrent is not None and concurrent.request_digest == digest:
                return concurrent, False
            raise PlacementRepositoryError(
                "PLACEMENT_IN_PROGRESS",
                "并发请求已占用该工作副本或幂等键",
                operation_id=concurrent.id if concurrent is not None else None,
            ) from exc
        return operation, True

    def reserve_target_path(
        self,
        *,
        operation: ClassificationPlacementOperation,
        root_id: str,
        normalized_relative_path: str,
    ) -> WorkingCopyPathReservation:
        """占用规范路径；哈希相同时还核对完整规范路径，防止摘要碰撞被忽略。"""

        normalized = normalized_relative_path.replace("\\", "/").casefold()
        path_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        existing = (
            self.db.query(WorkingCopyPathReservation)
            .filter(
                WorkingCopyPathReservation.root_id == root_id,
                WorkingCopyPathReservation.normalized_path_hash == path_hash,
            )
            .with_for_update()
            .one_or_none()
        )
        if existing is not None:
            if (
                existing.placement_operation_id == operation.id
                and existing.normalized_relative_path == normalized_relative_path
            ):
                return existing
            raise PlacementRepositoryError(
                "TARGET_NAME_CONFLICT",
                "目标路径已被其他文件操作占用",
                operation_id=existing.placement_operation_id,
            )
        reservation = WorkingCopyPathReservation(
            root_id=root_id,
            normalized_path_hash=path_hash,
            normalized_relative_path=normalized_relative_path,
            placement_operation_id=operation.id,
        )
        self.db.add(reservation)
        self.db.flush()
        return reservation

    def _idempotent_operation(
        self,
        *,
        authorization: PlacementAuthorizationContext,
        command: PlacementCommand,
    ) -> ClassificationPlacementOperation | None:
        """按文档固定的五列唯一键读取原操作。"""

        return (
            self.db.query(ClassificationPlacementOperation)
            .filter(
                ClassificationPlacementOperation.workspace_id
                == str(authorization.workspace_id),
                ClassificationPlacementOperation.actor_user_id
                == str(authorization.actor_user_id),
                ClassificationPlacementOperation.client_id == authorization.client_id,
                ClassificationPlacementOperation.idempotency_key
                == command.idempotency_key,
                ClassificationPlacementOperation.working_copy_id
                == str(command.working_copy_id),
            )
            .one_or_none()
        )
