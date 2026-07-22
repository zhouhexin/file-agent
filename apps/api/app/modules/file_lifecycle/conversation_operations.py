"""把自然语言文件动作收敛为真实、待确认的工作副本 OperationPlan。

本模块只接受后端已解析的附件 ID 或持久化同名冲突记录。Planner 不能提交物理路径、
工作副本 ID 或回收站 ID，从而避免自然语言猜测越过用户、会话和工作区边界。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    Document,
    DocumentVersion,
    FileRenameReviewItem,
    TrashEntry,
    UploadArchiveRecord,
    User,
    WorkingCopy,
    WorkingCopyRoot,
)
from app.modules.file_lifecycle.operations import WorkingCopyOperationService
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.operations.schemas import OperationPlanCreateRequest, OperationPlanItem


class ConversationalWorkingCopyPlanService:
    """按当前用户和会话解析删除、恢复及同名冲突计划。"""

    def __init__(self, db: Session, user_id: str) -> None:
        """保存请求级数据库会话，禁止跨请求复用带用户状态的服务。"""

        self.db = db
        self.user_id = user_id
        self.operations = WorkingCopyOperationService(db)
        self.storage = FileLifecycleStorageService()

    def prepare(
        self,
        *,
        action: str,
        message: str,
        document_ids: list[str],
        conversation_id: str,
        agent_run_id: str,
    ) -> dict[str, Any]:
        """创建待确认计划而不执行文件动作，并返回普通用户可理解的摘要。"""

        user = self.db.get(User, self.user_id)
        run = self.db.get(AgentRun, agent_run_id)
        if user is None or not user.default_workspace_id:
            return _error("USER_WORKSPACE_REQUIRED", "当前用户缺少默认工作区。")
        if run is None or run.user_id != user.id or run.conversation_id != conversation_id:
            return _error("AGENT_RUN_SCOPE_INVALID", "本次文件操作与当前对话范围不一致。")
        try:
            if action == "TRASH":
                plan = self._create_trash_plan(
                    user=user,
                    document_ids=document_ids,
                    conversation_id=conversation_id,
                    agent_run_id=agent_run_id,
                )
            elif action == "RESTORE":
                plan = self._create_restore_plan(
                    user=user,
                    document_ids=document_ids,
                    conversation_id=conversation_id,
                    agent_run_id=agent_run_id,
                )
            else:
                plan = self._create_conflict_plan(
                    user=user,
                    action=action,
                    message=message,
                    conversation_id=conversation_id,
                    agent_run_id=agent_run_id,
                )
        except HTTPException as exc:
            return _error(f"WORKING_COPY_PLAN_{exc.status_code}", str(exc.detail))
        except ValueError as exc:
            return _error("WORKING_COPY_SCOPE_INVALID", str(exc))
        self.db.commit()
        self.db.refresh(plan)
        return {
            "ok": True,
            "kind": "working_copy_operation_plan",
            "status": "WAITING_CONFIRMATION",
            "operation_plan_id": plan.id,
            "operation_type": plan.operation_type,
            "item_count": len([item for item in plan.plan_json.get("items", []) if isinstance(item, dict)]),
            "message": plan.reason,
        }

    def _create_trash_plan(
        self,
        *,
        user: User,
        document_ids: list[str],
        conversation_id: str,
        agent_run_id: str,
    ):
        """把后端确定的附件解析为活动工作副本，并只创建回收站计划。"""

        copies = self._resolve_working_copies(document_ids=document_ids, workspace_id=user.default_workspace_id)
        if not copies:
            raise ValueError("请明确选择要移入回收站的当前会话文件。")
        inactive = [item.filename for item in copies if item.status != "ACTIVE"]
        if inactive:
            raise ValueError(f"以下文件当前不能移入回收站：{'、'.join(inactive)}")
        plan = self.operations.create_plan(
            current_user=user,
            request=OperationPlanCreateRequest(
                conversation_id=conversation_id,
                operation_type="TRASH_WORKING_COPIES",
                risk_level="high",
                reason="把所选工作副本移入可恢复回收站",
                items=[OperationPlanItem(working_copy_id=item.id) for item in copies],
            ),
        )
        plan.agent_run_id = agent_run_id
        return plan

    def _create_restore_plan(
        self,
        *,
        user: User,
        document_ids: list[str],
        conversation_id: str,
        agent_run_id: str,
    ):
        """从附件追溯到唯一活动回收站条目，歧义时停止而不猜测。"""

        copies = self._resolve_working_copies(document_ids=document_ids, workspace_id=user.default_workspace_id)
        trashed = [item for item in copies if item.status == "TRASHED"]
        if len(trashed) != 1:
            raise ValueError("请在当前对话中明确指定一个已移入回收站的文件。")
        entries = (
            self.db.query(TrashEntry)
            .filter(
                TrashEntry.workspace_id == user.default_workspace_id,
                TrashEntry.working_copy_id == trashed[0].id,
                TrashEntry.status == "ACTIVE",
            )
            .all()
        )
        if len(entries) != 1:
            raise ValueError("没有找到唯一可恢复的回收站记录。")
        plan = self.operations.create_restore_plan(
            trash_entry_id=entries[0].id,
            conversation_id=conversation_id,
            current_user=user,
        )
        plan.agent_run_id = agent_run_id
        return plan

    def _create_conflict_plan(
        self,
        *,
        user: User,
        action: str,
        message: str,
        conversation_id: str,
        agent_run_id: str,
    ):
        """从待复核记录唯一解析冲突对象，不能由用户文本直接提供数据库 ID。"""

        decision = {
            "CONFLICT_KEEP_BOTH": "KEEP_BOTH",
            "CONFLICT_KEEP_EXISTING": "KEEP_EXISTING",
            "CONFLICT_REPLACE_EXISTING": "REPLACE_EXISTING_WORKING_COPY",
            "CONFLICT_DELETE_EXISTING": "DELETE_EXISTING_WORKING_COPY",
        }.get(action)
        if decision is None:
            raise ValueError("不支持的文件操作。")
        reviews = (
            self.db.query(FileRenameReviewItem)
            .filter(
                FileRenameReviewItem.user_id == user.id,
                FileRenameReviewItem.conversation_id == conversation_id,
                FileRenameReviewItem.status == "NEEDS_REVIEW",
            )
            .order_by(FileRenameReviewItem.created_at.desc())
            .all()
        )
        reviews = [item for item in reviews if item.review_context_json.get("reason") == "FILENAME_CONFLICT"]
        matched = [
            item
            for item in reviews
            if item.original_filename in message
            or str(item.review_context_json.get("filename") or "") in message
            or str(item.review_context_json.get("target_filename") or "") in message
        ]
        if len(matched) == 1:
            review = matched[0]
        elif len(reviews) == 1:
            review = reviews[0]
        else:
            raise ValueError("当前对话有多个同名冲突，请在消息中写出要处理的文件名。")
        context = dict(review.review_context_json or {})
        pending_copy = self.db.get(WorkingCopy, context.get("working_copy_id"))
        existing_ids = [str(value) for value in context.get("existing_working_copy_ids", []) if value]
        existing_copy = self.db.get(WorkingCopy, existing_ids[0]) if len(existing_ids) == 1 else None
        if pending_copy is None or existing_copy is None:
            raise ValueError("同名冲突记录已失效，请重新整理文件。")
        target_filename = Path(str(context.get("target_filename") or "")).name
        if not target_filename or target_filename in {".", ".."}:
            raise ValueError("同名冲突缺少有效目标文件名。")
        target_parent = PurePosixPath(existing_copy.relative_path).parent
        target_relative_path = (target_parent / target_filename).as_posix()
        if decision == "KEEP_BOTH":
            target_relative_path = self._next_version_path(
                pending_copy=pending_copy,
                target_parent=target_parent,
                target_filename=target_filename,
            )
        return self.operations.create_conflict_resolution_plan(
            review=review,
            pending_copy=pending_copy,
            existing_copy=existing_copy,
            decision=decision,
            target_relative_path=target_relative_path,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            current_user=user,
        )

    def _resolve_working_copies(self, *, document_ids: list[str], workspace_id: str) -> list[WorkingCopy]:
        """把上传附件或工作副本文档 ID 解析为当前用户工作副本，保持输入顺序去重。"""

        if not document_ids:
            return []
        documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == self.user_id)
            .all()
        )
        by_id = {item.id: item for item in documents}
        if any(document_id not in by_id for document_id in document_ids):
            raise ValueError("部分文件不存在或不属于当前用户。")
        resolved: list[WorkingCopy] = []
        for document_id in document_ids:
            document = by_id[document_id]
            direct = (
                self.db.query(WorkingCopy)
                .filter(WorkingCopy.document_id == document.id, WorkingCopy.workspace_id == workspace_id)
                .one_or_none()
            )
            if direct is not None:
                resolved.append(direct)
                continue
            upload_version = (
                self.db.query(DocumentVersion)
                .filter(DocumentVersion.document_id == document.id, DocumentVersion.storage_tier == "UPLOAD")
                .order_by(DocumentVersion.version_number.desc())
                .first()
            )
            archive = (
                self.db.query(UploadArchiveRecord)
                .filter(UploadArchiveRecord.upload_document_version_id == upload_version.id)
                .one_or_none()
                if upload_version is not None
                else None
            )
            working_copy = (
                self.db.query(WorkingCopy)
                .join(Document, Document.id == WorkingCopy.document_id)
                .filter(
                    WorkingCopy.managed_file_id == archive.managed_file_id,
                    WorkingCopy.workspace_id == workspace_id,
                    Document.user_id == self.user_id,
                )
                .one_or_none()
                if archive is not None and archive.managed_file_id
                else None
            )
            if working_copy is None:
                raise ValueError("文件尚未形成可操作的工作副本。")
            resolved.append(working_copy)
        # dict 保持插入顺序，确保批量计划顺序与用户附件顺序一致。
        return list({item.id: item for item in resolved}.values())

    def _next_version_path(
        self,
        *,
        pending_copy: WorkingCopy,
        target_parent: PurePosixPath,
        target_filename: str,
    ) -> str:
        """在用户选择同时保留后分配稳定版本后缀，并检查索引和文件系统。"""

        suffix = Path(target_filename).suffix
        stem = target_filename[: -len(suffix)] if suffix else target_filename
        root = self.db.get(WorkingCopyRoot, pending_copy.working_copy_root_id)
        if root is None:
            raise ValueError("工作副本根不存在。")
        for version in range(2, 1000):
            label = _version_label(version)
            filename = f"{stem}_第{label}版{suffix}"
            relative_path = (target_parent / filename).as_posix()
            indexed = (
                self.db.query(WorkingCopy.id)
                .filter(
                    WorkingCopy.working_copy_root_id == pending_copy.working_copy_root_id,
                    WorkingCopy.relative_path == relative_path,
                    WorkingCopy.status == "ACTIVE",
                )
                .first()
            )
            physical = self.storage.working_copy_path(f"{root.relative_storage_path}/{relative_path}")
            if indexed is None and not physical.exists():
                return relative_path
        raise ValueError("无法分配可用的版本后缀，请先整理同名文件。")


def _version_label(version: int) -> str:
    """为常见版本号生成中文标签，较大版本保留稳定数字表达。"""

    labels = {2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
    return labels.get(version, str(version))


def _error(code: str, message: str) -> dict[str, Any]:
    """构造不会泄漏路径和内部对象的 Tool 失败结果。"""

    return {
        "ok": False,
        "kind": "working_copy_operation_plan",
        "status": "FAILED",
        "error": {"code": code, "message": message},
    }
