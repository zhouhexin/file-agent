"""确认后的受管文件批量重命名执行服务。"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.db.models import AgentRun, ChangeSet, ManagedFile, ManagedRoot, OperationPlan, ToolInvocation, utcnow
from app.modules.changesets.repository import ChangeSetRepository
from app.modules.file_rename.batch_validator import validate_rename_mapping
from app.modules.file_rename.executor_factory import create_rename_executor
from app.modules.file_rename.executor_protocol import RenameExecutor, RenameExecutorError
from app.modules.file_rename.schemas import (
    RenameBatchItem,
    RenameBatchRequest,
    RenameBatchResult,
    RenameExecutionItem,
)
from app.modules.managed_files.service import sync_configured_managed_roots


ExecutorFactory = Callable[[Settings], RenameExecutor]


@dataclass
class _PreparedRename:
    """完成数据库和文件系统预检的计划项。"""

    plan_item: dict[str, Any]
    managed_file: ManagedFile
    root: ManagedRoot
    batch_item: RenameBatchItem


class ConfirmedRenameService:
    """执行已确认的 RENAME_FILES OperationPlan。"""

    def __init__(
        self,
        db: Session,
        *,
        settings: Settings | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        """保存请求级数据库会话、配置和执行器工厂。"""

        self.db = db
        self.settings = settings or get_settings()
        self.executor_factory = executor_factory or create_rename_executor

    def execute(self, *, plan: OperationPlan) -> tuple[RenameBatchResult, ChangeSet]:
        """锁定并校验计划项，按受管根目录批量执行后写入审计。"""

        started_at = time.monotonic()
        if not plan.agent_run_id:
            raise ValueError("重命名 OperationPlan 缺少 agent_run_id，不能生成审计记录。")
        run = self.db.get(AgentRun, plan.agent_run_id)
        if run is None:
            raise ValueError("重命名 OperationPlan 关联的 AgentRun 不存在。")
        sync_configured_managed_roots(self.db, scan=False)
        changeset = self._get_or_create_changeset(run=run, plan=plan)
        plan_items = [item for item in plan.plan_json.get("items", []) if isinstance(item, dict)]
        log_event(
            "file_rename_execution_started",
            agent_run_id=run.id,
            user_id=plan.user_id,
            conversation_id=plan.conversation_id,
            tool_name="confirmed-file-action",
            status="RUNNING",
            operation_plan_id=plan.id,
            item_count=len(plan_items),
            executor=self.settings.file_rename_executor.lower(),
        )
        results: list[RenameExecutionItem] = []
        prepared_by_root: dict[str, list[_PreparedRename]] = defaultdict(list)

        for plan_item in plan_items:
            try:
                prepared = self._prepare_item(plan_item=plan_item)
                prepared_by_root[prepared.root.id].append(prepared)
            except Exception as exc:
                result = self._failure_from_exception(plan_item=plan_item, exc=exc)
                results.append(result)
                self._record_failure(changeset=changeset, plan_item=plan_item, result=result)

        executor: RenameExecutor | None = None
        executor_error: RenameExecutorError | None = None
        try:
            executor = self.executor_factory(self.settings)
        except RenameExecutorError as exc:
            executor_error = exc

        preview_digests: list[str] = []
        executor_version = ""
        for prepared_items in prepared_by_root.values():
            for batch in _chunks(prepared_items, self.settings.file_rename_max_batch_size):
                if executor is None:
                    assert executor_error is not None
                    batch_results = [
                        self._failure_from_exception(plan_item=item.plan_item, exc=executor_error)
                        for item in batch
                    ]
                    for prepared, result in zip(batch, batch_results, strict=True):
                        self._record_failure(changeset=changeset, plan_item=prepared.plan_item, result=result)
                    results.extend(batch_results)
                    continue
                batch_result = self._execute_batch(
                    plan=plan,
                    changeset=changeset,
                    executor=executor,
                    prepared_items=batch,
                )
                results.extend(batch_result.items)
                if batch_result.preview_digest:
                    preview_digests.append(batch_result.preview_digest)
                executor_version = batch_result.executor_version or executor_version

        completed_count = sum(item.status == "COMPLETED" for item in results)
        failed_count = len(results) - completed_count
        status = "EXECUTED" if failed_count == 0 else "FAILED" if completed_count == 0 else "PARTIAL"
        executor_name = executor.name if executor is not None else self.settings.file_rename_executor.lower()
        combined_digest = _combine_preview_digests(preview_digests)
        final_result = RenameBatchResult(
            executor=executor_name,
            executor_version=executor_version,
            preview_digest=combined_digest,
            status=status,
            matched_count=len(results),
            completed_count=completed_count,
            failed_count=failed_count,
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            items=results,
        )
        changeset.status = "COMPLETED" if status == "EXECUTED" else status
        changeset.summary = f"重命名 {len(results)} 个文件：成功 {completed_count} 个，失败 {failed_count} 个。"
        changeset.updated_at = utcnow()
        plan.status = status
        plan.executed_at = utcnow()
        plan.updated_at = utcnow()
        plan.plan_json = {
            **plan.plan_json,
            "items": plan_items,
            "execution": {
                "executor": executor_name,
                "executor_version": executor_version,
                "preview_digest": combined_digest,
            },
        }
        run.changeset_id = changeset.id
        self._record_tool_invocation(
            run=run,
            plan=plan,
            changeset=changeset,
            result=final_result,
        )
        self.db.flush()
        log_event(
            "file_rename_execution_completed",
            agent_run_id=run.id,
            user_id=plan.user_id,
            conversation_id=plan.conversation_id,
            tool_name="confirmed-file-action",
            status=status,
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            operation_plan_id=plan.id,
            executor=executor_name,
            executor_version=executor_version,
            completed_count=completed_count,
            failed_count=failed_count,
        )
        return final_result, changeset

    def _prepare_item(self, *, plan_item: dict[str, Any]) -> _PreparedRename:
        """锁定受管文件并校验路径、权限、哈希和索引冲突。"""

        before = plan_item.get("before") if isinstance(plan_item.get("before"), dict) else {}
        after = plan_item.get("after") if isinstance(plan_item.get("after"), dict) else {}
        managed_file_id = str(before.get("managed_file_id") or "")
        before_relative_path = str(before.get("relative_path") or "")
        after_relative_path = str(after.get("relative_path") or "")
        managed_file = (
            self.db.query(ManagedFile)
            .filter(ManagedFile.id == managed_file_id)
            .with_for_update()
            .one_or_none()
        )
        if managed_file is None:
            raise RenameExecutorError("MANAGED_FILE_NOT_FOUND", "受管文件索引不存在。")
        root = self.db.get(ManagedRoot, managed_file.root_id)
        if root is None or not root.enabled:
            raise RenameExecutorError("MANAGED_ROOT_NOT_FOUND", "受管目录不存在或未启用。")
        if root.read_only or "rename" not in set(root.allowed_operations_json or []):
            raise RenameExecutorError("RENAME_NOT_ALLOWED", "该受管目录未启用重命名操作。")
        if managed_file.relative_path != before_relative_path:
            raise RenameExecutorError("OPERATION_PLAN_STALE", "文件路径已经变化，请重新生成重命名计划。")
        paths = validate_rename_mapping(
            root_path=Path(root.container_path),
            before_relative_path=before_relative_path,
            after_relative_path=after_relative_path,
        )
        expected_sha256 = str(before.get("source_sha256") or "")
        if expected_sha256 and _sha256_file(paths.source_path) != expected_sha256:
            raise RenameExecutorError("OPERATION_PLAN_STALE", "文件内容已经变化，请重新生成重命名计划。")
        target_hash = _path_hash(after_relative_path)
        duplicate = (
            self.db.query(ManagedFile.id)
            .filter(
                ManagedFile.root_id == root.id,
                ManagedFile.relative_path_hash == target_hash,
                ManagedFile.id != managed_file.id,
            )
            .first()
        )
        if duplicate is not None:
            raise RenameExecutorError("TARGET_ALREADY_INDEXED", "目标文件名已存在于受管文件索引。")
        return _PreparedRename(
            plan_item=plan_item,
            managed_file=managed_file,
            root=root,
            batch_item=RenameBatchItem(
                managed_file_id=managed_file.id,
                before_relative_path=before_relative_path,
                after_relative_path=after_relative_path,
                source_sha256=expected_sha256,
            ),
        )

    def _execute_batch(
        self,
        *,
        plan: OperationPlan,
        changeset: ChangeSet,
        executor: RenameExecutor,
        prepared_items: list[_PreparedRename],
    ) -> RenameBatchResult:
        """预演并执行一个同根目录批次，数据库失败时逆序补偿。"""

        request = RenameBatchRequest(
            root_path=Path(prepared_items[0].root.container_path),
            operation_plan_id=plan.id,
            items=[item.batch_item for item in prepared_items],
            timeout_seconds=self.settings.file_rename_execution_timeout_seconds,
        )
        preview = executor.preview_batch(request)
        if preview.status != "PREVIEWED":
            return self._record_executor_failures(
                changeset=changeset,
                prepared_items=prepared_items,
                executor_result=preview,
            )

        execution_result: RenameBatchResult | None = None
        try:
            with self.db.begin_nested():
                execution_result = executor.execute_batch(request)
                if execution_result.preview_digest != preview.preview_digest:
                    raise RenameExecutorError("PREVIEW_DIGEST_MISMATCH", "执行前后的预演摘要不一致。")
                result_by_id = {item.managed_file_id: item for item in execution_result.items}
                normalized_results: list[RenameExecutionItem] = []
                for prepared in prepared_items:
                    result = result_by_id.get(prepared.managed_file.id)
                    if result is None:
                        result = RenameExecutionItem(
                            managed_file_id=prepared.managed_file.id,
                            before_relative_path=prepared.batch_item.before_relative_path,
                            after_relative_path=prepared.batch_item.after_relative_path,
                            status="FAILED",
                            error_code="EXECUTOR_RESULT_INCOMPLETE",
                            error_message="执行器未返回该文件的结果。",
                        )
                    normalized_results.append(result)
                    if result.status == "COMPLETED":
                        self._apply_success(changeset=changeset, prepared=prepared)
                    else:
                        self._record_failure(
                            changeset=changeset,
                            plan_item=prepared.plan_item,
                            result=result,
                        )
                self.db.flush()
            normalized_completed = sum(item.status == "COMPLETED" for item in normalized_results)
            normalized_failed = len(normalized_results) - normalized_completed
            normalized_status = (
                "EXECUTED"
                if normalized_failed == 0
                else "FAILED"
                if normalized_completed == 0
                else "PARTIAL"
            )
            return execution_result.model_copy(
                update={
                    "status": normalized_status,
                    "matched_count": len(normalized_results),
                    "completed_count": normalized_completed,
                    "failed_count": normalized_failed,
                    "items": normalized_results,
                }
            )
        except Exception as exc:
            if execution_result is not None and any(
                item.status == "COMPLETED" for item in execution_result.items
            ):
                compensation = executor.compensate_batch(request, execution_result)
                if compensation.status != "COMPENSATED":
                    exc = RenameExecutorError(
                        "F2_COMPENSATION_FAILED" if executor.name == "f2" else "NATIVE_COMPENSATION_FAILED",
                        "数据库更新失败后无法完整恢复原文件名。",
                    )
            failed_items = [
                self._failure_from_exception(plan_item=item.plan_item, exc=exc)
                for item in prepared_items
            ]
            for prepared, result in zip(prepared_items, failed_items, strict=True):
                self._record_failure(changeset=changeset, plan_item=prepared.plan_item, result=result)
            return RenameBatchResult(
                executor=executor.name,
                executor_version=preview.executor_version,
                preview_digest=preview.preview_digest,
                status="FAILED",
                matched_count=len(failed_items),
                completed_count=0,
                failed_count=len(failed_items),
                items=failed_items,
            )

    def _record_executor_failures(
        self,
        *,
        changeset: ChangeSet,
        prepared_items: list[_PreparedRename],
        executor_result: RenameBatchResult,
    ) -> RenameBatchResult:
        """把执行器批次失败逐项写入 ChangeSet。"""

        by_id = {item.managed_file_id: item for item in executor_result.items}
        normalized: list[RenameExecutionItem] = []
        for prepared in prepared_items:
            result = by_id.get(prepared.managed_file.id) or RenameExecutionItem(
                managed_file_id=prepared.managed_file.id,
                before_relative_path=prepared.batch_item.before_relative_path,
                after_relative_path=prepared.batch_item.after_relative_path,
                status="FAILED",
                error_code="EXECUTOR_RESULT_INCOMPLETE",
                error_message="执行器未返回该文件的结果。",
            )
            normalized.append(result)
            self._record_failure(changeset=changeset, plan_item=prepared.plan_item, result=result)
        return executor_result.model_copy(
            update={
                "status": "FAILED",
                "matched_count": len(normalized),
                "completed_count": 0,
                "failed_count": len(normalized),
                "items": normalized,
            }
        )

    def _apply_success(self, *, changeset: ChangeSet, prepared: _PreparedRename) -> None:
        """更新受管文件索引并写入成功 ChangeItem。"""

        target_path = Path(prepared.root.container_path) / prepared.batch_item.after_relative_path
        stat = target_path.stat()
        managed_file = prepared.managed_file
        managed_file.relative_path = prepared.batch_item.after_relative_path
        managed_file.relative_path_hash = _path_hash(prepared.batch_item.after_relative_path)
        managed_file.filename = target_path.name
        managed_file.extension = target_path.suffix.lower()
        managed_file.size_bytes = stat.st_size
        managed_file.modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        managed_file.fingerprint = _fingerprint(
            relative_path=prepared.batch_item.after_relative_path,
            size_bytes=stat.st_size,
            modified_at=stat.st_mtime,
        )
        if prepared.root.classification_mode == "PATH_AS_CATEGORY":
            parent = Path(prepared.batch_item.after_relative_path).parent.as_posix()
            managed_file.category_path = None if parent in {"", "."} else parent
        managed_file.updated_at = utcnow()
        prepared.plan_item["execution_status"] = "COMPLETED"
        before = prepared.plan_item.get("before") if isinstance(prepared.plan_item.get("before"), dict) else {}
        ChangeSetRepository(self.db).create_item(
            changeset_id=changeset.id,
            target_type="MANAGED_FILE",
            target_id=managed_file.id,
            target_document_id=str(prepared.plan_item.get("document_id") or "") or None,
            change_type="FILENAME_CHANGED",
            before_value={
                "root_key": prepared.root.root_key,
                "relative_path": prepared.batch_item.before_relative_path,
                "filename": str(before.get("filename") or Path(prepared.batch_item.before_relative_path).name),
                "sha256": prepared.batch_item.source_sha256,
            },
            after_value={
                "root_key": prepared.root.root_key,
                "relative_path": prepared.batch_item.after_relative_path,
                "filename": target_path.name,
                "sha256": prepared.batch_item.source_sha256,
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

    def _record_failure(
        self,
        *,
        changeset: ChangeSet,
        plan_item: dict[str, Any],
        result: RenameExecutionItem,
    ) -> None:
        """记录单个文件的结构化失败，保持批次其余项目可继续。"""

        plan_item["execution_status"] = "FAILED"
        ChangeSetRepository(self.db).create_item(
            changeset_id=changeset.id,
            target_type="MANAGED_FILE",
            target_id=result.managed_file_id or None,
            target_document_id=str(plan_item.get("document_id") or "") or None,
            change_type="FILE_OPERATION_FAILED",
            before_value={"relative_path": result.before_relative_path},
            after_value={
                "relative_path": result.after_relative_path,
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
    ) -> RenameExecutionItem:
        """把预检或数据库异常转换为文件级结果。"""

        before = plan_item.get("before") if isinstance(plan_item.get("before"), dict) else {}
        after = plan_item.get("after") if isinstance(plan_item.get("after"), dict) else {}
        return RenameExecutionItem(
            managed_file_id=str(before.get("managed_file_id") or ""),
            before_relative_path=str(before.get("relative_path") or ""),
            after_relative_path=str(after.get("relative_path") or ""),
            status="FAILED",
            error_code=exc.code if isinstance(exc, RenameExecutorError) else exc.__class__.__name__,
            error_message=str(exc),
        )

    def _get_or_create_changeset(self, *, run: AgentRun, plan: OperationPlan) -> ChangeSet:
        """复用计划生成 AgentRun 的 ChangeSet，没有时创建审计容器。"""

        changeset = ChangeSetRepository(self.db).get_by_agent_run(run.id)
        if changeset is not None:
            return changeset
        changeset = ChangeSet(
            workspace_id=plan.workspace_id,
            conversation_id=plan.conversation_id,
            agent_run_id=run.id,
            user_id=plan.user_id,
            status="COMPLETED",
            summary="等待记录确认后的文件重命名结果。",
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
        result: RenameBatchResult,
    ) -> None:
        """保存执行器摘要和相对路径结果，不保存 CSV 或绝对路径。"""

        self.db.add(
            ToolInvocation(
                agent_run_id=run.id,
                tool_name="confirmed-file-action",
                input_json={"operation_plan_id": plan.id, "executor": result.executor},
                output_json={
                    "operation_plan_id": plan.id,
                    "changeset_id": changeset.id,
                    "executor": result.executor,
                    "executor_version": result.executor_version,
                    "preview_digest": result.preview_digest,
                    "items": [item.model_dump(mode="json") for item in result.items],
                },
                status="COMPLETED" if result.status == "EXECUTED" else result.status,
                changeset_id=changeset.id,
                operation_plan_id=plan.id,
                finished_at=utcnow(),
            )
        )


def _chunks(items: list[_PreparedRename], size: int) -> list[list[_PreparedRename]]:
    """按配置上限拆分执行批次。"""

    effective_size = max(1, size)
    return [items[index : index + effective_size] for index in range(0, len(items), effective_size)]


def _combine_preview_digests(digests: list[str]) -> str:
    """把多个根目录或分块摘要收敛为计划级摘要。"""

    if not digests:
        return ""
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    """流式计算确认时源文件内容哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_hash(relative_path: str) -> str:
    """生成受管文件相对路径唯一哈希。"""

    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()


def _fingerprint(*, relative_path: str, size_bytes: int, modified_at: float) -> str:
    """生成与扫描器一致的轻量指纹。"""

    payload = f"{relative_path}\0{size_bytes}\0{int(modified_at)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
