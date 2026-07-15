"""确认后的上传附件临时存储重命名执行服务。

执行器只接受后端持久化的 RENAME_UPLOADED_FILES OperationPlan，并把目标固定到当前
Document 的私有临时目录。物理内容被多个 FileObject 共享时使用写时复制，避免修改其他
用户、其他草稿或受管快照引用的文件。
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import log_event
from app.db.models import (
    AgentRun,
    ChangeSet,
    Document,
    FileObject,
    ManagedFileSnapshot,
    OperationPlan,
    ToolInvocation,
    utcnow,
)
from app.modules.changesets.repository import ChangeSetRepository
from app.modules.file_rename.filename_builder import FilenameBuildError, validate_target_filename
from app.modules.file_rename.schemas import UploadedRenameBatchResult, UploadedRenameExecutionItem
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.repository import FileRepository


class UploadedRenameExecutionError(ValueError):
    """上传附件重命名预检或执行失败，并携带稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        """保存结构化错误码和用户可理解的失败原因。"""

        super().__init__(message)
        self.code = code


@dataclass
class _PreparedUploadedRename:
    """完成用户、哈希、文件名和存储路径校验的计划项。"""

    plan_item: dict[str, Any]
    document: Document
    file_object: FileObject
    before_filename: str
    after_filename: str
    source_path: Path
    target_path: Path
    target_storage_path: str
    shared_reference_count: int


