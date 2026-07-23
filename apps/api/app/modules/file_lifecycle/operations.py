"""工作副本 OperationPlan 创建与确认执行服务。

重命名和移动只改变工作副本路径并写不可变路径记录；删除只把工作副本移入回收站。
任何操作都不得修改受管原始目录。
"""

from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import get_settings
from app.db.models import (
    ChangeItem,
    Document,
    DocumentVersion,
    FileObject,
    FileRenameReviewItem,
    OperationConfirmation,
    OperationPlan,
    TrashEntry,
    ToolInvocation,
    User,
    WorkingCopy,
    WorkingCopyPathRecord,
    WorkingCopyRoot,
    utcnow,
)
from app.modules.file_lifecycle.repository import FileLifecycleRepository
from app.modules.file_lifecycle.service import create_lifecycle_audit
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.operations.repository import OperationPlanRepository
from app.modules.operations.schemas import OperationPlanCreateRequest
from app.modules.retrieval.search_profile import DocumentSearchProfileService


WORKING_COPY_OPERATION_TYPES = {
    "RENAME_WORKING_COPIES",
    "MOVE_WORKING_COPIES",
    "TRASH_WORKING_COPIES",
    "RESTORE_WORKING_COPIES",
    "RESOLVE_FILENAME_CONFLICT",
}


