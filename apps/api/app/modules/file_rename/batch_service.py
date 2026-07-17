"""重命名范围批次、逐文件状态和完整性门禁。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import FileRenameBatch, FileRenameBatchItem, FileRenameReviewItem, User, utcnow
from app.modules.file_rename.schemas import RenameSuggestion
from app.modules.operations.repository import OperationPlanRepository


EXECUTABLE_STATUSES = {"READY", "USER_NAMED"}
RESOLVED_STATUSES = {*EXECUTABLE_STATUSES, "EXCLUDED"}


class RenameBatchService:
    """持久化一次固定文件范围，并在全部文件已决策后创建计划。"""

    def __init__(self, db: Session, user_id: str) -> None:
        self.db = db
        self.user_id = user_id

    def create_batch(
        self,
        *,
        conversation_id: str,
        agent_run_id: str,
        scope: dict[str, Any],
    ) -> FileRenameBatch:
        """创建仍处于分析阶段的批次。"""

        user = self.db.get(User, self.user_id)
        if user is None or not user.default_workspace_id:
            raise ValueError("当前用户缺少默认工作区，无法创建重命名批次。")
        batch = FileRenameBatch(
            workspace_id=user.default_workspace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            user_id=self.user_id,
            status="ANALYZING",
            scope_json=scope,
        )
        self.db.add(batch)
        self.db.flush()
        return batch

    def add_suggestion(
        self,
        *,
        batch: FileRenameBatch,
        suggestion: RenameSuggestion,
        position: int,
    ) -> FileRenameBatchItem:
        """把一条解析建议固化为批次文件项。"""

        status = "READY" if suggestion.status == "READY" else "NEEDS_REVIEW"
        item = FileRenameBatchItem(
            rename_batch_id=batch.id,
            managed_file_id=suggestion.managed_file_id,
            document_id=suggestion.document_id or None,
            root_key=suggestion.root_key,
            original_relative_path=suggestion.relative_path,
            original_filename=suggestion.filename,
            source_sha256=suggestion.source_sha256,
            proposed_relative_path=suggestion.proposed_relative_path,
            proposed_filename=suggestion.proposed_filename,
            status=status,
            position=position,
            metadata_json={"suggestion": suggestion.model_dump(mode="json")},
        )
        self.db.add(item)
        self.db.flush()
        return item

    def refresh_counts(self, batch: FileRenameBatch) -> FileRenameBatch:
        """根据文件项真实状态刷新批次统计和门禁状态。"""

        self.db.flush()
        statuses = [
            value
            for value, in self.db.query(FileRenameBatchItem.status)
            .filter(FileRenameBatchItem.rename_batch_id == batch.id)
            .all()
        ]
        counts = Counter(statuses)
        batch.total_count = len(statuses)
        batch.ready_count = counts["READY"] + counts["USER_NAMED"]
        batch.needs_review_count = counts["NEEDS_REVIEW"] + counts["FAILED"]
        batch.excluded_count = counts["EXCLUDED"]
        batch.completed_count = counts["COMPLETED"]
        batch.failed_count = counts["FAILED"]
        if batch.status not in {"EXECUTING", "COMPLETED", "PARTIAL_FAILED"}:
            batch.status = "NEEDS_REVIEW" if batch.needs_review_count else "READY_FOR_CONFIRMATION"
        batch.updated_at = utcnow()
        self.db.flush()
        return batch

    def create_operation_plan_if_complete(self, batch: FileRenameBatch) -> Any | None:
        """仅在范围内没有待复核项时，为所有可执行文件创建一个计划。"""

        self.refresh_counts(batch)
        if batch.needs_review_count:
            return None
        if batch.operation_plan_id:
            return OperationPlanRepository(self.db).get_owned_plan(
                plan_id=batch.operation_plan_id,
                user_id=self.user_id,
            )
        items = self.list_all_items(batch_id=batch.id, statuses=EXECUTABLE_STATUSES)
        if not items:
            batch.status = "COMPLETED"
            self.db.flush()
            return None
        plan = OperationPlanRepository(self.db).create_plan(
            workspace_id=batch.workspace_id,
            conversation_id=batch.conversation_id,
            agent_run_id=batch.agent_run_id,
            user_id=batch.user_id,
            operation_type="RENAME_FILES",
            risk_level="medium",
            reason="批次内全部文件已生成名称或由用户明确排除",
            plan_json={
                "policy_key": str(batch.scope_json.get("policy_key") or "school_official_document"),
                "policy_version": str(batch.scope_json.get("policy_version") or "1.3"),
                "scope": {**batch.scope_json, "rename_batch_id": batch.id},
                "items": [_operation_plan_item(item) for item in items],
                "skipped_items": [],
            },
        )
        batch.operation_plan_id = plan.id
        batch.status = "READY_FOR_CONFIRMATION"
        batch.updated_at = utcnow()
        self.db.flush()
        return plan

    def get_owned_batch(self, batch_id: str) -> FileRenameBatch:
        """读取当前用户自己的重命名批次。"""

        batch = (
            self.db.query(FileRenameBatch)
            .filter(FileRenameBatch.id == batch_id, FileRenameBatch.user_id == self.user_id)
            .one_or_none()
        )
        if batch is None:
            raise HTTPException(status_code=404, detail="Rename batch not found")
        return batch

    def list_all_items(
        self,
        *,
        batch_id: str,
        statuses: set[str] | None = None,
    ) -> list[FileRenameBatchItem]:
        """按稳定顺序读取批次文件项，供计划创建和执行审计使用。"""

        query = self.db.query(FileRenameBatchItem).filter(FileRenameBatchItem.rename_batch_id == batch_id)
        if statuses:
            query = query.filter(FileRenameBatchItem.status.in_(statuses))
        return query.order_by(FileRenameBatchItem.position.asc(), FileRenameBatchItem.id.asc()).all()

    def list_page(
        self,
        *,
        batch: FileRenameBatch,
        status: str | None,
        cursor: int,
        limit: int,
    ) -> tuple[list[FileRenameBatchItem], int | None]:
        """按位置游标返回一页文件项，避免聊天页面一次加载整个批次。"""

        query = self.db.query(FileRenameBatchItem).filter(
            FileRenameBatchItem.rename_batch_id == batch.id,
            FileRenameBatchItem.position >= cursor,
        )
        if status == "EXECUTABLE":
            query = query.filter(FileRenameBatchItem.status.in_({"READY", "USER_NAMED"}))
        elif status:
            query = query.filter(FileRenameBatchItem.status == status)
        rows = query.order_by(FileRenameBatchItem.position.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = page[-1].position + 1 if has_more and page else None
        return page, next_cursor

    def record_execution_result(self, batch: FileRenameBatch, result: Any) -> FileRenameBatch:
        """把执行器逐文件结果同步回批次，保留失败隔离。"""

        result_by_file_id = {entry.managed_file_id: entry for entry in result.items}
        for item in self.list_all_items(batch_id=batch.id, statuses=EXECUTABLE_STATUSES):
            execution = result_by_file_id.get(item.managed_file_id)
            item.status = "COMPLETED" if execution and execution.status == "COMPLETED" else "FAILED"
            item.decision_json = {
                **(item.decision_json if isinstance(item.decision_json, dict) else {}),
                "execution": execution.model_dump(mode="json") if execution else {},
            }
            item.updated_at = utcnow()
            if item.status == "FAILED":
                self._ensure_failed_review_item(batch=batch, item=item)
        self.refresh_counts(batch)
        if batch.failed_count:
            # 旧计划继续保留审计，但失败项需要在用户更正后生成新的重试计划。
            batch.operation_plan_id = None
            batch.status = "NEEDS_REVIEW"
        else:
            batch.status = "COMPLETED"
        batch.updated_at = utcnow()
        self.db.flush()
        return batch

    def _ensure_failed_review_item(
        self,
        *,
        batch: FileRenameBatch,
        item: FileRenameBatchItem,
    ) -> FileRenameReviewItem:
        """为执行失败项创建或恢复待复核记录，支持用户更名后单独重试。"""

        review = (
            self.db.query(FileRenameReviewItem)
            .filter(
                FileRenameReviewItem.rename_batch_item_id == item.id,
                FileRenameReviewItem.user_id == self.user_id,
            )
            .one_or_none()
        )
        if review is None:
            review = FileRenameReviewItem(
                conversation_id=batch.conversation_id,
                agent_run_id=batch.agent_run_id,
                user_id=batch.user_id,
                rename_batch_id=batch.id,
                rename_batch_item_id=item.id,
                managed_file_id=item.managed_file_id,
                document_id=item.document_id,
                root_key=item.root_key,
                original_relative_path=item.original_relative_path,
                original_filename=item.original_filename,
                source_sha256=item.source_sha256,
                review_context_json=item.metadata_json,
            )
            self.db.add(review)
        review.status = "NEEDS_REVIEW"
        review.decision_json = {
            "action": "retry_required",
            "execution": item.decision_json.get("execution", {}),
        }
        review.updated_at = utcnow()
        self.db.flush()
        return review


def _operation_plan_item(item: FileRenameBatchItem) -> dict[str, Any]:
    """把已经完成决策的批次文件项转换为不可变计划项。"""

    metadata = item.metadata_json.get("suggestion", {}) if isinstance(item.metadata_json, dict) else {}
    rename_metadata = {
        "source": "user_correction" if item.status == "USER_NAMED" else "automatic",
        "rename_batch_item_id": item.id,
    }
    if isinstance(metadata, dict):
        rename_metadata.update({
            "policy_key": metadata.get("policy_key"),
            "policy_version": metadata.get("policy_version"),
            "template_key": metadata.get("template_key"),
            "document_date": metadata.get("document_date", {}),
            "year": metadata.get("year", {}),
            "document_number": metadata.get("document_number", {}),
            "title": metadata.get("title", {}),
            "parse_mode": metadata.get("rename_parse_mode", ""),
            "candidate_parsers": metadata.get("rename_candidate_parsers", []),
            "arbitration_warnings": metadata.get("arbitration_warnings", []),
        })
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
            "relative_path": item.proposed_relative_path,
            "filename": item.proposed_filename,
        },
        "rename_metadata": rename_metadata,
        "execution_status": "PLANNED",
    }