class ConfirmedUploadedRenameService:
    """执行已确认的上传附件临时存储重命名计划。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话和受控本地存储根目录。"""

        self.db = db
        self.storage_root = Path(get_settings().file_storage_root).resolve()

    def execute(self, *, plan: OperationPlan) -> tuple[UploadedRenameBatchResult, ChangeSet]:
        """逐文件执行临时重命名，写入 ChangeSet 和 confirmed-file-action 审计。"""

        started_at = time.monotonic()
        if plan.operation_type != "RENAME_UPLOADED_FILES":
            raise ValueError("上传附件执行器只接受 RENAME_UPLOADED_FILES 计划。")
        if not plan.agent_run_id:
            raise ValueError("上传附件重命名计划缺少 agent_run_id。")
        run = self.db.get(AgentRun, plan.agent_run_id)
        if run is None or run.user_id != plan.user_id or run.conversation_id != plan.conversation_id:
            raise ValueError("上传附件重命名计划关联的 AgentRun 无效。")

        changeset = self._get_or_create_changeset(run=run, plan=plan)
        plan_items = [item for item in plan.plan_json.get("items", []) if isinstance(item, dict)]
        if not plan_items:
            raise ValueError("上传附件重命名计划没有可执行项目。")
        log_event(
            "uploaded_file_rename_execution_started",
            agent_run_id=run.id,
            user_id=plan.user_id,
            conversation_id=plan.conversation_id,
            tool_name="confirmed-file-action",
            status="RUNNING",
            operation_plan_id=plan.id,
            item_count=len(plan_items),
        )
        results: list[UploadedRenameExecutionItem] = []
        for plan_item in plan_items:
            try:
                prepared = self._prepare_item(plan=plan, plan_item=plan_item)
                result = self._execute_one(changeset=changeset, prepared=prepared)
            except Exception as exc:
                result = self._failure_from_exception(plan_item=plan_item, exc=exc)
                self._record_failure(changeset=changeset, plan_item=plan_item, result=result)
            results.append(result)

        completed_count = sum(item.status == "COMPLETED" for item in results)
        failed_count = len(results) - completed_count
        status = "EXECUTED" if failed_count == 0 else "FAILED" if completed_count == 0 else "PARTIAL"
        final_result = UploadedRenameBatchResult(
            status=status,
            matched_count=len(results),
            completed_count=completed_count,
            failed_count=failed_count,
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            items=results,
        )
        changeset.status = "COMPLETED" if status == "EXECUTED" else status
        changeset.summary = f"临时存储重命名 {len(results)} 个文件：成功 {completed_count} 个，失败 {failed_count} 个。"
        changeset.updated_at = utcnow()
        plan.status = status
        plan.executed_at = utcnow()
        plan.updated_at = utcnow()
        plan.plan_json = {
            **plan.plan_json,
            "items": plan_items,
            "execution": {"executor": "temporary-storage", "storage_scope": "temporary"},
        }
        run.changeset_id = changeset.id
        self._record_tool_invocation(run=run, plan=plan, changeset=changeset, result=final_result)
        self.db.flush()
        log_event(
            "uploaded_file_rename_execution_completed",
            agent_run_id=run.id,
            user_id=plan.user_id,
            conversation_id=plan.conversation_id,
            tool_name="confirmed-file-action",
            status=status,
            duration_ms=final_result.duration_ms,
            operation_plan_id=plan.id,
            completed_count=completed_count,
            failed_count=failed_count,
        )
        return final_result, changeset

    def _prepare_item(
        self,
        *,
        plan: OperationPlan,
        plan_item: dict[str, Any],
    ) -> _PreparedUploadedRename:
        """锁定 Document，并从数据库事实计算源路径和私有目标路径。"""

        document_id = str(plan_item.get("document_id") or "")
        before = plan_item.get("before") if isinstance(plan_item.get("before"), dict) else {}
        after = plan_item.get("after") if isinstance(plan_item.get("after"), dict) else {}
        before_filename = str(before.get("filename") or "")
        requested_target = str(after.get("filename") or "")
        document = (
            self.db.query(Document)
            .filter(Document.id == document_id, Document.user_id == plan.user_id)
            .with_for_update()
            .one_or_none()
        )
        if document is None:
            raise UploadedRenameExecutionError("DOCUMENT_NOT_FOUND", "上传附件不存在或不属于当前用户。")
        if document.status not in {"UPLOADED", "USED_IN_MESSAGE"}:
            raise UploadedRenameExecutionError("DOCUMENT_STATUS_INVALID", "当前 Document 状态不允许重命名。")
        if self.db.query(ManagedFileSnapshot.id).filter(
            ManagedFileSnapshot.document_id == document.id
        ).first() is not None:
            raise UploadedRenameExecutionError(
                "MANAGED_SNAPSHOT_IMMUTABLE",
                "受管文件快照不能通过上传附件临时重命名执行器修改。",
            )
        if document.original_filename != before_filename:
            raise UploadedRenameExecutionError("OPERATION_PLAN_STALE", "文件名已经变化，请重新生成计划。")
        expected_sha256 = str(before.get("source_sha256") or "")
        if expected_sha256 and document.sha256 != expected_sha256:
            raise UploadedRenameExecutionError("OPERATION_PLAN_STALE", "文件内容标识已经变化，请重新生成计划。")
        try:
            after_filename = validate_target_filename(
                original_filename=document.original_filename,
                target_filename=requested_target,
            )
        except FilenameBuildError as exc:
            raise UploadedRenameExecutionError("INVALID_TARGET_FILENAME", str(exc)) from exc

        resolved = FileExtractionRepository(self.db, plan.user_id).resolve_original_file_for_document(document)
        if not resolved.get("ok"):
            error = resolved.get("error") or {}
            raise UploadedRenameExecutionError(
                str(error.get("code") or "FILE_NOT_FOUND_ON_DISK"),
                str(error.get("message") or "临时文件不存在。"),
            )
        file_object = resolved["file_object"]
        source_path = Path(resolved["file_path"]).resolve()
        if expected_sha256 and _sha256_file(source_path) != expected_sha256:
            raise UploadedRenameExecutionError("OPERATION_PLAN_STALE", "确认前文件内容已经变化。")

        target_relative = Path(document.user_id) / document.id / after_filename
        target_path = (self.storage_root / target_relative).resolve()
        _require_under_root(target_path, self.storage_root)
        if source_path != target_path and target_path.exists():
            raise UploadedRenameExecutionError("TARGET_ALREADY_EXISTS", "目标临时文件名已经存在。")
        reference_count = FileRepository(self.db).count_file_objects_by_storage_path(
            storage_backend=file_object.storage_backend,
            storage_path=file_object.storage_path,
        )
        return _PreparedUploadedRename(
            plan_item=plan_item,
            document=document,
            file_object=file_object,
            before_filename=before_filename,
            after_filename=after_filename,
            source_path=source_path,
            target_path=target_path,
            target_storage_path=target_relative.as_posix(),
            shared_reference_count=reference_count,
        )

    def _execute_one(
        self,
        *,
        changeset: ChangeSet,
        prepared: _PreparedUploadedRename,
    ) -> UploadedRenameExecutionItem:
        """执行单文件写时复制或移动，并在数据库失败时补偿物理动作。"""

        action = "none"
        prepared.target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.db.begin_nested():
                if prepared.source_path != prepared.target_path:
                    if prepared.shared_reference_count > 1:
                        _copy_exclusive(prepared.source_path, prepared.target_path)
                        action = "copied"
                    else:
                        prepared.source_path.rename(prepared.target_path)
                        action = "moved"
                prepared.file_object.storage_path = prepared.target_storage_path
                prepared.document.original_filename = prepared.after_filename
                prepared.document.updated_at = utcnow()
                prepared.plan_item["execution_status"] = "COMPLETED"
                ChangeSetRepository(self.db).create_item(
                    changeset_id=changeset.id,
                    target_type="document",
                    target_id=prepared.document.id,
                    target_document_id=prepared.document.id,
                    change_type="FILENAME_CHANGED",
                    before_value={
                        "storage_scope": "temporary",
                        "filename": prepared.before_filename,
                        "sha256": prepared.document.sha256,
                    },
                    after_value={
                        "storage_scope": "temporary",
                        "filename": prepared.after_filename,
                        "sha256": prepared.document.sha256,
                    },
                    source="confirmed-file-action",
                    confidence=1,
                    evidence=(
                        prepared.plan_item.get("rename_metadata")
                        if isinstance(prepared.plan_item.get("rename_metadata"), dict)
                        else {}
                    ),
                    execution_status="COMPLETED",
                )
                self.db.flush()
        except Exception:
            self._compensate_file_action(prepared=prepared, action=action)
            raise
        return UploadedRenameExecutionItem(
            document_id=prepared.document.id,
            before_filename=prepared.before_filename,
            after_filename=prepared.after_filename,
            status="COMPLETED",
        )

    def _compensate_file_action(self, *, prepared: _PreparedUploadedRename, action: str) -> None:
        """数据库写入失败时恢复或删除本次创建的临时文件，不影响共享源对象。"""

        if action == "copied":
            prepared.target_path.unlink(missing_ok=True)
        elif action == "moved" and prepared.target_path.exists() and not prepared.source_path.exists():
            prepared.source_path.parent.mkdir(parents=True, exist_ok=True)
            prepared.target_path.rename(prepared.source_path)
        _remove_empty_parents(prepared.target_path.parent, stop_at=self.storage_root)

    def _record_failure(
        self,
        *,
        changeset: ChangeSet,
        plan_item: dict[str, Any],
        result: UploadedRenameExecutionItem,
    ) -> None:
        """把单个上传附件失败记录为 ChangeItem，保持批次其他文件可继续。"""

        plan_item["execution_status"] = "FAILED"
        target_document_id = (
            result.document_id if result.document_id and self.db.get(Document, result.document_id) else None
        )
        ChangeSetRepository(self.db).create_item(
            changeset_id=changeset.id,
            target_type="document",
            target_id=result.document_id or None,
            target_document_id=target_document_id,
            change_type="FILE_OPERATION_FAILED",
            before_value={"storage_scope": "temporary", "filename": result.before_filename},
            after_value={
                "storage_scope": "temporary",
                "filename": result.after_filename,
                "error_code": result.error_code,
            },
            source="confirmed-file-action",
            confidence=0,
            evidence={},
            execution_status="FAILED",
        )

    @staticmethod
    def _failure_from_exception(
        *,
        plan_item: dict[str, Any],
        exc: Exception,
    ) -> UploadedRenameExecutionItem:
        """把预检或执行异常转换为不含本地路径的逐文件结果。"""

        before = plan_item.get("before") if isinstance(plan_item.get("before"), dict) else {}
        after = plan_item.get("after") if isinstance(plan_item.get("after"), dict) else {}
        return UploadedRenameExecutionItem(
            document_id=str(plan_item.get("document_id") or ""),
            before_filename=str(before.get("filename") or ""),
            after_filename=str(after.get("filename") or ""),
            status="FAILED",
            error_code=(
                exc.code if isinstance(exc, UploadedRenameExecutionError) else exc.__class__.__name__
            ),
            error_message=str(exc),
        )

    def _get_or_create_changeset(self, *, run: AgentRun, plan: OperationPlan) -> ChangeSet:
        """复用计划生成阶段的解析 ChangeSet，没有时创建文件动作审计容器。"""

        changeset = ChangeSetRepository(self.db).get_by_agent_run(run.id)
        if changeset is not None:
            return changeset
        changeset = ChangeSet(
            workspace_id=plan.workspace_id,
            conversation_id=plan.conversation_id,
            agent_run_id=run.id,
            user_id=plan.user_id,
            status="COMPLETED",
            summary="等待记录确认后的上传附件临时重命名结果。",
        )
        self.db.add(changeset)
        self.db.flush()
        return changeset

    def _record_tool_invocation(
        self,
        *,
        run: AgentRun,
        plan: OperationPlan,
        changeset: ChangeSet,
        result: UploadedRenameBatchResult,
    ) -> None:
        """保存临时存储执行摘要，不记录绝对路径或文件正文。"""

        self.db.add(
            ToolInvocation(
                agent_run_id=run.id,
                tool_name="confirmed-file-action",
                input_json={"operation_plan_id": plan.id, "executor": result.executor},
                output_json={
                    "operation_plan_id": plan.id,
                    "changeset_id": changeset.id,
                    "executor": result.executor,
                    "storage_scope": "temporary",
                    "status": result.status,
                    "matched_count": result.matched_count,
                    "completed_count": result.completed_count,
                    "failed_count": result.failed_count,
                    "items": [item.model_dump(mode="json") for item in result.items],
                },
                status="COMPLETED" if result.status == "EXECUTED" else result.status,
                changeset_id=changeset.id,
                operation_plan_id=plan.id,
                finished_at=utcnow(),
            )
        )


def _copy_exclusive(source: Path, target: Path) -> None:
    """以排他方式复制共享物理对象，避免覆盖并发创建的目标文件。"""

    created = False
    try:
        with source.open("rb") as source_file, target.open("xb") as target_file:
            created = True
            shutil.copyfileobj(source_file, target_file)
        shutil.copystat(source, target)
    except Exception:
        # 只清理由本次排他创建的文件；并发方预先存在的目标绝不能被删除。
        if created:
            target.unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    """流式计算确认时临时文件哈希，检测计划生成后的内容变化。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_under_root(path: Path, root: Path) -> None:
    """确保后端计算的目标路径仍位于本地存储根目录内。"""

    try:
        path.relative_to(root)
    except ValueError as exc:
        raise UploadedRenameExecutionError("UNSAFE_STORAGE_PATH", "临时存储路径越界。") from exc


def _remove_empty_parents(start_dir: Path, *, stop_at: Path) -> None:
    """清理执行或补偿留下的空目录，但绝不越过存储根目录。"""

    current = start_dir.resolve()
    stop = stop_at.resolve()
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent
