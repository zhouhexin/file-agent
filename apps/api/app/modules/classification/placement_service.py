"""分类 PRIMARY/目录落位的事务协调服务。

公开入口只负责把 ``PlacementCommand`` 与服务端构造的授权上下文提交到
``submit``。文件 worker 只接受已持久化的 operation ID 调用 ``execute``；两者
都不能传入任意宿主路径、文件名或跳过确认标记。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    ChangeItem,
    ChangeSet,
    ClassificationGraphOutbox,
    ClassificationPlacementOperation,
    DocumentCategory,
    DocumentCategoryConfirmationSource,
    DocumentCategoryFeedback,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentOrganizationDecision,
    DocumentVersion,
    FileObject,
    FilesystemJob,
    OperationPlan,
    ToolInvocation,
    User,
    WorkingCopy,
    WorkingCopyPathRecord,
    WorkingCopyPathReservation,
    WorkingCopyRoot,
    utcnow,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.organization_path import (
    CategoryOrganizationPathError,
    CategoryOrganizationPathResolver,
    CategoryOrganizationTarget,
)
from app.modules.classification.placement_authorization import (
    PlacementAuthorizationError,
    PlacementAuthorizationService,
)
from app.modules.classification.placement_repository import (
    ClassificationPlacementRepository,
    PlacementOperationDraft,
    PlacementRepositoryError,
)
from app.modules.classification.placement_schemas import (
    PlacementAction,
    PlacementAuthorizationContext,
    PlacementAuthorizationMode,
    PlacementCommand,
)
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.file_lifecycle.working_copy_executor import (
    WorkingCopyExecutionError,
    WorkingCopyExecutor,
    WorkingCopyFileIdentity,
)
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.retrieval.search_profile import DocumentSearchProfileService


class PlacementServiceError(ValueError):
    """面向 API/Tool 的稳定分类落位错误。"""

    def __init__(self, code: str, message: str, *, operation_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id


@dataclass(frozen=True, slots=True)
class PlacementSubmission:
    """事务 A 的可序列化受理结果。"""

    operation_id: str
    status: str
    created: bool
    working_copy_id: str
    effective_primary: dict[str, Any] | None
    pending_primary: dict[str, Any]
    placement_status: str
    requires_confirmation: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "created": self.created,
            "working_copy_id": self.working_copy_id,
            "effective_primary": self.effective_primary,
            "pending_primary": self.pending_primary,
            "placement_status": self.placement_status,
            "requires_confirmation": self.requires_confirmation,
            # 受理阶段尚未触碰文件系统，不能伪造“已移动”或“未移动”。
            "file_position_changed": None,
        }


class ClassificationPlacementService:
    """协调事务 A、无覆盖文件动作和事务 B，确保分类与目录同时生效。"""

    def __init__(
        self,
        db: Session,
        *,
        storage: FileLifecycleStorageService | None = None,
        authorization_service: PlacementAuthorizationService | None = None,
        executor: WorkingCopyExecutor | None = None,
        policy_version: str = "workdata-v1",
    ) -> None:
        self.db = db
        self.storage = storage or FileLifecycleStorageService()
        self.authorization_service = authorization_service or PlacementAuthorizationService(db)
        self.executor = executor or WorkingCopyExecutor(self.storage)
        self.policy_version = policy_version
        self.repository = ClassificationPlacementRepository(db)

    def submit(
        self,
        *,
        command: PlacementCommand,
        authorization: PlacementAuthorizationContext,
        feedback_context: dict[str, Any] | None = None,
        record_tool_invocation: bool = True,
    ) -> PlacementSubmission:
        """事务 A：冻结授权、源身份、目标目录并创建 plan/operation/job。

        该方法不移动文件；调用方提交当前数据库事务后，文件 worker 才能看到任务。
        ``feedback_context`` 仅允许服务端反馈入口传入，用于事务 B 绑定真实反馈，
        不能作为 HTTP/MCP 命令字段暴露。
        """

        if authorization.authorization_mode != PlacementAuthorizationMode.EXPLICIT_REQUEST:
            raise PlacementServiceError("AUTHORIZATION_MODE_INVALID", "公开落位提交必须来自明确请求授权")
        if command.action not in {PlacementAction.SET_PRIMARY, PlacementAction.MOVE}:
            raise PlacementServiceError("OPERATION_NOT_AUTHORIZED", "只支持主分类更正或受控移动")
        working_copy = self._load_submission_copy(command=command, authorization=authorization)
        working_root = self.db.get(WorkingCopyRoot, working_copy.working_copy_root_id)
        version = self.db.get(DocumentVersion, working_copy.current_version_id)
        if working_root is None or version is None:
            raise PlacementServiceError("WORKING_COPY_LINEAGE_MISSING", "工作副本缺少根或当前版本")
        target = self._resolve_target(
            command=command,
            working_copy=working_copy,
            working_root=working_root,
        )
        primary = self._active_primary(working_copy)
        effective_primary = _relation_view(primary)
        expected_identity = self.executor.capture_source_identity(
            root_relative_path=working_root.relative_storage_path,
            source_relative_path=working_copy.relative_path,
        )
        if expected_identity.sha256 != working_copy.content_sha256 or expected_identity.sha256 != version.sha256:
            raise PlacementServiceError("SOURCE_IDENTITY_CHANGED", "当前工作副本内容与数据库快照不一致")

        plan = OperationPlan(
            workspace_id=working_copy.workspace_id,
            conversation_id=(str(authorization.conversation_id) if authorization.conversation_id else None),
            user_id=str(authorization.actor_user_id),
            operation_type="CLASSIFICATION_PLACEMENT",
            status="AUTHORIZED",
            risk_level="medium",
            reason="用户明确提交主分类更正或受控移动。",
            authorization_mode=PlacementAuthorizationMode.EXPLICIT_REQUEST.value,
            authorization_context_json=authorization.audit_snapshot(),
            plan_json={
                "schema_version": "classification-placement-plan-v1",
                "request_digest": command.request_digest(),
                "action": command.action.value,
                "items": [],
            },
        )
        self.db.add(plan)
        self.db.flush()
        taxonomy = load_default_taxonomy()
        source_identity = {
            "working_copy_root_id": working_root.id,
            "root_relative_storage_path": working_root.relative_storage_path,
            "file_identity": expected_identity.as_json(),
        }
        draft = PlacementOperationDraft(
            working_copy=working_copy,
            source_sha256=expected_identity.sha256,
            source_identity=source_identity,
            before_primary_relation_id=primary.id if primary is not None else None,
            target_relative_path=target.target_relative_path,
            target_category_id=target.category_id,
            taxonomy_key=target.taxonomy_key,
            taxonomy_digest=hashlib.sha256(taxonomy.model_dump_json().encode("utf-8")).hexdigest(),
            policy_version=self.policy_version,
            target_filename=working_copy.filename,
            container_segments=list(target.container_segments),
            decision_snapshot={
                "schema_version": "classification-placement-decision-v1",
                "category_id": target.category_id,
                "category_path": list(target.organization_path),
                "container_segments": list(target.container_segments),
                "target_relative_path": target.target_relative_path,
                "classification_outcome": (
                    "OTHER" if target.category_id == "system.other" else "CLASSIFIED"
                ),
                "selection_basis": "EXPLICIT_REQUEST",
                **(
                    {"feedback_context": _normalize_feedback_context(feedback_context)}
                    if feedback_context is not None
                    else {}
                ),
            },
        )
        try:
            operation, created = self.repository.prepare(
                command=command,
                authorization=authorization,
                draft=draft,
                operation_plan_id=plan.id,
                authorization_source=PlacementAuthorizationMode.EXPLICIT_REQUEST.value,
            )
        except PlacementRepositoryError as exc:
            # Plan 尚未被独立提交；调用方事务回滚即可保证没有半成品计划。
            raise PlacementServiceError(exc.code, str(exc), operation_id=exc.operation_id) from exc
        if not created:
            # 同键重放不能创建第二个 plan、job、占用或审计项。
            self.db.delete(plan)
            return self._submission_for_existing(operation)

        self.repository.reserve_target_path(
            operation=operation,
            root_id=working_root.id,
            normalized_relative_path=target.target_relative_path,
        )
        changeset = ChangeSet(
            workspace_id=working_copy.workspace_id,
            conversation_id=(str(authorization.conversation_id) if authorization.conversation_id else None),
            agent_run_id=None,
            placement_operation_id=operation.id,
            user_id=str(authorization.actor_user_id),
            status="PENDING",
            summary="已受理分类落位，等待受控文件执行。",
        )
        self.db.add(changeset)
        self.db.flush()
        operation.changeset_id = changeset.id
        job = FilesystemJobQueue(self.db).create_job(
            job_type="EXECUTE_CLASSIFICATION_PLACEMENT",
            queue_name="FILE_OPERATION",
            root_id=working_root.managed_root_id,
            created_by=str(authorization.actor_user_id),
            deduplication_key=f"classification-placement:{operation.id}",
            priority=20,
            payload={"placement_operation_id": operation.id},
        )
        operation.job_id = job.id
        if record_tool_invocation:
            invocation = ToolInvocation(
                agent_run_id=None,
                placement_operation_id=operation.id,
                tool_name="working-copy-placement-submit",
                input_json={
                    "working_copy_id": working_copy.id,
                    "action": command.action.value,
                    "expected_revision": command.expected_revision,
                    "expected_document_version_id": str(command.expected_document_version_id),
                    "target_category_id": target.category_id,
                    "taxonomy_version": command.taxonomy_version,
                    "idempotency_key": command.idempotency_key,
                },
                output_json={"operation_id": operation.id, "status": "PREPARED", "job_id": job.id},
                status="COMPLETED",
                changeset_id=changeset.id,
                operation_plan_id=plan.id,
                finished_at=utcnow(),
            )
            self.db.add(invocation)
        plan.plan_json = {
            **dict(plan.plan_json or {}),
            "items": [
                {
                    "placement_operation_id": operation.id,
                    "working_copy_id": working_copy.id,
                    "before_relative_path": working_copy.relative_path,
                    "target_relative_path": target.target_relative_path,
                    "target_category_id": target.category_id,
                    "job_id": job.id,
                }
            ],
        }
        working_copy.placement_status = "PENDING"
        working_copy.placement_policy_version = self.policy_version
        self.db.flush()
        return PlacementSubmission(
            operation_id=operation.id,
            status="PREPARED",
            created=True,
            working_copy_id=working_copy.id,
            effective_primary=effective_primary,
            pending_primary={
                "category_id": target.category_id,
                "category_path": list(target.organization_path),
            },
            placement_status="PENDING",
        )

    def execute(
        self,
        *,
        operation_id: str,
        execution_token: str | None = None,
    ) -> dict[str, Any]:
        """worker 入口：持久化执行意图、执行无覆盖移动，再以事务 B 提交事实。"""

        operation = self.db.get(ClassificationPlacementOperation, operation_id)
        if operation is None:
            raise PlacementServiceError("PLACEMENT_NOT_FOUND", "分类落位操作不存在")
        if operation.state == "COMMITTED":
            return dict(operation.result_json or {})
        if operation.state in {"FAILED", "CANCELLED"}:
            raise PlacementServiceError("PLACEMENT_NOT_RETRYABLE", "该分类落位操作不能继续执行")
        plan = self.db.get(OperationPlan, operation.operation_plan_id)
        if plan is None:
            raise PlacementServiceError("PLAN_NOT_FOUND", "分类落位缺少内部操作计划")
        try:
            context = PlacementAuthorizationContext.model_validate(operation.authorization_snapshot_json)
            self.authorization_service.authorize_execution(
                plan=plan,
                operation=operation,
                context=context,
            )
        except (PlacementAuthorizationError, ValueError) as exc:
            self._persist_failure(operation_id, "AUTHORIZATION_REVOKED", str(exc), terminal=True)
            raise PlacementServiceError("AUTHORIZATION_REVOKED", "执行授权已失效") from exc
        actor = self.db.get(User, operation.actor_user_id)
        if actor is None:
            self._persist_failure(operation_id, "ACTOR_NOT_FOUND", "发起用户不存在", terminal=True)
            raise PlacementServiceError("ACTOR_NOT_FOUND", "发起用户不存在")
        self._begin_execution(operation_id=operation_id, execution_token=execution_token)
        try:
            operation = self.db.get(ClassificationPlacementOperation, operation_id)
            if operation is None:
                raise PlacementServiceError("PLACEMENT_NOT_FOUND", "分类落位操作不存在")
            working_copy, root, expected_identity = self._execution_lineage(operation)
            move_result = self.executor.apply_move(
                operation_id=operation.id,
                working_copy_id=working_copy.id,
                root_relative_path=root.relative_storage_path,
                source_relative_path=operation.before_relative_path,
                target_relative_path=operation.target_relative_path,
                expected_identity=expected_identity,
            )
            self._record_filesystem_applied(
                operation_id=operation.id,
                execution_token=execution_token,
                target_identity=move_result.target_identity,
            )
            return self._commit_metadata(operation_id=operation.id, execution_token=execution_token)
        except WorkingCopyExecutionError as exc:
            self._persist_failure(
                operation_id,
                exc.code,
                str(exc),
                terminal=exc.code in {"SOURCE_IDENTITY_CHANGED", "TARGET_NAME_CONFLICT"},
                reconciling=exc.code == "PLACEMENT_RECONCILIATION_REQUIRED",
            )
            raise PlacementServiceError(exc.code, str(exc), operation_id=operation_id) from exc
        except PlacementServiceError:
            raise
        except Exception as exc:
            self._persist_failure(operation_id, "PLACEMENT_EXECUTION_FAILED", str(exc), terminal=False)
            raise

    def _load_submission_copy(
        self,
        *,
        command: PlacementCommand,
        authorization: PlacementAuthorizationContext,
    ) -> WorkingCopy:
        working_copy = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.id == str(command.working_copy_id))
            .with_for_update()
            .one_or_none()
        )
        if working_copy is None or working_copy.status != "ACTIVE":
            raise PlacementServiceError("WORKING_COPY_NOT_FOUND", "活动工作副本不存在")
        if working_copy.workspace_id != str(authorization.workspace_id):
            raise PlacementServiceError("FORBIDDEN", "当前请求无权操作该工作副本")
        if working_copy.revision != command.expected_revision:
            raise PlacementServiceError("WORKING_COPY_REVISION_CONFLICT", "工作副本已发生变化，请刷新后重试")
        if working_copy.current_version_id != str(command.expected_document_version_id):
            raise PlacementServiceError("WORKING_COPY_VERSION_CONFLICT", "工作副本当前版本已变化，请刷新后重试")
        return working_copy

    def _resolve_target(
        self,
        *,
        command: PlacementCommand,
        working_copy: WorkingCopy,
        working_root: WorkingCopyRoot,
    ) -> CategoryOrganizationTarget:
        taxonomy = load_default_taxonomy()
        if command.taxonomy_version != taxonomy.version:
            raise PlacementServiceError("TAXONOMY_VERSION_STALE", "分类目录版本已更新，请刷新后重试")
        resolver = CategoryOrganizationPathResolver(self.storage)
        try:
            if command.target_category_id:
                return resolver.resolve_category(
                    category_id=command.target_category_id,
                    taxonomy_key=taxonomy.key,
                    taxonomy_version=command.taxonomy_version,
                    working_copy=working_copy,
                    working_root=working_root,
                    container_segments=command.container_segments,
                )
            reversed_target = resolver.reverse_resolve_directory(
                target_root_key=str(command.target_root_key or ""),
                target_directory_segments=command.target_directory_segments,
                working_root=working_root,
            )
            return resolver.resolve_category(
                category_id=reversed_target.category_id,
                taxonomy_key=reversed_target.taxonomy_key,
                taxonomy_version=reversed_target.taxonomy_version,
                working_copy=working_copy,
                working_root=working_root,
                container_segments=reversed_target.container_segments,
            )
        except CategoryOrganizationPathError as exc:
            raise PlacementServiceError("CATEGORY_NOT_PLACEABLE", str(exc)) from exc

    def _submission_for_existing(self, operation: ClassificationPlacementOperation) -> PlacementSubmission:
        """幂等重放直接返回原操作的真实状态，不生成第二项审计。"""

        result = dict(operation.result_json or {})
        return PlacementSubmission(
            operation_id=operation.id,
            status=operation.state,
            created=False,
            working_copy_id=operation.working_copy_id,
            effective_primary=result.get("effective_primary"),
            pending_primary=(
                result.get("pending_primary")
                or {"category_id": operation.target_category_id}
            ),
            placement_status=str(result.get("placement_status") or "PENDING"),
        )

    def _active_primary(self, working_copy: WorkingCopy) -> DocumentCategory | None:
        return (
            self.db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id == working_copy.id,
                DocumentCategory.document_version_id == working_copy.current_version_id,
                DocumentCategory.relation_role == "PRIMARY",
                DocumentCategory.status.in_({"AUTO_APPLIED", "CONFIRMED"}),
            )
            .with_for_update()
            .one_or_none()
        )

    def _begin_execution(self, *, operation_id: str, execution_token: str | None) -> None:
        """在实际文件动作前单独提交执行意图，避免 worker 崩溃丢失阶段事实。"""

        operation = (
            self.db.query(ClassificationPlacementOperation)
            .filter(ClassificationPlacementOperation.id == operation_id)
            .with_for_update()
            .one()
        )
        if operation.state == "COMMITTED":
            return
        if operation.state not in {"PREPARED", "RETRYABLE_FAILED", "FS_APPLIED", "RECONCILING"}:
            raise PlacementServiceError("PLACEMENT_IN_PROGRESS", "分类落位正在由其他 worker 执行")
        operation.state = "EXECUTING"
        operation.attempt_count += 1
        operation.execution_token = execution_token or operation.execution_token or operation.id
        operation.lease_expires_at = None
        operation.error_json = {}
        plan = self.db.get(OperationPlan, operation.operation_plan_id)
        if plan is not None:
            plan.status = "EXECUTING"
        self.db.commit()

    def _execution_lineage(
        self, operation: ClassificationPlacementOperation
    ) -> tuple[WorkingCopy, WorkingCopyRoot, WorkingCopyFileIdentity]:
        working_copy = self.db.get(WorkingCopy, operation.working_copy_id)
        root = self.db.get(WorkingCopyRoot, working_copy.working_copy_root_id) if working_copy else None
        if working_copy is None or root is None:
            raise PlacementServiceError("WORKING_COPY_LINEAGE_MISSING", "工作副本链路已失效")
        if (
            working_copy.revision != operation.expected_revision
            or working_copy.current_version_id != operation.expected_document_version_id
            or working_copy.relative_path != operation.before_relative_path
        ):
            raise PlacementServiceError("WORKING_COPY_REVISION_CONFLICT", "工作副本在执行前已变化")
        source_identity = dict(operation.source_identity_json or {})
        if source_identity.get("working_copy_root_id") != root.id:
            raise PlacementServiceError("SOURCE_IDENTITY_CHANGED", "工作副本根已变化")
        identity = WorkingCopyFileIdentity.from_json(
            dict(source_identity.get("file_identity") or {})
        )
        if identity.sha256 != operation.source_sha256:
            raise PlacementServiceError("SOURCE_IDENTITY_CHANGED", "冻结内容哈希不一致")
        return working_copy, root, identity

    def _record_filesystem_applied(
        self,
        *,
        operation_id: str,
        execution_token: str | None,
        target_identity: WorkingCopyFileIdentity,
    ) -> None:
        """文件已移动但尚未变更分类/路径投影时持久化 FS_APPLIED。"""

        operation = self.db.get(ClassificationPlacementOperation, operation_id)
        if operation is None:
            raise PlacementServiceError("PLACEMENT_NOT_FOUND", "分类落位操作不存在")
        if execution_token and operation.execution_token not in {execution_token, operation.id}:
            raise PlacementServiceError("PLACEMENT_EXECUTION_TOKEN_STALE", "worker 租约已失效")
        operation.state = "FS_APPLIED"
        operation.result_json = {
            **dict(operation.result_json or {}),
            "filesystem_target_identity": target_identity.as_json(),
            "filesystem_applied": True,
        }
        self.db.commit()

    def _commit_metadata(self, *, operation_id: str, execution_token: str | None) -> dict[str, Any]:
        """事务 B：一次性提交正式关系、路径、版本、审计、投影和 outbox。"""

        operation = (
            self.db.query(ClassificationPlacementOperation)
            .filter(ClassificationPlacementOperation.id == operation_id)
            .with_for_update()
            .one()
        )
        if operation.state == "COMMITTED":
            return dict(operation.result_json or {})
        if operation.state != "FS_APPLIED":
            raise PlacementServiceError("PLACEMENT_RECONCILIATION_REQUIRED", "文件阶段尚未完成")
        if execution_token and operation.execution_token not in {execution_token, operation.id}:
            raise PlacementServiceError("PLACEMENT_EXECUTION_TOKEN_STALE", "worker 租约已失效")
        working_copy, root, expected_identity = self._execution_lineage(operation)
        target_identity = WorkingCopyFileIdentity.from_json(
            dict((operation.result_json or {}).get("filesystem_target_identity") or {})
        )
        actual_target = self.executor.capture_source_identity(
            root_relative_path=root.relative_storage_path,
            source_relative_path=operation.target_relative_path,
        )
        if not target_identity.matches(actual_target) or not expected_identity.matches(actual_target):
            raise PlacementServiceError("PLACEMENT_RECONCILIATION_REQUIRED", "目标文件身份已变化")

        feedback, suggestion = self._resolve_feedback_context(operation=operation)
        existing_primary = self._active_primary(working_copy)
        relation_changed = (
            existing_primary is None
            or existing_primary.category_id != operation.target_category_id
        )
        source_changed = False
        path_changed = working_copy.relative_path != operation.target_relative_path
        if relation_changed:
            now = utcnow()
            active_primaries = (
                self.db.query(DocumentCategory)
                .filter(
                    DocumentCategory.working_copy_id == working_copy.id,
                    DocumentCategory.document_version_id == working_copy.current_version_id,
                    DocumentCategory.relation_role == "PRIMARY",
                    DocumentCategory.status.in_({"AUTO_APPLIED", "CONFIRMED"}),
                )
                .with_for_update()
                .all()
            )
            for relation in active_primaries:
                relation.status = "ENDED"
                relation.ended_at = now
                relation.updated_at = now
                self._enqueue_outbox(relation, source_key=f"placement-ended:{operation.id}")
            self.db.flush()
            category_path = list(
                (operation.decision_snapshot_json or {}).get("category_path") or []
            )
            classification_run = (
                self.db.get(DocumentClassificationRun, suggestion.classification_run_id)
                if suggestion is not None
                else None
            )
            new_primary = DocumentCategory(
                working_copy_id=working_copy.id,
                document_id=working_copy.document_id,
                document_version_id=str(working_copy.current_version_id or ""),
                category_id=operation.target_category_id,
                category_path_json=category_path,
                relation_role="PRIMARY",
                status="CONFIRMED",
                taxonomy_key=operation.taxonomy_key,
                taxonomy_version=operation.taxonomy_version,
                classifier_version=(
                    classification_run.classifier_version
                    if classification_run is not None
                    else "placement-service-v1"
                ),
                source=("user_confirmed" if feedback is not None else "placement_explicit_request"),
                source_suggestion_id=(suggestion.id if suggestion is not None else None),
                evidence_json=(list(suggestion.evidence_json or []) if suggestion is not None else []),
            )
            self.db.add(new_primary)
            self.db.flush()
            self._enqueue_outbox(new_primary, source_key=f"placement-applied:{operation.id}")
        else:
            new_primary = existing_primary
            if feedback is not None and suggestion is not None:
                # 同类确认不能假装无事发生：AUTO_APPLIED 升级为人工确认，或新的
                # 用户反馈成为该共享关系的可追溯来源，均属于一次真实元数据变更。
                if (
                    new_primary.status != "CONFIRMED"
                    or new_primary.source != "user_confirmed"
                    or new_primary.source_suggestion_id != suggestion.id
                    or list(new_primary.evidence_json or []) != list(suggestion.evidence_json or [])
                ):
                    new_primary.status = "CONFIRMED"
                    new_primary.source = "user_confirmed"
                    new_primary.source_suggestion_id = suggestion.id
                    new_primary.evidence_json = list(suggestion.evidence_json or [])
                    new_primary.updated_at = utcnow()
                    relation_changed = True
                    self._enqueue_outbox(
                        new_primary,
                        source_key=f"placement-confirmed:{operation.id}",
                    )

        if feedback is not None and suggestion is not None and new_primary is not None:
            source_changed = self._apply_feedback_confirmation_source(
                relation=new_primary,
                feedback=feedback,
                suggestion=suggestion,
                actor_user_id=operation.actor_user_id,
            )
            feedback.application_status = "APPLIED"
            feedback.placement_operation_id = operation.id

        version = self.db.get(DocumentVersion, working_copy.current_version_id)
        if version is None:
            raise PlacementServiceError("WORKING_COPY_LINEAGE_MISSING", "工作副本当前版本不存在")
        now = utcnow()
        before_path = working_copy.relative_path
        if path_changed:
            working_copy.relative_path = operation.target_relative_path
            working_copy.relative_path_hash = hashlib.sha256(
                operation.target_relative_path.encode("utf-8")
            ).hexdigest()
            working_copy.filename = operation.target_filename
            working_copy.extension = PurePosixPath(operation.target_filename).suffix.lower()
            version.storage_path = f"{root.relative_storage_path}/{operation.target_relative_path}"
            version.filename = operation.target_filename
            file_objects = (
                self.db.query(FileObject)
                .filter(
                    FileObject.document_id == working_copy.document_id,
                    FileObject.storage_backend == "working_copy_local",
                    FileObject.storage_path == f"{root.relative_storage_path}/{before_path}",
                    FileObject.sha256 == operation.source_sha256,
                )
                .all()
            )
            if len(file_objects) > 1:
                raise PlacementServiceError("FILE_OBJECT_AMBIGUOUS", "当前工作副本文件对象不唯一")
            if file_objects:
                file_objects[0].storage_path = version.storage_path
        changed = relation_changed or path_changed or source_changed
        if changed:
            working_copy.revision += 1
        working_copy.placement_status = "IN_SYNC"
        working_copy.placement_policy_version = operation.policy_version
        working_copy.last_operation_plan_id = operation.operation_plan_id
        working_copy.updated_at = now

        path_record = None
        if path_changed:
            sequence_number = int(
                self.db.query(func.max(WorkingCopyPathRecord.sequence_number))
                .filter(WorkingCopyPathRecord.working_copy_id == working_copy.id)
                .scalar()
                or 0
            ) + 1
            path_record = WorkingCopyPathRecord(
                working_copy_id=working_copy.id,
                sequence_number=sequence_number,
                operation_type="CLASSIFICATION_PLACEMENT",
                before_relative_path=before_path,
                after_relative_path=operation.target_relative_path,
                before_filename=PurePosixPath(before_path).name,
                after_filename=operation.target_filename,
                document_version_id=version.id,
                content_sha256=operation.source_sha256,
                operation_plan_id=operation.operation_plan_id,
                placement_operation_id=operation.id,
                status="COMPLETED",
                executed_by=operation.actor_user_id,
            )
            self.db.add(path_record)
            self.db.flush()

        decision = DocumentOrganizationDecision(
            working_copy_id=working_copy.id,
            document_id=working_copy.document_id,
            document_version_id=version.id,
            primary_suggestion_id=None,
            category_id=operation.target_category_id,
            taxonomy_key=operation.taxonomy_key,
            taxonomy_version=operation.taxonomy_version,
            classifier_version="placement-service-v1",
            calibration_version="explicit-request",
            policy_version=operation.policy_version,
            authorization_source=operation.authorization_source,
            source_request_id=operation.request_id,
            before_revision=operation.expected_revision,
            after_revision=working_copy.revision,
            decision=("APPLIED_OTHER" if operation.target_category_id == "system.other" else "APPLIED_BUSINESS"),
            calibrated_confidence=None,
            required_threshold=None,
            top_margin=None,
            required_margin=None,
            feature_snapshot_json={"placement_operation_id": operation.id},
            reason_codes_json=["EXPLICIT_REQUEST"],
            target_relative_path_snapshot=operation.target_relative_path,
            path_record_id=path_record.id if path_record else None,
            placement_operation_id=operation.id,
            idempotency_key=f"placement:{operation.id}",
            completed_at=now,
        )
        self.db.add(decision)
        changeset = self.db.get(ChangeSet, operation.changeset_id)
        if changeset is None:
            raise PlacementServiceError("CHANGESET_MISSING", "分类落位缺少审计变更集")
        if relation_changed or source_changed:
            self.db.add(
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="document_category",
                    target_id=new_primary.id if new_primary else None,
                    target_document_id=working_copy.document_id,
                    change_type=("CATEGORY_ADDED" if existing_primary is None else "CATEGORY_CONFIRMED"),
                    before_value_json=_relation_view(existing_primary) or {},
                    after_value_json=_relation_view(new_primary) or {},
                    source=("placement_feedback" if feedback is not None else "placement_explicit_request"),
                    confidence=1.0,
                    evidence_json=(
                        {"source_suggestion_id": suggestion.id}
                        if suggestion is not None
                        else {}
                    ),
                    execution_status="COMPLETED",
                )
            )
        if path_changed:
            self.db.add(
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="working_copy",
                    target_id=working_copy.id,
                    target_document_id=working_copy.document_id,
                    change_type="FILE_MOVED",
                    before_value_json={"relative_path": before_path},
                    after_value_json={"relative_path": operation.target_relative_path},
                    source="placement_explicit_request",
                    confidence=1.0,
                    evidence_json={},
                    execution_status="COMPLETED",
                )
            )
        changeset.status = "COMPLETED"
        changeset.summary = (
            "主分类、确认来源和工作副本目录已同步更新。"
            if changed
            else "目标主分类、确认来源和目录已一致，无需变更。"
        )
        plan = self.db.get(OperationPlan, operation.operation_plan_id)
        if plan is not None:
            plan.status = "EXECUTED"
            plan.executed_at = now
        result = {
            "operation_id": operation.id,
            "status": "COMMITTED",
            "working_copy_id": working_copy.id,
            "working_copy_revision": working_copy.revision,
            "effective_primary": _relation_view(new_primary),
            "pending_primary": None,
            "placement_status": "IN_SYNC",
            "relative_path": operation.target_relative_path,
            "file_position_changed": path_changed,
            "original_unchanged": True,
            "index_status": "READY",
            "requires_confirmation": False,
            "feedback_application_status": (
                feedback.application_status if feedback is not None else None
            ),
        }
        operation.state = "COMMITTED"
        operation.result_json = result
        operation.completed_at = now
        operation.execution_token = None
        self.db.query(WorkingCopyPathReservation).filter(
            WorkingCopyPathReservation.placement_operation_id == operation.id
        ).delete(synchronize_session=False)
        invocation = (
            self.db.query(ToolInvocation)
            .filter(
                ToolInvocation.placement_operation_id == operation.id,
                ToolInvocation.tool_name == "working-copy-placement-submit",
            )
            .one_or_none()
        )
        if invocation is not None:
            invocation.output_json = {**dict(invocation.output_json or {}), **result}
        DocumentSearchProfileService(db=self.db).upsert_current_profile(working_copy.id)
        self.db.commit()
        return result

    def _resolve_feedback_context(
        self,
        *,
        operation: ClassificationPlacementOperation,
    ) -> tuple[DocumentCategoryFeedback | None, DocumentCategorySuggestion | None]:
        """读取并校验由事务 A 冻结的真实反馈关联。

        普通 API/MCP 的 SET_PRIMARY 没有 suggestion/feedback，必须返回空关联；
        绝不能为它们补造一条反馈。来自建议的 PRIMARY 决定则必须保持 actor、工作
        副本、版本、目标分类与冻结快照一致，防止 worker 把过期反馈应用到新版本。
        """

        context = dict((operation.decision_snapshot_json or {}).get("feedback_context") or {})
        if not context:
            return None, None
        feedback_id = str(context.get("feedback_id") or "")
        suggestion_id = str(context.get("suggestion_id") or "")
        if not feedback_id or not suggestion_id:
            raise PlacementServiceError("FEEDBACK_CONTEXT_INVALID", "分类反馈关联不完整")
        feedback = (
            self.db.query(DocumentCategoryFeedback)
            .filter(DocumentCategoryFeedback.id == feedback_id)
            .with_for_update()
            .one_or_none()
        )
        suggestion = self.db.get(DocumentCategorySuggestion, suggestion_id)
        if feedback is None or suggestion is None:
            raise PlacementServiceError("FEEDBACK_CONTEXT_INVALID", "分类反馈或建议已不存在")
        if (
            feedback.user_id != operation.actor_user_id
            or feedback.working_copy_id != operation.working_copy_id
            or feedback.document_version_id != operation.expected_document_version_id
            or feedback.suggestion_id != suggestion.id
            or suggestion.document_version_id != operation.expected_document_version_id
            or str(context.get("relation_role")) != "PRIMARY"
            or str(context.get("target_category_id")) != operation.target_category_id
            or feedback.application_status not in {"PENDING", "APPLIED"}
            or feedback.placement_operation_id not in {None, operation.id}
        ):
            raise PlacementServiceError("FEEDBACK_CONTEXT_INVALID", "分类反馈已过期或与落位操作不一致")
        return feedback, suggestion

    def _apply_feedback_confirmation_source(
        self,
        *,
        relation: DocumentCategory,
        feedback: DocumentCategoryFeedback,
        suggestion: DocumentCategorySuggestion,
        actor_user_id: str,
    ) -> bool:
        """为当前用户写入或更新 PRIMARY 确认来源，返回是否发生实际变化。"""

        existing_source = (
            self.db.query(DocumentCategoryConfirmationSource)
            .filter(
                DocumentCategoryConfirmationSource.document_category_id == relation.id,
                DocumentCategoryConfirmationSource.user_id == actor_user_id,
                DocumentCategoryConfirmationSource.status == "ACTIVE",
            )
            .with_for_update()
            .one_or_none()
        )
        if existing_source is None:
            self.db.add(
                DocumentCategoryConfirmationSource(
                    document_category_id=relation.id,
                    user_id=actor_user_id,
                    feedback_id=feedback.id,
                    suggestion_id=suggestion.id,
                    status="ACTIVE",
                )
            )
            self.db.flush()
            return True
        if (
            existing_source.feedback_id == feedback.id
            and existing_source.suggestion_id == suggestion.id
        ):
            return False
        existing_source.feedback_id = feedback.id
        existing_source.suggestion_id = suggestion.id
        return True

    def _enqueue_outbox(self, relation: DocumentCategory, *, source_key: str) -> None:
        latest = int(
            self.db.query(func.max(ClassificationGraphOutbox.state_version))
            .filter(ClassificationGraphOutbox.document_category_id == relation.id)
            .scalar()
            or 0
        )
        state_version = latest + 1
        self.db.add(
            ClassificationGraphOutbox(
                document_category_id=relation.id,
                working_copy_id=relation.working_copy_id,
                document_version_id=relation.document_version_id,
                expected_status=relation.status,
                state_version=state_version,
                deduplication_key=f"{relation.id}:{state_version}:{relation.status}:{source_key}",
                status="PENDING",
            )
        )

    def _persist_failure(
        self,
        operation_id: str,
        code: str,
        message: str,
        *,
        terminal: bool,
        reconciling: bool = False,
    ) -> None:
        """保存真实失败阶段；文件动作可能已发生时保留占用给恢复器。"""

        self.db.rollback()
        operation = self.db.get(ClassificationPlacementOperation, operation_id)
        if operation is None or operation.state == "COMMITTED":
            return
        operation.state = "RECONCILING" if reconciling else ("FAILED" if terminal else "RETRYABLE_FAILED")
        operation.error_json = {"code": code, "message": message[:500]}
        if terminal:
            operation.completed_at = utcnow()
            # 终态失败前尚未确认文件系统副作用时，目标路径不能被永久占用；
            # 已进入 FS_APPLIED/恢复态的操作则必须保留占用给恢复器。
            if operation.state == "FAILED":
                self.db.query(WorkingCopyPathReservation).filter(
                    WorkingCopyPathReservation.placement_operation_id == operation.id
                ).delete(synchronize_session=False)
        working_copy = self.db.get(WorkingCopy, operation.working_copy_id)
        if working_copy is not None:
            working_copy.placement_status = "ERROR" if terminal or reconciling else "PENDING"
        plan = self.db.get(OperationPlan, operation.operation_plan_id)
        if plan is not None:
            plan.status = "FAILED" if terminal else "EXECUTING"
        feedback_context = dict(
            (operation.decision_snapshot_json or {}).get("feedback_context") or {}
        )
        feedback_id = str(feedback_context.get("feedback_id") or "")
        if terminal and feedback_id:
            feedback = self.db.get(DocumentCategoryFeedback, feedback_id)
            if (
                feedback is not None
                and feedback.placement_operation_id == operation.id
                and feedback.application_status == "PENDING"
            ):
                # 文件动作终态失败时保留“已收到但未应用”的真实反馈事实，不能将
                # 尚未生效的目标投影为 PRIMARY。
                feedback.application_status = "FAILED"
        self.db.commit()


def _relation_view(relation: DocumentCategory | None) -> dict[str, Any] | None:
    """生成不包含内部审计 ID 的当前 PRIMARY 投影。"""

    if relation is None:
        return None
    return {
        "category_id": relation.category_id,
        "category_path": list(relation.category_path_json or []),
        "status": relation.status,
    }


def _normalize_feedback_context(value: dict[str, Any]) -> dict[str, str]:
    """只冻结反馈绑定所需的稳定 ID，拒绝把任意请求内容写入操作快照。"""

    required = {
        "feedback_id",
        "suggestion_id",
        "action",
        "relation_role",
        "target_category_id",
    }
    normalized = {key: str(value.get(key) or "").strip() for key in required}
    if not all(normalized.values()):
        raise PlacementServiceError("FEEDBACK_CONTEXT_INVALID", "分类反馈关联不完整")
    if normalized["action"] not in {"ACCEPTED", "CORRECTED"}:
        raise PlacementServiceError("FEEDBACK_CONTEXT_INVALID", "分类反馈动作不支持主类落位")
    if normalized["relation_role"] != "PRIMARY":
        raise PlacementServiceError("FEEDBACK_CONTEXT_INVALID", "分类反馈不是主类决定")
    return normalized