class WorkingCopyOperationService:
    """创建并执行只作用于工作副本的高风险计划。"""

    def __init__(self, db: Session) -> None:
        """注入请求级数据库会话。"""

        self.db = db
        self.repository = FileLifecycleRepository(db)
        self.plan_repository = OperationPlanRepository(db)
        self.storage = FileLifecycleStorageService()

    def create_plan(self, *, request: OperationPlanCreateRequest, current_user: User) -> OperationPlan:
        """根据数据库当前版本生成不可伪造的工作副本计划。"""

        if request.operation_type not in WORKING_COPY_OPERATION_TYPES:
            raise HTTPException(status_code=400, detail="Unsupported working copy operation")
        if request.operation_type == "RESTORE_WORKING_COPIES":
            raise HTTPException(status_code=400, detail="Restore plans must be created from a trash entry")
        if request.operation_type == "RESOLVE_FILENAME_CONFLICT":
            raise HTTPException(status_code=400, detail="Conflict plans must be created from a pending review")
        if not current_user.default_workspace_id:
            raise HTTPException(status_code=400, detail="Default workspace is required")
        normalized_items: list[dict[str, Any]] = []
        prepared_records: list[WorkingCopyPathRecord] = []
        for requested in request.items:
            if not requested.working_copy_id:
                raise HTTPException(status_code=400, detail="working_copy_id is required")
            working_copy = self.repository.get_owned_working_copy(
                working_copy_id=requested.working_copy_id,
                workspace_id=current_user.default_workspace_id,
            )
            if working_copy is None or working_copy.status != "ACTIVE":
                raise HTTPException(status_code=404, detail="Working copy not found")
            version = self.db.get(DocumentVersion, working_copy.current_version_id) if working_copy.current_version_id else None
            if version is None:
                raise HTTPException(status_code=409, detail="Working copy version is missing")
            after_relative_path = self._resolve_after_path(
                operation_type=request.operation_type,
                working_copy=working_copy,
                requested_after=dict(requested.after or {}),
            )
            item = {
                "document_id": working_copy.document_id,
                "working_copy_id": working_copy.id,
                "managed_file_id": working_copy.managed_file_id,
                "operation": request.operation_type,
                "before": {
                    "relative_path": working_copy.relative_path,
                    "filename": working_copy.filename,
                    "sha256": working_copy.content_sha256,
                    "document_version_id": version.id,
                },
                "after": {
                    "relative_path": after_relative_path,
                    "filename": PurePosixPath(after_relative_path).name if after_relative_path else None,
                },
                "protection": {
                    "managed_original_unchanged": True,
                    "creates_new_version": False,
                    "recoverable": request.operation_type == "TRASH_WORKING_COPIES",
                },
                "rename_metadata": dict(requested.rename_metadata or {}),
                "execution_status": "PLANNED",
            }
            normalized_items.append(item)
        plan = self.plan_repository.create_plan(
            workspace_id=current_user.default_workspace_id,
            conversation_id=request.conversation_id,
            user_id=current_user.id,
            operation_type=request.operation_type,
            risk_level=request.risk_level,
            reason=request.reason,
            plan_json={"items": normalized_items, "target": "WORKING_COPY"},
        )
        for item in normalized_items:
            if request.operation_type not in {"RENAME_WORKING_COPIES", "MOVE_WORKING_COPIES"}:
                continue
            working_copy = self.db.get(WorkingCopy, item["working_copy_id"])
            sequence = int(
                self.db.query(func.max(WorkingCopyPathRecord.sequence_number))
                .filter(WorkingCopyPathRecord.working_copy_id == working_copy.id)
                .scalar()
                or 0
            ) + 1
            record = WorkingCopyPathRecord(
                working_copy_id=working_copy.id,
                sequence_number=sequence,
                operation_type="RENAME" if request.operation_type == "RENAME_WORKING_COPIES" else "MOVE",
                before_relative_path=item["before"]["relative_path"],
                after_relative_path=item["after"]["relative_path"],
                before_filename=item["before"]["filename"],
                after_filename=item["after"]["filename"],
                document_version_id=item["before"]["document_version_id"],
                content_sha256=item["before"]["sha256"],
                operation_plan_id=plan.id,
                status="PLANNED",
            )
            self.db.add(record)
            self.db.flush()
            item["working_copy_path_record_id"] = record.id
            prepared_records.append(record)
        plan.plan_json = {**plan.plan_json, "items": normalized_items}
        # JSON 内部列表已经被原地补入路径记录 ID，必须显式标记才能跨请求持久化。
        flag_modified(plan, "plan_json")
        self.db.flush()
        return plan

    def create_conflict_resolution_plan(
        self,
        *,
        review: FileRenameReviewItem,
        pending_copy: WorkingCopy,
        existing_copy: WorkingCopy,
        decision: str,
        target_relative_path: str,
        conversation_id: str,
        agent_run_id: str,
        current_user: User,
    ) -> OperationPlan:
        """从持久化冲突记录创建计划，禁止 Planner 自报工作副本和目标路径。"""

        if not current_user.default_workspace_id or review.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Filename conflict review not found")
        if review.status != "NEEDS_REVIEW" or review.conversation_id != conversation_id:
            raise HTTPException(status_code=409, detail="Filename conflict review is not pending")
        if pending_copy.workspace_id != current_user.default_workspace_id or existing_copy.workspace_id != current_user.default_workspace_id:
            raise HTTPException(status_code=404, detail="Working copy not found")
        if pending_copy.status != "ACTIVE" or existing_copy.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Filename conflict working copy is not active")

        items: list[dict[str, Any]] = []
        if decision in {"REPLACE_EXISTING_WORKING_COPY", "DELETE_EXISTING_WORKING_COPY"}:
            items.append(self._snapshot_conflict_item(working_copy=existing_copy, operation="TRASH_WORKING_COPIES"))
        if decision == "KEEP_EXISTING":
            items.append(self._snapshot_conflict_item(working_copy=pending_copy, operation="TRASH_WORKING_COPIES"))
        else:
            items.append(
                self._snapshot_conflict_item(
                    working_copy=pending_copy,
                    operation="RENAME_WORKING_COPIES",
                    target_relative_path=target_relative_path,
                )
            )
        plan = self.plan_repository.create_plan(
            workspace_id=current_user.default_workspace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            user_id=current_user.id,
            operation_type="RESOLVE_FILENAME_CONFLICT",
            risk_level="high" if decision in {"REPLACE_EXISTING_WORKING_COPY", "DELETE_EXISTING_WORKING_COPY"} else "medium",
            reason={
                "KEEP_BOTH": "同名文件同时保留，并为新工作副本分配版本后缀",
                "KEEP_EXISTING": "保留已有文件，把新工作副本移入可恢复回收站",
                "REPLACE_EXISTING_WORKING_COPY": "用新工作副本替换已有工作副本，旧副本进入可恢复回收站",
                "DELETE_EXISTING_WORKING_COPY": "删除已有工作副本并保留新文件，删除项进入可恢复回收站",
            }[decision],
            plan_json={
                "target": "WORKING_COPY",
                "conflict_review_id": review.id,
                "conflict_decision": decision,
                "items": items,
            },
        )
        for item in items:
            if item["operation"] != "RENAME_WORKING_COPIES":
                continue
            working_copy = self.db.get(WorkingCopy, item["working_copy_id"])
            sequence = int(
                self.db.query(func.max(WorkingCopyPathRecord.sequence_number))
                .filter(WorkingCopyPathRecord.working_copy_id == working_copy.id)
                .scalar()
                or 0
            ) + 1
            record = WorkingCopyPathRecord(
                working_copy_id=working_copy.id,
                sequence_number=sequence,
                operation_type="RENAME",
                before_relative_path=item["before"]["relative_path"],
                after_relative_path=item["after"]["relative_path"],
                before_filename=item["before"]["filename"],
                after_filename=item["after"]["filename"],
                document_version_id=item["before"]["document_version_id"],
                content_sha256=item["before"]["sha256"],
                operation_plan_id=plan.id,
                agent_run_id=agent_run_id,
                status="PLANNED",
            )
            self.db.add(record)
            self.db.flush()
            item["working_copy_path_record_id"] = record.id
        plan.plan_json = {**plan.plan_json, "items": items}
        flag_modified(plan, "plan_json")
        review.status = "PLANNED"
        review.decision_json = {"action": decision, "operation_plan_id": plan.id}
        review.updated_at = utcnow()
        self.db.flush()
        return plan

    def _snapshot_conflict_item(
        self,
        *,
        working_copy: WorkingCopy,
        operation: str,
        target_relative_path: str | None = None,
    ) -> dict[str, Any]:
        """为冲突计划生成数据库快照，执行时必须再次校验路径、版本和哈希。"""

        version = self.db.get(DocumentVersion, working_copy.current_version_id) if working_copy.current_version_id else None
        if version is None:
            raise HTTPException(status_code=409, detail="Working copy version is missing")
        return {
            "document_id": working_copy.document_id,
            "working_copy_id": working_copy.id,
            "managed_file_id": working_copy.managed_file_id,
            "operation": operation,
            "before": {
                "relative_path": working_copy.relative_path,
                "filename": working_copy.filename,
                "sha256": working_copy.content_sha256,
                "document_version_id": version.id,
            },
            "after": {
                "relative_path": target_relative_path,
                "filename": PurePosixPath(target_relative_path).name if target_relative_path else None,
            },
            "protection": {
                "managed_original_unchanged": True,
                "creates_new_version": False,
                "recoverable": operation == "TRASH_WORKING_COPIES",
            },
            "execution_status": "PLANNED",
        }

    def create_restore_plan(
        self,
        *,
        trash_entry_id: str,
        conversation_id: str,
        current_user: User,
    ) -> OperationPlan:
        """为当前工作区回收站条目创建恢复计划，路径冲突时使用稳定备用路径。"""

        if not current_user.default_workspace_id:
            raise HTTPException(status_code=400, detail="Default workspace is required")
        entry = (
            self.db.query(TrashEntry)
            .filter(
                TrashEntry.id == trash_entry_id,
                TrashEntry.workspace_id == current_user.default_workspace_id,
                TrashEntry.status == "ACTIVE",
            )
            .one_or_none()
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="Trash entry not found")
        working_copy = self.db.get(WorkingCopy, entry.working_copy_id)
        version = self.db.get(DocumentVersion, entry.document_version_id)
        if working_copy is None or version is None or working_copy.status != "TRASHED":
            raise HTTPException(status_code=409, detail="Trash entry cannot be restored")
        target_relative_path = entry.original_relative_path
        root = self.db.get(WorkingCopyRoot, working_copy.working_copy_root_id)
        if root is None:
            raise HTTPException(status_code=409, detail="Working copy root is missing")
        target = self.storage.working_copy_path(f"{root.relative_storage_path}/{target_relative_path}")
        if target.exists():
            target_relative_path = f"restored/{entry.id}/{working_copy.filename}"
        plan = self.plan_repository.create_plan(
            workspace_id=current_user.default_workspace_id,
            conversation_id=conversation_id,
            user_id=current_user.id,
            operation_type="RESTORE_WORKING_COPIES",
            risk_level="medium",
            reason="从回收站恢复工作副本",
            plan_json={
                "target": "WORKING_COPY",
                "items": [
                    {
                        "document_id": working_copy.document_id,
                        "working_copy_id": working_copy.id,
                        "managed_file_id": working_copy.managed_file_id,
                        "operation": "RESTORE_WORKING_COPIES",
                        "before": {
                            "relative_path": entry.trash_relative_path,
                            "filename": working_copy.filename,
                            "sha256": working_copy.content_sha256,
                            "document_version_id": version.id,
                            "trash_entry_id": entry.id,
                        },
                        "after": {
                            "relative_path": target_relative_path,
                            "filename": PurePosixPath(target_relative_path).name,
                        },
                        "protection": {
                            "managed_original_unchanged": True,
                            "creates_new_version": False,
                            "recoverable": True,
                        },
                        "execution_status": "PLANNED",
                    }
                ],
            },
        )
        self.db.flush()
        return plan

    def execute(self, *, plan: OperationPlan, current_user: User) -> tuple[dict[str, Any], str]:
        """执行已确认计划，逐文件隔离失败并生成统一 ChangeSet。"""

        if plan.operation_type not in WORKING_COPY_OPERATION_TYPES:
            raise HTTPException(status_code=409, detail="Unsupported working copy operation")
        confirmation = (
            self.db.query(OperationConfirmation)
            .filter(OperationConfirmation.operation_plan_id == plan.id)
            .order_by(OperationConfirmation.created_at.desc())
            .first()
        )
        results: list[dict[str, Any]] = []
        for item in [value for value in plan.plan_json.get("items", []) if isinstance(value, dict)]:
            try:
                result = self._execute_item(plan=plan, item=item, current_user=current_user, confirmation=confirmation)
            except Exception as exc:
                result = {
                    "working_copy_id": item.get("working_copy_id"),
                    "before_relative_path": (item.get("before") or {}).get("relative_path"),
                    "after_relative_path": (item.get("after") or {}).get("relative_path"),
                    "status": "FAILED",
                    "error_code": exc.__class__.__name__,
                    "error_message": str(exc),
                    "managed_original_unchanged": True,
                    "working_copy_path_record_id": item.get("working_copy_path_record_id"),
                }
                self._mark_path_record_failed(item=item, error=result)
            item["execution_status"] = result["status"]
            results.append(result)
        completed_count = sum(item["status"] == "COMPLETED" for item in results)
        failed_count = len(results) - completed_count
        plan.status = "EXECUTED" if failed_count == 0 else "FAILED" if completed_count == 0 else "PARTIAL"
        plan.executed_at = utcnow()
        plan.updated_at = plan.executed_at
        plan.plan_json = {**plan.plan_json, "items": plan.plan_json.get("items", []), "results": results}
        flag_modified(plan, "plan_json")
        review_id = plan.plan_json.get("conflict_review_id")
        review = self.db.get(FileRenameReviewItem, review_id) if review_id else None
        if review is not None:
            review.status = "EXECUTED" if failed_count == 0 else "NEEDS_REVIEW"
            review.decision_json = {
                **dict(review.decision_json or {}),
                "execution_status": plan.status,
                "results": results,
            }
            review.updated_at = utcnow()
        changeset_id = self._persist_audit(plan=plan, results=results, current_user=current_user)
        return {
            "status": plan.status,
            "matched_count": len(results),
            "completed_count": completed_count,
            "failed_count": failed_count,
            "items": results,
        }, changeset_id

    def _execute_item(
        self,
        *,
        plan: OperationPlan,
        item: dict[str, Any],
        current_user: User,
        confirmation: OperationConfirmation | None,
    ) -> dict[str, Any]:
        """校验计划快照后执行单个工作副本动作。"""

        working_copy = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.id == item.get("working_copy_id"), WorkingCopy.workspace_id == plan.workspace_id)
            .with_for_update()
            .one_or_none()
        )
        operation_type = str(item.get("operation") or plan.operation_type)
        expected_status = "TRASHED" if operation_type == "RESTORE_WORKING_COPIES" else "ACTIVE"
        if working_copy is None or working_copy.status != expected_status:
            raise RuntimeError("工作副本不存在或不处于活动状态")
        before = dict(item.get("before") or {})
        after = dict(item.get("after") or {})
        if (
            (operation_type != "RESTORE_WORKING_COPIES" and working_copy.relative_path != before.get("relative_path"))
            or working_copy.content_sha256 != before.get("sha256")
            or working_copy.current_version_id != before.get("document_version_id")
        ):
            self._mark_path_record_stale(item=item)
            raise RuntimeError("OperationPlan 已过期，请重新生成")
        version = self.db.get(DocumentVersion, working_copy.current_version_id)
        root = self.db.get(WorkingCopyRoot, working_copy.working_copy_root_id)
        if version is None or root is None:
            raise RuntimeError("工作副本关系不完整")
        before_storage_path = version.storage_path
        source = (
            self.storage.trash_path(before_storage_path)
            if operation_type == "RESTORE_WORKING_COPIES"
            else self.storage.working_copy_path(before_storage_path)
        )
        if self.storage.sha256_file(source) != working_copy.content_sha256:
            self._mark_path_record_stale(item=item)
            raise RuntimeError("工作副本内容已经变化，请重新生成计划")
        if operation_type == "TRASH_WORKING_COPIES":
            result = self._trash(
                plan=plan,
                working_copy=working_copy,
                version=version,
                root=root,
                source=source,
                current_user=current_user,
            )
            return {**result, "operation_type": operation_type}
        if operation_type == "RESTORE_WORKING_COPIES":
            result = self._restore(
                plan=plan,
                working_copy=working_copy,
                version=version,
                root=root,
                source=source,
                item=item,
            )
            return {**result, "operation_type": operation_type}
        after_relative_path = str(after.get("relative_path") or "")
        after_storage_path = f"{root.relative_storage_path}/{after_relative_path}"
        target = self.storage.working_copy_path(after_storage_path)
        if target.exists():
            raise FileExistsError("目标工作副本路径已存在")
        target.parent.mkdir(parents=True, exist_ok=True)
        record = self.db.get(WorkingCopyPathRecord, item.get("working_copy_path_record_id"))
        if record is None or record.status != "PLANNED":
            raise RuntimeError("工作副本路径记录不存在或状态异常")
        record.status = "RUNNING"
        record.operation_confirmation_id = confirmation.id if confirmation else None
        record.executed_by = current_user.id
        self.db.flush()
        os.replace(source, target)
        operation_time = utcnow()
        working_copy.relative_path = after_relative_path
        working_copy.relative_path_hash = hashlib.sha256(after_relative_path.encode("utf-8")).hexdigest()
        working_copy.filename = PurePosixPath(after_relative_path).name
        working_copy.updated_at = operation_time
        working_copy.last_operation_plan_id = plan.id
        version.storage_path = after_storage_path
        version.filename = working_copy.filename
        document = self.db.get(Document, working_copy.document_id)
        if document is not None:
            # Document 名称是工作副本当前展示名；内容版本和原始文件均保持不变。
            document.original_filename = working_copy.filename
        file_object = self.db.query(FileObject).filter(FileObject.document_id == working_copy.document_id).first()
        if file_object is not None:
            file_object.storage_backend = "working_copy_local"
            file_object.storage_path = after_storage_path
        # 已确认的改名/移动必须在同一事务刷新投影，避免新旧文件名同时或都不能被检索。
        DocumentSearchProfileService(db=self.db).upsert_current_profile(working_copy.id)
        record.status = "COMPLETED"
        record.updated_at = operation_time
        return {
            "working_copy_id": working_copy.id,
            "before_relative_path": before["relative_path"],
            "after_relative_path": after_relative_path,
            "document_version_id": version.id,
            "working_copy_path_record_id": record.id,
            "path_record_updated_at": operation_time.isoformat(),
            "status": "COMPLETED",
            "managed_original_unchanged": True,
            "operation_type": operation_type,
        }

    def _trash(
        self,
        *,
        plan: OperationPlan,
        working_copy: WorkingCopy,
        version: DocumentVersion,
        root: WorkingCopyRoot,
        source: Path,
        current_user: User,
    ) -> dict[str, Any]:
        """把工作副本移入回收站并保留原始文件和消息引用。"""

        now = utcnow()
        trash_relative_path = f"{working_copy.workspace_id}/{working_copy.id}/{int(now.timestamp())}/{working_copy.filename}"
        target = self.storage.trash_path(trash_relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError("回收站目标路径冲突")
        os.replace(source, target)
        entry = TrashEntry(
            workspace_id=working_copy.workspace_id,
            working_copy_id=working_copy.id,
            document_version_id=version.id,
            entry_type="DELETED",
            original_relative_path=working_copy.relative_path,
            trash_relative_path=trash_relative_path,
            status="ACTIVE",
            operation_plan_id=plan.id,
            deleted_by=current_user.id,
            deleted_at=now,
            retention_until=now + timedelta(days=get_settings().trash_retention_days),
        )
        self.db.add(entry)
        self.db.flush()
        working_copy.status = "TRASHED"
        working_copy.last_operation_plan_id = plan.id
        working_copy.updated_at = now
        version.storage_tier = "TRASH"
        version.storage_path = trash_relative_path
        file_object = self.db.query(FileObject).filter(FileObject.document_id == working_copy.document_id).first()
        if file_object is not None:
            file_object.storage_backend = "trash_local"
            file_object.storage_path = trash_relative_path
        # 回收站文件必须立即退出可搜索范围；不等待后台 reconciliation。
        DocumentSearchProfileService(db=self.db).deactivate_profile(working_copy.id)
        return {
            "working_copy_id": working_copy.id,
            "before_relative_path": working_copy.relative_path,
            "after_relative_path": None,
            "document_version_id": version.id,
            "trash_entry_id": entry.id,
            "retention_until": entry.retention_until.isoformat(),
            "status": "COMPLETED",
            "managed_original_unchanged": True,
            "recoverable": True,
        }

    def _persist_audit(self, *, plan: OperationPlan, results: list[dict[str, Any]], current_user: User) -> str:
        """为计划创建一份 ChangeSet，并为每个工作副本写独立 ChangeItem。"""

        first = results[0] if results else {}
        change_types = {
            "RENAME_WORKING_COPIES": "FILENAME_CHANGED",
            "MOVE_WORKING_COPIES": "FILE_MOVED",
            "TRASH_WORKING_COPIES": "FILE_TRASHED",
            "RESTORE_WORKING_COPIES": "FILE_RESTORED",
        }
        first_change_type = change_types.get(str(first.get("operation_type") or plan.operation_type), "FILE_OPERATION_COMPLETED")
        changeset, _message = create_lifecycle_audit(
            db=self.db,
            user_id=current_user.id,
            workspace_id=plan.workspace_id,
            conversation_id=plan.conversation_id,
            tool_name="confirmed-file-action",
            message_content=f"工作副本操作完成：{plan.operation_type}",
            change_type=first_change_type if first.get("status") == "COMPLETED" else "FILE_OPERATION_FAILED",
            target_type="working_copy",
            target_id=first.get("working_copy_id"),
            target_document_id=(
                self.db.get(WorkingCopy, first.get("working_copy_id")).document_id
                if first.get("working_copy_id") and self.db.get(WorkingCopy, first.get("working_copy_id"))
                else None
            ),
            before_value={"relative_path": first.get("before_relative_path")},
            after_value=first,
            execution_status=first.get("status", "FAILED"),
        )
        plan.agent_run_id = changeset.agent_run_id
        for result in results[1:]:
            result_change_type = change_types.get(str(result.get("operation_type") or plan.operation_type), "FILE_OPERATION_COMPLETED")
            self.db.add(
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="working_copy",
                    target_id=result.get("working_copy_id"),
                    target_document_id=(
                        self.db.get(WorkingCopy, result.get("working_copy_id")).document_id
                        if result.get("working_copy_id") and self.db.get(WorkingCopy, result.get("working_copy_id"))
                        else None
                    ),
                    change_type=result_change_type if result.get("status") == "COMPLETED" else "FILE_OPERATION_FAILED",
                    before_value_json={"relative_path": result.get("before_relative_path")},
                    after_value_json=result,
                    source="confirmed-file-action",
                    confidence=1.0,
                    evidence_json={},
                    execution_status=result.get("status", "FAILED"),
                )
            )
        self.db.flush()
        change_items = self.db.query(ChangeItem).filter(ChangeItem.changeset_id == changeset.id).all()
        item_by_target = {item.target_id: item for item in change_items}
        invocation = (
            self.db.query(ToolInvocation)
            .filter(ToolInvocation.agent_run_id == changeset.agent_run_id)
            .order_by(ToolInvocation.created_at.desc())
            .first()
        )
        for result in results:
            record_id = result.get("working_copy_path_record_id")
            record = self.db.get(WorkingCopyPathRecord, record_id) if record_id else None
            if record is not None:
                record.changeset_id = changeset.id
                change_item = item_by_target.get(record.working_copy_id)
                record.change_item_id = change_item.id if change_item else None
                record.agent_run_id = changeset.agent_run_id
                record.tool_invocation_id = invocation.id if invocation else None
        return changeset.id

    def _restore(
        self,
        *,
        plan: OperationPlan,
        working_copy: WorkingCopy,
        version: DocumentVersion,
        root: WorkingCopyRoot,
        source: Path,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        """把回收站内容恢复为活动工作副本，原始文件保持不变。"""

        before = dict(item.get("before") or {})
        after = dict(item.get("after") or {})
        entry = self.db.get(TrashEntry, before.get("trash_entry_id"))
        if entry is None or entry.status != "ACTIVE" or entry.working_copy_id != working_copy.id:
            raise RuntimeError("回收站条目已失效")
        after_relative_path = str(after.get("relative_path") or "")
        after_storage_path = f"{root.relative_storage_path}/{after_relative_path}"
        target = self.storage.working_copy_path(after_storage_path)
        if target.exists():
            raise FileExistsError("恢复目标路径已被占用")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        now = utcnow()
        working_copy.status = "ACTIVE"
        working_copy.relative_path = after_relative_path
        working_copy.relative_path_hash = hashlib.sha256(after_relative_path.encode("utf-8")).hexdigest()
        working_copy.filename = PurePosixPath(after_relative_path).name
        working_copy.last_operation_plan_id = plan.id
        working_copy.updated_at = now
        version.storage_tier = "WORKING_COPY"
        version.storage_path = after_storage_path
        version.filename = working_copy.filename
        entry.status = "RESTORED"
        entry.restored_at = now
        entry.updated_at = now
        file_object = self.db.query(FileObject).filter(FileObject.document_id == working_copy.document_id).first()
        if file_object is not None:
            file_object.storage_backend = "working_copy_local"
            file_object.storage_path = after_storage_path
        # 恢复为 ACTIVE 时重新生成当前版本投影，防止沿用回收前的陈旧名称或摘要。
        DocumentSearchProfileService(db=self.db).upsert_current_profile(working_copy.id)
        return {
            "working_copy_id": working_copy.id,
            "before_relative_path": before.get("relative_path"),
            "after_relative_path": after_relative_path,
            "document_version_id": version.id,
            "trash_entry_id": entry.id,
            "status": "COMPLETED",
            "managed_original_unchanged": True,
        }

    def _resolve_after_path(
        self,
        *,
        operation_type: str,
        working_copy: WorkingCopy,
        requested_after: dict[str, Any],
    ) -> str | None:
        """把用户请求收敛为工作副本根内的安全相对路径。"""

        if operation_type in {"TRASH_WORKING_COPIES", "RESTORE_WORKING_COPIES"}:
            return None
        if operation_type == "RENAME_WORKING_COPIES":
            filename = Path(str(requested_after.get("filename") or "")).name
            if not filename or filename in {".", ".."}:
                raise HTTPException(status_code=400, detail="Rename target filename is required")
            parent = PurePosixPath(working_copy.relative_path).parent
            value = (parent / filename).as_posix()
        else:
            value = str(requested_after.get("relative_path") or "").replace("\\", "/").strip("/")
        normalized = PurePosixPath(value)
        if not value or normalized.is_absolute() or ".." in normalized.parts:
            raise HTTPException(status_code=400, detail="Working copy target path is invalid")
        return normalized.as_posix()

    def _mark_path_record_failed(self, *, item: dict[str, Any], error: dict[str, Any]) -> None:
        """路径操作失败时持久化 FAILED，而不是只写日志。"""

        record_id = item.get("working_copy_path_record_id")
        record = self.db.get(WorkingCopyPathRecord, record_id) if record_id else None
        if record is not None and record.status not in {"COMPLETED", "STALE"}:
            record.status = "FAILED"
            record.error_code = str(error.get("error_code") or "FILE_OPERATION_FAILED")
            record.error_message = str(error.get("error_message") or "工作副本路径操作失败")
            record.updated_at = utcnow()

    def _mark_path_record_stale(self, *, item: dict[str, Any]) -> None:
        """计划快照失效时把路径记录标记为 STALE。"""

        record_id = item.get("working_copy_path_record_id")
        record = self.db.get(WorkingCopyPathRecord, record_id) if record_id else None
        if record is not None and record.status == "PLANNED":
            record.status = "STALE"
            record.error_code = "OPERATION_PLAN_STALE"
            record.error_message = "工作副本路径、版本或内容已经变化"
            record.updated_at = utcnow()
