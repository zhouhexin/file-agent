"""确认后的受管文件重命名执行服务。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AgentRun, ChangeSet, ManagedFile, ManagedRoot, OperationPlan, ToolInvocation, utcnow
from app.modules.changesets.repository import ChangeSetRepository
from app.modules.file_rename.native_executor import NativeRenameError, NativeRenameExecutor
from app.modules.file_rename.schemas import RenameBatchResult, RenameExecutionItem
from app.modules.managed_files.service import sync_configured_managed_roots


class ConfirmedRenameService:
    """执行已确认的 RENAME_FILES OperationPlan。"""

    def __init__(self, db: Session) -> None:
        """保存数据库会话并使用第一版 Native 执行器。"""

        self.db = db
        self.executor = NativeRenameExecutor()

    def execute(self, *, plan: OperationPlan) -> tuple[RenameBatchResult, ChangeSet]:
        """逐文件执行计划，更新索引并写入 ChangeSet。"""

        if not plan.agent_run_id:
            raise ValueError("重命名 OperationPlan 缺少 agent_run_id，不能生成审计记录。")
        run = self.db.get(AgentRun, plan.agent_run_id)
        if run is None:
            raise ValueError("重命名 OperationPlan 关联的 AgentRun 不存在。")
        sync_configured_managed_roots(self.db, scan=False)
        changeset = self._get_or_create_changeset(run=run, plan=plan)
        results: list[RenameExecutionItem] = []
        items = [item for item in plan.plan_json.get("items", []) if isinstance(item, dict)]
        for item in items:
            result = self._execute_one(plan=plan, item=item, changeset=changeset)
            results.append(result)

        completed_count = len([item for item in results if item.status == "COMPLETED"])
        failed_count = len(results) - completed_count
        status = "EXECUTED" if failed_count == 0 else "FAILED" if completed_count == 0 else "PARTIAL"
        changeset.status = "COMPLETED" if status == "EXECUTED" else status
        changeset.summary = f"重命名 {len(results)} 个文件：成功 {completed_count} 个，失败 {failed_count} 个。"
        changeset.updated_at = utcnow()
        plan.status = status
        plan.executed_at = utcnow()
        plan.updated_at = utcnow()
        run.changeset_id = changeset.id
        self._record_tool_invocation(
            run=run,
            plan=plan,
            changeset=changeset,
            status="COMPLETED" if status == "EXECUTED" else status,
            results=results,
        )
        self.db.flush()
        return (
            RenameBatchResult(
                status=status,
                matched_count=len(results),
                completed_count=completed_count,
                failed_count=failed_count,
                items=results,
            ),
            changeset,
        )

    def _execute_one(
        self,
        *,
        plan: OperationPlan,
        item: dict[str, Any],
        changeset: ChangeSet,
    ) -> RenameExecutionItem:
        """在数据库 savepoint 内执行一个文件，并在失败时补偿文件系统。"""

        before = item.get("before") if isinstance(item.get("before"), dict) else {}
        after = item.get("after") if isinstance(item.get("after"), dict) else {}
        managed_file_id = str(before.get("managed_file_id") or "")
        before_relative_path = str(before.get("relative_path") or "")
        after_relative_path = str(after.get("relative_path") or "")
        source_path: Path | None = None
        target_path: Path | None = None
        try:
            with self.db.begin_nested():
                managed_file = self.db.get(ManagedFile, managed_file_id)
                if managed_file is None:
                    raise NativeRenameError("MANAGED_FILE_NOT_FOUND", "受管文件索引不存在。")
                root = self.db.get(ManagedRoot, managed_file.root_id)
                if root is None or not root.enabled:
                    raise NativeRenameError("MANAGED_ROOT_NOT_FOUND", "受管目录不存在或未启用。")
                if root.read_only or "rename" not in set(root.allowed_operations_json or []):
                    raise NativeRenameError("RENAME_NOT_ALLOWED", "该受管目录未启用重命名操作。")
                if managed_file.relative_path != before_relative_path:
                    raise NativeRenameError("OPERATION_PLAN_STALE", "文件路径已经变化，请重新生成重命名计划。")
                source_path, target_path = self.executor.preview(
                    root_path=Path(root.container_path),
                    before_relative_path=before_relative_path,
                    after_relative_path=after_relative_path,
                )
                expected_sha256 = str(before.get("source_sha256") or "")
                if expected_sha256 and _sha256_file(source_path) != expected_sha256:
                    raise NativeRenameError("OPERATION_PLAN_STALE", "文件内容已经变化，请重新生成重命名计划。")
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
                    raise NativeRenameError("TARGET_ALREADY_INDEXED", "目标文件名已存在于受管文件索引。")
                source_path, target_path = self.executor.execute(
                    root_path=Path(root.container_path),
                    before_relative_path=before_relative_path,
                    after_relative_path=after_relative_path,
                )
                stat = target_path.stat()
                managed_file.relative_path = after_relative_path
                managed_file.relative_path_hash = target_hash
                managed_file.filename = target_path.name
                managed_file.extension = target_path.suffix.lower()
                managed_file.size_bytes = stat.st_size
                managed_file.modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                managed_file.fingerprint = _fingerprint(
                    relative_path=after_relative_path,
                    size_bytes=stat.st_size,
                    modified_at=stat.st_mtime,
                )
                if root.classification_mode == "PATH_AS_CATEGORY":
                    parent = Path(after_relative_path).parent.as_posix()
                    managed_file.category_path = None if parent in {"", "."} else parent
                managed_file.updated_at = utcnow()
                item["execution_status"] = "COMPLETED"
                ChangeSetRepository(self.db).create_item(
                    changeset_id=changeset.id,
                    target_type="MANAGED_FILE",
                    target_id=managed_file.id,
                    target_document_id=str(item.get("document_id") or "") or None,
                    change_type="FILENAME_CHANGED",
                    before_value={
                        "root_key": root.root_key,
                        "relative_path": before_relative_path,
                        "filename": str(before.get("filename") or Path(before_relative_path).name),
                        "sha256": expected_sha256,
                    },
                    after_value={
                        "root_key": root.root_key,
                        "relative_path": after_relative_path,
                        "filename": target_path.name,
                        "sha256": expected_sha256,
                    },
                    source="confirmed-file-action",
                    confidence=1,
                    evidence=item.get("rename_metadata") if isinstance(item.get("rename_metadata"), dict) else {},
                    execution_status="COMPLETED",
                )
                self.db.flush()
            return RenameExecutionItem(
                managed_file_id=managed_file_id,
                before_relative_path=before_relative_path,
                after_relative_path=after_relative_path,
                status="COMPLETED",
            )
        except Exception as exc:
            if source_path is not None and target_path is not None:
                try:
                    self.executor.compensate(source_path=source_path, target_path=target_path)
                except OSError:
                    pass
            code = exc.code if isinstance(exc, NativeRenameError) else exc.__class__.__name__
            item["execution_status"] = "FAILED"
            ChangeSetRepository(self.db).create_item(
                changeset_id=changeset.id,
                target_type="MANAGED_FILE",
                target_id=managed_file_id or None,
                target_document_id=str(item.get("document_id") or "") or None,
                change_type="FILE_OPERATION_FAILED",
                before_value={"relative_path": before_relative_path},
                after_value={"relative_path": after_relative_path, "error_code": code},
                source="confirmed-file-action",
                confidence=0,
                evidence={},
                execution_status="FAILED",
            )
            return RenameExecutionItem(
                managed_file_id=managed_file_id,
                before_relative_path=before_relative_path,
                after_relative_path=after_relative_path,
                status="FAILED",
                error_code=code,
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
        status: str,
        results: list[RenameExecutionItem],
    ) -> None:
        """记录确认执行 Tool，避免高风险动作只有普通日志。"""

        self.db.add(
            ToolInvocation(
                agent_run_id=run.id,
                tool_name="confirmed-file-action",
                input_json={"operation_plan_id": plan.id},
                output_json={
                    "operation_plan_id": plan.id,
                    "changeset_id": changeset.id,
                    "items": [item.model_dump(mode="json") for item in results],
                },
                status=status,
                changeset_id=changeset.id,
                operation_plan_id=plan.id,
                finished_at=utcnow(),
            )
        )


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

