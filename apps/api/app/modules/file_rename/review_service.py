"""重命名待复核项持久化、用户更正解析和即时确认执行。"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    FileRenameBatch,
    FileRenameBatchItem,
    FileRenameReviewItem,
    ManagedFile,
    User,
    utcnow,
)
from app.modules.file_rename.batch_service import RenameBatchService
from app.modules.file_rename.execution_service import ConfirmedRenameService
from app.modules.file_rename.schemas import RenameSuggestion
from app.modules.operations.repository import OperationPlanRepository


_CORRECTION_PATTERN = re.compile(r"^\s*文件\s*(?P<source>.+?)\s*更正为\s*(?P<target>.+?)\s*[。；;]?\s*$")
_DISMISS_MESSAGES = {"不需要", "不需要改名", "无需改名", "不用改名"}


class RenameReviewService:
    """管理同一用户、同一会话内尚未处理的重命名待复核项。"""

    def __init__(self, db: Session, user_id: str) -> None:
        """保存请求级数据库会话和用户边界。"""

        self.db = db
        self.user_id = user_id
        self.repository = OperationPlanRepository(db)

    def persist_suggestion(
        self,
        *,
        conversation_id: str,
        agent_run_id: str,
        suggestion: RenameSuggestion,
        rename_batch_id: str | None = None,
        rename_batch_item_id: str | None = None,
    ) -> FileRenameReviewItem:
        """把未进入执行计划的建议保存为后续对话可解析的待复核项。"""

        stale_items = (
            self.db.query(FileRenameReviewItem)
            .filter(
                FileRenameReviewItem.user_id == self.user_id,
                FileRenameReviewItem.conversation_id == conversation_id,
                FileRenameReviewItem.managed_file_id == suggestion.managed_file_id,
                FileRenameReviewItem.status == "NEEDS_REVIEW",
            )
            .all()
        )
        for stale in stale_items:
            stale.status = "INVALIDATED"
            stale.updated_at = utcnow()
        item = FileRenameReviewItem(
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            user_id=self.user_id,
            rename_batch_id=rename_batch_id,
            rename_batch_item_id=rename_batch_item_id,
            managed_file_id=suggestion.managed_file_id,
            document_id=suggestion.document_id or None,
            root_key=suggestion.root_key,
            original_relative_path=suggestion.relative_path,
            original_filename=suggestion.filename,
            source_sha256=suggestion.source_sha256,
            status="NEEDS_REVIEW",
            review_context_json={
                "status": suggestion.status,
                "document_date": suggestion.document_date.model_dump(mode="json"),
                "year": suggestion.year.model_dump(mode="json"),
                "document_number": suggestion.document_number.model_dump(mode="json"),
                "title": suggestion.title.model_dump(mode="json"),
                "warnings": suggestion.warnings,
                "errors": suggestion.errors,
                "policy_key": suggestion.policy_key,
                "policy_version": suggestion.policy_version,
            },
        )
        self.db.add(item)
        self.db.flush()
        return item

    def resolve_message(
        self,
        *,
        conversation_id: str,
        agent_run_id: str,
        message: str,
    ) -> dict[str, Any]:
        """处理放弃或人工更正消息，并在更正成功时立即确认和执行计划。"""

        pending = self._pending_items(conversation_id=conversation_id)
        if not pending:
            return _resolution_error("PENDING_RENAME_NOT_FOUND", "当前会话没有待复核的重命名文件。")
        batch = self._batch_for_pending(pending)
        batch_service = RenameBatchService(self.db, self.user_id) if batch is not None else None
        normalized_message = message.strip().rstrip("。！!")
        if normalized_message in _DISMISS_MESSAGES:
            for item in pending:
                item.status = "DISMISSED"
                item.decision_json = {"action": "dismiss", "message": normalized_message}
                item.updated_at = utcnow()
                batch_item = self._batch_item(item)
                if batch_item is not None:
                    batch_item.status = "EXCLUDED"
                    batch_item.decision_json = {"action": "exclude", "message": normalized_message}
                    batch_item.updated_at = utcnow()
            if batch_service and batch:
                batch_service.refresh_counts(batch)
            self.db.flush()
            return {
                "ok": True,
                "kind": "rename_review_resolution",
                "status": "COMPLETED",
                "dismissed_count": len(pending),
                "completed_items": [],
                "failed_items": [],
                "ambiguous_items": [],
                "operation_plan_id": None,
                "changeset_id": None,
                "rename_batch_id": batch.id if batch else None,
                "accepted_count": 0,
                "remaining_review_count": batch.needs_review_count if batch else 0,
            }

        corrections, parse_failures = _parse_corrections(message)
        if not corrections:
            return _resolution_error(
                "RENAME_CORRECTION_REQUIRED",
                "请按“文件原文件名更正为新文件名”的格式提供名称。",
            )

        plan_candidates: list[tuple[FileRenameReviewItem, str]] = []
        ambiguous_items: list[dict[str, Any]] = []
        failed_items = list(parse_failures)
        for source_name, requested_name in corrections:
            matches = _match_pending_items(pending, source_name)
            if not matches:
                failed_items.append({
                    "source": source_name,
                    "requested_name": requested_name,
                    "error_code": "PENDING_RENAME_NOT_FOUND",
                    "error_message": "未找到对应的待复核文件。",
                })
                continue
            if len(matches) > 1:
                ambiguous_items.append({
                    "source": source_name,
                    "requested_name": requested_name,
                    "candidates": [
                        {
                            "review_id": item.id,
                            "root_key": item.root_key,
                            "relative_path": item.original_relative_path,
                            "filename": item.original_filename,
                        }
                        for item in matches
                    ],
                })
                continue
            item = matches[0]
            try:
                target_relative_path = _build_target_relative_path(item, requested_name)
            except ValueError as exc:
                failed_items.append({
                    "review_id": item.id,
                    "source": source_name,
                    "requested_name": requested_name,
                    "error_code": "INVALID_TARGET_FILENAME",
                    "error_message": str(exc),
                })
                continue
            plan_candidates.append((item, target_relative_path))

        # 同一批次目标名重复时，仅保留不冲突的人工更正，避免覆盖其他文件。
        duplicate_targets = {
            target for target, count in Counter(target for _, target in plan_candidates).items() if count > 1
        }
        occupied_targets = {
            item.proposed_relative_path
            for item in (
                batch_service.list_all_items(batch_id=batch.id, statuses={"READY", "USER_NAMED"})
                if batch_service and batch
                else []
            )
            if item.proposed_relative_path
        }
        executable: list[tuple[FileRenameReviewItem, str]] = []
        for item, target_relative_path in plan_candidates:
            if target_relative_path in duplicate_targets or target_relative_path in occupied_targets:
                failed_items.append({
                    "review_id": item.id,
                    "source": item.original_filename,
                    "requested_name": Path(target_relative_path).name,
                    "error_code": "DUPLICATE_TARGET",
                    "error_message": "多个文件被更正为相同名称，请分别指定不同名称。",
                })
                continue
            executable.append((item, target_relative_path))

        plan = None
        result = None
        changeset = None
        if executable and batch is not None and batch_service is not None:
            batch_executable: list[tuple[FileRenameReviewItem, FileRenameBatchItem, str]] = []
            for item, target_relative_path in executable:
                batch_item = self._batch_item(item)
                if batch_item is None:
                    failed_items.append({
                        "review_id": item.id,
                        "source": item.original_filename,
                        "error_code": "RENAME_BATCH_ITEM_NOT_FOUND",
                        "error_message": "重命名批次文件项不存在，请重新发起重命名任务。",
                    })
                    continue
                batch_executable.append((item, batch_item, target_relative_path))
                batch_item.proposed_relative_path = target_relative_path
                batch_item.proposed_filename = Path(target_relative_path).name
                batch_item.status = "USER_NAMED"
                batch_item.decision_json = {
                    "action": "rename",
                    "message": message[:200],
                }
                batch_item.updated_at = utcnow()
                item.status = "CORRECTED"
                item.decision_json = {
                    "action": "rename",
                    "requested_relative_path": target_relative_path,
                }
                item.updated_at = utcnow()
            plan = batch_service.create_operation_plan_for_ready(
                batch,
                item_ids={batch_item.id for _, batch_item, _ in batch_executable},
                reason="用户在待确认回执中明确提供并确认了文件名",
                reuse_waiting_plan=False,
            )
            if plan is not None:
                self.repository.confirm_plan(
                    plan=plan,
                    user_id=self.user_id,
                    confirmation_text=message[:200],
                )
                result, changeset = ConfirmedRenameService(self.db).execute(plan=plan)
                batch_service.record_execution_result(batch, result)
                result_by_id = {entry.managed_file_id: entry for entry in result.items}
                for item, batch_item, target_relative_path in batch_executable:
                    execution = result_by_id.get(item.managed_file_id)
                    item.status = "EXECUTED" if execution and execution.status == "COMPLETED" else "NEEDS_REVIEW"
                    item.decision_json = {
                        "action": "rename",
                        "requested_relative_path": target_relative_path,
                        "operation_plan_id": plan.id,
                        "execution": execution.model_dump(mode="json") if execution else {},
                    }
                    item.updated_at = utcnow()
                    if execution is None or execution.status != "COMPLETED":
                        batch_item.status = "FAILED"
        elif executable:
            # 迁移前遗留待复核项没有批次关系，继续兼容原来的单次人工确认执行。
            user = self.db.get(User, self.user_id)
            if user is None or not user.default_workspace_id:
                return _resolution_error("USER_WORKSPACE_REQUIRED", "当前用户缺少默认工作区。")
            plan = self.repository.create_plan(
                workspace_id=user.default_workspace_id,
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
                user_id=self.user_id,
                operation_type="RENAME_FILES",
                risk_level="medium",
                reason="用户在待复核回执中明确提供更正后的文件名",
                plan_json={
                    "policy_key": "manual-correction",
                    "policy_version": "1",
                    "items": [_manual_operation_plan_item(item, target) for item, target in executable],
                },
            )
            self.repository.confirm_plan(
                plan=plan,
                user_id=self.user_id,
                confirmation_text=message[:200],
            )
            result, changeset = ConfirmedRenameService(self.db).execute(plan=plan)
            result_by_id = {entry.managed_file_id: entry for entry in result.items}
            for item, target_relative_path in executable:
                execution = result_by_id.get(item.managed_file_id)
                # 单项冲突或执行失败后继续保留待复核状态，允许用户再次提供名称。
                item.status = "EXECUTED" if execution and execution.status == "COMPLETED" else "NEEDS_REVIEW"
                item.decision_json = {
                    "action": "rename",
                    "requested_relative_path": target_relative_path,
                    "operation_plan_id": plan.id,
                    "execution": execution.model_dump(mode="json") if execution else {},
                }
                item.updated_at = utcnow()

        self.db.flush()
        completed_items = [] if result is None else [
            item.model_dump(mode="json") for item in result.items if item.status == "COMPLETED"
        ]
        execution_failures = [] if result is None else [
            item.model_dump(mode="json") for item in result.items if item.status != "COMPLETED"
        ]
        failed_items.extend(execution_failures)
        status = _resolution_status(
            completed_count=len(completed_items),
            failed_count=len(failed_items),
            ambiguous_count=len(ambiguous_items),
        )
        return {
            "ok": bool(completed_items) or (not failed_items and not ambiguous_items),
            "kind": "rename_review_resolution",
            "status": status,
            "dismissed_count": 0,
            "completed_items": completed_items,
            "failed_items": failed_items,
            "ambiguous_items": ambiguous_items,
            "operation_plan_id": plan.id if plan else None,
            "changeset_id": changeset.id if changeset else None,
            "rename_batch_id": batch.id if batch else None,
            "accepted_count": len(executable),
            "remaining_review_count": batch.needs_review_count if batch else 0,
        }

    def _pending_items(self, *, conversation_id: str) -> list[FileRenameReviewItem]:
        """读取当前用户和会话的待复核项，较新的批次优先。"""

        query = self.db.query(FileRenameReviewItem)
        latest_batch_id = (
            self.db.query(FileRenameReviewItem.rename_batch_id)
            .filter(
                FileRenameReviewItem.user_id == self.user_id,
                FileRenameReviewItem.conversation_id == conversation_id,
                FileRenameReviewItem.status == "NEEDS_REVIEW",
                FileRenameReviewItem.rename_batch_id.isnot(None),
            )
            .order_by(FileRenameReviewItem.created_at.desc())
            .limit(1)
            .scalar()
        )
        query = query.filter(
            FileRenameReviewItem.user_id == self.user_id,
            FileRenameReviewItem.conversation_id == conversation_id,
            FileRenameReviewItem.status == "NEEDS_REVIEW",
        )
        if latest_batch_id:
            query = query.filter(FileRenameReviewItem.rename_batch_id == latest_batch_id)
        else:
            query = query.filter(FileRenameReviewItem.rename_batch_id.is_(None))
        return query.order_by(FileRenameReviewItem.created_at.desc()).all()

    def _batch_for_pending(self, pending: list[FileRenameReviewItem]) -> FileRenameBatch | None:
        """从最新待复核项解析其唯一批次。"""

        batch_ids = {item.rename_batch_id for item in pending if item.rename_batch_id}
        if len(batch_ids) != 1:
            return None
        return self.db.get(FileRenameBatch, next(iter(batch_ids)))

    def _batch_item(self, review_item: FileRenameReviewItem) -> FileRenameBatchItem | None:
        """读取待复核项对应的批次文件。"""

        if not review_item.rename_batch_item_id:
            return None
        return self.db.get(FileRenameBatchItem, review_item.rename_batch_item_id)


def _parse_corrections(message: str) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """按行解析人工文件名更正，错误行保留为独立失败项。"""

    corrections: list[tuple[str, str]] = []
    failures: list[dict[str, Any]] = []
    lines = [line.strip() for line in message.replace("；", "\n").splitlines() if line.strip()]
    for line in lines:
        matched = _CORRECTION_PATTERN.match(line)
        if not matched:
            failures.append({
                "source": line,
                "error_code": "INVALID_CORRECTION_FORMAT",
                "error_message": "请使用“文件原文件名更正为新文件名”的格式。",
            })
            continue
        source = _strip_name_quotes(matched.group("source"))
        target = _strip_name_quotes(matched.group("target"))
        if not source or not target:
            failures.append({
                "source": source or line,
                "error_code": "INVALID_CORRECTION_FORMAT",
                "error_message": "原文件名和新文件名不能为空。",
            })
            continue
        corrections.append((source, target))
    return corrections, failures


def _match_pending_items(
    pending: list[FileRenameReviewItem],
    source_name: str,
) -> list[FileRenameReviewItem]:
    """优先按完整相对路径匹配，再按文件名匹配。"""

    normalized = source_name.replace("\\", "/").strip().strip("/")
    by_path = [item for item in pending if item.original_relative_path == normalized]
    if by_path:
        return by_path
    return [item for item in pending if item.original_filename == normalized]


def _build_target_relative_path(item: FileRenameReviewItem, requested_name: str) -> str:
    """校验人工名称并保留源扩展名和原父目录。"""

    target = _strip_name_quotes(requested_name).strip()
    if not target or target in {".", ".."} or "/" in target or "\\" in target:
        raise ValueError("更正后的名称必须是单个文件名，不能包含目录路径。")
    if target.startswith("."):
        raise ValueError("更正后的名称不能是隐藏文件名。")
    source_suffix = Path(item.original_filename).suffix
    requested_suffix = Path(target).suffix
    if requested_suffix and requested_suffix.lower() != source_suffix.lower():
        raise ValueError("更正后的名称不能改变原文件扩展名。")
    if not requested_suffix:
        target = f"{target}{source_suffix}"
    if len(target.encode("utf-8")) > 240:
        raise ValueError("更正后的文件名超过 240 字节限制。")
    parent = PurePosixPath(item.original_relative_path).parent
    return (parent / target).as_posix()


def _manual_operation_plan_item(item: FileRenameReviewItem, target_relative_path: str) -> dict[str, Any]:
    """把用户明确更正转换成可审计的 OperationPlan 项。"""

    return {
        "document_id": item.document_id or "",
        "before": {
            "managed_file_id": item.managed_file_id,
            "root_key": item.root_key,
            "relative_path": item.original_relative_path,
            "filename": item.original_filename,
            "source_sha256": item.source_sha256,
        },
        "after": {
            "relative_path": target_relative_path,
            "filename": Path(target_relative_path).name,
        },
        "rename_metadata": {
            "source": "user_correction",
            "review_item_id": item.id,
        },
        "execution_status": "PLANNED",
    }


def _strip_name_quotes(value: str) -> str:
    """移除用户输入中文件名两侧常见引号。"""

    return value.strip().strip("\"'“”‘’《》")


def _resolution_status(*, completed_count: int, failed_count: int, ambiguous_count: int) -> str:
    """根据逐项结果生成总体状态。"""

    if completed_count and not failed_count and not ambiguous_count:
        return "EXECUTED"
    if completed_count:
        return "PARTIAL"
    return "NEEDS_REVIEW"


def _resolution_error(code: str, message: str) -> dict[str, Any]:
    """构造待复核消息处理错误。"""

    return {
        "ok": False,
        "kind": "rename_review_resolution",
        "status": "NEEDS_REVIEW",
        "error": {"code": code, "message": message},
        "dismissed_count": 0,
        "completed_items": [],
        "failed_items": [],
        "ambiguous_items": [],
        "operation_plan_id": None,
        "changeset_id": None,
    }
