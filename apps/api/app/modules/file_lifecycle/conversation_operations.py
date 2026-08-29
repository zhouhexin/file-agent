"""把自然语言文件动作收敛为真实、可审计的工作副本 OperationPlan。

本模块只接受后端已解析的附件 ID 或持久化同名冲突记录。Planner 不能提交物理路径、
工作副本 ID 或回收站 ID，从而避免自然语言猜测越过用户、会话和工作区边界。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    AgentRun,
    ChangeItem,
    ChangeSet,
    Document,
    DocumentCategory,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    FileRenameReviewItem,
    TrashEntry,
    User,
    WorkingCopy,
    WorkingCopyRoot,
    utcnow,
)
from app.modules.classification.auto_placement_policy import AutoPlacementPolicy
from app.modules.classification.organization_repository import (
    OrganizationDecisionRepository,
)
from app.modules.classification.organization_path import (
    CategoryOrganizationPathError,
    CategoryOrganizationPathResolver,
)
from app.modules.file_lifecycle.operations import WorkingCopyOperationService
from app.modules.file_lifecycle.shared_access import (
    CanonicalWorkingFileError,
    CanonicalWorkingFileResolver,
)
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.operations.schemas import OperationPlanCreateRequest, OperationPlanItem
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


class WorkingCopyConflictPending(ValueError):
    """目标同名冲突已经持久化，等待用户选择处理方式。"""

    def __init__(self, *, filename: str) -> None:
        super().__init__(f"目标目录已存在同名文件“{filename}”。")
        self.filename = filename


class ConversationalWorkingCopyPlanService:
    """按当前用户和会话解析删除、恢复、分类归位及同名冲突计划。"""

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
        """创建受控计划；显式分类归位直接执行，其他高风险动作等待确认。"""

        user = self.db.get(User, self.user_id)
        run = self.db.get(AgentRun, agent_run_id)
        if user is None:
            return _error("USER_NOT_FOUND", "当前用户不存在。")
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
            elif action == "MOVE_BY_CONFIRMED_CATEGORY":
                outcome = self._create_category_move_plan(
                    user=user,
                    document_ids=document_ids,
                    conversation_id=conversation_id,
                    agent_run_id=agent_run_id,
                )
                if isinstance(outcome, dict):
                    self.db.commit()
                    return outcome
                plan = outcome
            elif action == "MOVE_AFTER_AUTO_RECLASSIFICATION":
                outcome = self._create_auto_reclassification_move_plan(
                    user=user,
                    document_ids=document_ids,
                    conversation_id=conversation_id,
                    agent_run_id=agent_run_id,
                )
                if isinstance(outcome, dict):
                    self.db.commit()
                    return outcome
                plan = outcome
            elif action == "CONFLICT_CANCEL":
                message_text = self._cancel_conflict(
                    user=user,
                    message=message,
                    conversation_id=conversation_id,
                )
                self.db.commit()
                return {
                    "ok": True,
                    "kind": "working_copy_conflict_cancelled",
                    "status": "COMPLETED",
                    "message": message_text,
                }
            else:
                plan = self._create_conflict_plan(
                    user=user,
                    action=action,
                    message=message,
                    conversation_id=conversation_id,
                    agent_run_id=agent_run_id,
                )
        except WorkingCopyConflictPending as exc:
            self.db.commit()
            return {
                "ok": True,
                "kind": "filename_conflict",
                "status": "NEEDS_REVIEW",
                "filename": exc.filename,
                "allowed_decisions": [
                    "REPLACE_EXISTING_WORKING_COPY",
                    "KEEP_BOTH",
                    "CANCEL",
                ],
                "message": (
                    f"目标目录已存在同名文件“{exc.filename}”。"
                    "请选择“覆盖已有文件”“同时保留”或“取消”。"
                ),
            }
        except HTTPException as exc:
            return _error(f"WORKING_COPY_PLAN_{exc.status_code}", str(exc.detail))
        except ValueError as exc:
            return _error("WORKING_COPY_SCOPE_INVALID", str(exc))
        if action in {
            "MOVE_BY_CONFIRMED_CATEGORY",
            "MOVE_AFTER_AUTO_RECLASSIFICATION",
        }:
            # 用户在当前消息中已经明确要求分类、重新分类或按分类整理，
            # 该指令本身就是本次归位授权；仍保留 OperationPlan、确认记录、
            # 路径快照和 ChangeSet，但不再要求用户发送第二条确认消息。
            self.operations.plan_repository.confirm_plan(
                plan=plan,
                user_id=user.id,
                confirmation_text=message,
            )
            result, changeset_id = self.operations.execute(
                plan=plan,
                current_user=user,
            )
            self.db.commit()
            self.db.refresh(plan)
            completed_count = int(result.get("completed_count") or 0)
            return {
                "ok": plan.status in {"EXECUTED", "PARTIAL"},
                "kind": "working_copy_operation_result",
                "status": plan.status,
                "operation_plan_id": plan.id,
                "operation_type": plan.operation_type,
                "changeset_id": changeset_id,
                "item_count": len(result.get("items") or []),
                "items": list(result.get("items") or []),
                "file_position_changed": completed_count > 0,
                "message": (
                    "已按分类直接移动工作副本。"
                    if completed_count > 0
                    else "分类已保存，但文件移动未完成，请查看失败原因。"
                ),
            }
        if action == "CONFLICT_REPLACE_EXISTING":
            # 用户当前回复已经构成唯一冲突的明确覆盖确认；仍然创建、确认并执行
            # OperationPlan，只是不再插入第二次重复确认。
            self.operations.plan_repository.confirm_plan(
                plan=plan,
                user_id=user.id,
                confirmation_text=message,
            )
            result, changeset_id = self.operations.execute(
                plan=plan,
                current_user=user,
            )
            self.db.commit()
            self.db.refresh(plan)
            return {
                "ok": True,
                "kind": "working_copy_operation_result",
                "status": plan.status,
                "operation_plan_id": plan.id,
                "operation_type": plan.operation_type,
                "changeset_id": changeset_id,
                "item_count": len(result.get("items") or []),
                "message": "已按你的选择覆盖同名工作副本，旧文件已移入可恢复回收站。",
            }
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

    def _create_auto_reclassification_move_plan(
        self,
        *,
        user: User,
        document_ids: list[str],
        conversation_id: str,
        agent_run_id: str,
    ):
        """比较本次重新分类与正式主分类，达标时生成并直接执行移动计划。"""

        settings = get_settings()
        copies = self._resolve_working_copies(
            document_ids=document_ids,
            workspace_id=get_shared_workspace_id(self.db),
        )
        if not copies:
            raise ValueError("请明确选择要重新分类并整理的共享文件。")

        policy = AutoPlacementPolicy(settings)
        repository = OrganizationDecisionRepository(self.db)
        changed_document_ids: list[str] = []
        details: list[dict[str, Any]] = []
        for working_copy in copies:
            classification_run = (
                self.db.query(DocumentClassificationRun)
                .filter(
                    DocumentClassificationRun.agent_run_id == agent_run_id,
                    DocumentClassificationRun.document_id == working_copy.document_id,
                    DocumentClassificationRun.status == "COMPLETED",
                )
                .order_by(DocumentClassificationRun.created_at.desc())
                .first()
            )
            suggestions = (
                self.db.query(DocumentCategorySuggestion)
                .filter(
                    DocumentCategorySuggestion.classification_run_id
                    == classification_run.id
                )
                .order_by(
                    DocumentCategorySuggestion.rank.asc(),
                    DocumentCategorySuggestion.confidence.desc(),
                )
                .all()
                if classification_run is not None
                else []
            )
            primary_suggestion = suggestions[0] if suggestions else None
            if (
                classification_run is None
                or primary_suggestion is None
                or primary_suggestion.document_version_id
                != working_copy.current_version_id
            ):
                details.append(
                    {
                        "document_id": working_copy.document_id,
                        "filename": working_copy.filename,
                        "status": "SKIPPED",
                        "reason_codes": ["CURRENT_RECLASSIFICATION_NOT_FOUND"],
                    }
                )
                continue

            categories = [self._policy_category(item) for item in suggestions]
            policy_result = policy.evaluate(
                categories=categories,
                extraction_status=classification_run.status,
                risk_passed=True,
            )
            reason_codes = list(policy_result.reason_codes)
            if (
                not settings.auto_primary_classification_enabled
                or settings.auto_classification_shadow_mode
            ):
                reason_codes.append("AUTO_RECLASSIFICATION_DISABLED")

            active_primary = (
                self.db.query(DocumentCategory)
                .filter(
                    DocumentCategory.working_copy_id == working_copy.id,
                    DocumentCategory.document_version_id
                    == working_copy.current_version_id,
                    DocumentCategory.relation_role == "PRIMARY",
                    DocumentCategory.status.in_(["AUTO_APPLIED", "CONFIRMED"]),
                )
                .order_by(DocumentCategory.created_at.asc())
                .all()
            )
            previous = active_primary[0] if len(active_primary) == 1 else None
            if len(active_primary) > 1:
                reason_codes.append("MULTIPLE_ACTIVE_PRIMARY_CATEGORIES")
            if (
                previous is not None
                and previous.status == "CONFIRMED"
                and previous.category_id != primary_suggestion.category_id
            ):
                reason_codes.append("CONFIRMED_CATEGORY_PROTECTED")

            target_relative_path: str | None = None
            working_root = self.db.get(
                WorkingCopyRoot, working_copy.working_copy_root_id
            )
            if working_root is None:
                reason_codes.append("WORKING_COPY_ROOT_MISSING")
            elif not reason_codes:
                try:
                    target = CategoryOrganizationPathResolver(
                        self.storage
                    ).resolve_category(
                        category_id=primary_suggestion.category_id,
                        taxonomy_key=classification_run.taxonomy_key,
                        taxonomy_version=classification_run.taxonomy_version,
                        working_copy=working_copy,
                        working_root=working_root,
                    )
                    target_relative_path = target.target_relative_path
                except CategoryOrganizationPathError:
                    reason_codes.append("TARGET_PATH_UNAVAILABLE")

            if reason_codes:
                repository.create_or_update_decision(
                    working_copy=working_copy,
                    classification_run=classification_run,
                    primary_suggestion=primary_suggestion,
                    policy_result=policy_result,
                    policy_version=settings.auto_classification_policy_version,
                    calibration_version=(
                        settings.auto_classification_calibration_version
                    ),
                    decision="NEEDS_REVIEW",
                    reason_codes=reason_codes,
                    shadow_only=True,
                    decision_scope=f"reclassification:{agent_run_id}",
                )
                details.append(
                    {
                        "document_id": working_copy.document_id,
                        "filename": working_copy.filename,
                        "status": "NEEDS_REVIEW",
                        "reason_codes": reason_codes,
                    }
                )
                continue

            if (
                previous is not None
                and previous.category_id == primary_suggestion.category_id
            ):
                repository.create_or_update_decision(
                    working_copy=working_copy,
                    classification_run=classification_run,
                    primary_suggestion=primary_suggestion,
                    policy_result=policy_result,
                    policy_version=settings.auto_classification_policy_version,
                    calibration_version=(
                        settings.auto_classification_calibration_version
                    ),
                    decision="UNCHANGED",
                    reason_codes=[],
                    target_relative_path=target_relative_path,
                    decision_scope=f"reclassification:{agent_run_id}",
                )
                details.append(
                    {
                        "document_id": working_copy.document_id,
                        "filename": working_copy.filename,
                        "status": "UNCHANGED",
                        "category_id": primary_suggestion.category_id,
                    }
                )
                continue

            new_relation, ended_relations = repository.replace_auto_applied_primary(
                working_copy=working_copy,
                classification_run=classification_run,
                suggestion=primary_suggestion,
            )
            self._audit_auto_reclassification(
                user=user,
                agent_run_id=agent_run_id,
                new_relation=new_relation,
                ended_relations=ended_relations,
                confidence=policy_result.calibrated_confidence,
            )
            repository.create_or_update_decision(
                working_copy=working_copy,
                classification_run=classification_run,
                primary_suggestion=primary_suggestion,
                policy_result=policy_result,
                policy_version=settings.auto_classification_policy_version,
                calibration_version=settings.auto_classification_calibration_version,
                decision=(
                    "DIRECT_MOVE_AUTHORIZED"
                    if target_relative_path != working_copy.relative_path
                    else "AUTO_RECLASSIFIED"
                ),
                reason_codes=[],
                target_relative_path=target_relative_path,
                decision_scope=f"reclassification:{agent_run_id}",
            )
            if target_relative_path != working_copy.relative_path:
                changed_document_ids.append(working_copy.document_id)
                item_status = "MOVE_REQUIRED"
            else:
                item_status = "AUTO_RECLASSIFIED"
            details.append(
                {
                    "document_id": working_copy.document_id,
                    "filename": working_copy.filename,
                    "status": item_status,
                    "previous_category_id": (
                        previous.category_id if previous is not None else None
                    ),
                    "category_id": primary_suggestion.category_id,
                    "target_relative_path": target_relative_path,
                }
            )

        if not changed_document_ids:
            return {
                "ok": True,
                "kind": "auto_reclassification_no_move",
                "status": "COMPLETED",
                "item_count": 0,
                "suggestions": details,
                "message": "重新分类已完成，没有需要移动的文件。",
            }
        return self._create_category_move_plan(
            user=user,
            document_ids=changed_document_ids,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            reason=(
                "重新分类结果达到自动分类标准且与原分类不同；"
                "已按本次重新分类指令直接移动共享工作副本"
            ),
        )

    @staticmethod
    def _policy_category(suggestion: DocumentCategorySuggestion) -> dict[str, Any]:
        """把持久化建议还原为自动分类门槛需要的有限字段。"""

        candidate_scores = dict(suggestion.candidate_scores_json or {})
        return {
            "name": suggestion.category_name,
            "category_id": suggestion.category_id,
            "category_path": list(suggestion.category_path_json or []),
            "confidence": suggestion.confidence,
            "status": suggestion.status,
            "source": suggestion.source,
            "taxonomy_key": suggestion.taxonomy_key,
            "taxonomy_version": suggestion.taxonomy_version,
            "evidence_items": list(suggestion.evidence_json or []),
            "candidate_scores": candidate_scores,
            "matched_content_signals": list(
                candidate_scores.get("matched_content_signals") or []
            ),
            "negative_signals": list(
                candidate_scores.get("negative_signals") or []
            ),
        }

    def _audit_auto_reclassification(
        self,
        *,
        user: User,
        agent_run_id: str,
        new_relation: DocumentCategory,
        ended_relations: list[DocumentCategory],
        confidence: float | None,
    ) -> None:
        """把自动正式分类替换追加到本次 AgentRun 的 ChangeSet。"""

        run = self.db.get(AgentRun, agent_run_id)
        if run is None:
            raise ValueError("重新分类运行不存在。")
        changeset = self.db.get(ChangeSet, run.changeset_id) if run.changeset_id else None
        if changeset is None:
            changeset = ChangeSet(
                workspace_id=get_shared_workspace_id(self.db),
                conversation_id=run.conversation_id,
                agent_run_id=run.id,
                user_id=user.id,
                status="COMPLETED",
                summary="重新分类达到自动标准，已更新正式分类并按当前指令直接归位。",
            )
            self.db.add(changeset)
            self.db.flush()
            run.changeset_id = changeset.id
        for relation in ended_relations:
            self.db.add(
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="DOCUMENT_CATEGORY",
                    target_id=relation.id,
                    target_document_id=relation.document_id,
                    change_type="CATEGORY_REMOVED",
                    before_value_json={
                        "category_id": relation.category_id,
                        "status": "AUTO_APPLIED",
                    },
                    after_value_json={"status": "ENDED"},
                    source="auto-reclassification-policy",
                    confidence=float(confidence or 0.0),
                    evidence_json={},
                    execution_status="COMPLETED",
                )
            )
        self.db.add(
            ChangeItem(
                changeset_id=changeset.id,
                target_type="DOCUMENT_CATEGORY",
                target_id=new_relation.id,
                target_document_id=new_relation.document_id,
                change_type="CATEGORY_ADDED",
                before_value_json={},
                after_value_json={
                    "category_id": new_relation.category_id,
                    "status": new_relation.status,
                },
                source="auto-reclassification-policy",
                confidence=float(confidence or 0.0),
                evidence_json={
                    "source_suggestion_id": new_relation.source_suggestion_id
                },
                execution_status="COMPLETED",
            )
        )

    def _cancel_conflict(
        self,
        *,
        user: User,
        message: str,
        conversation_id: str,
    ) -> str:
        """取消当前唯一同名冲突，不创建计划也不改变文件。"""

        reviews = (
            self.db.query(FileRenameReviewItem)
            .filter(
                FileRenameReviewItem.user_id == user.id,
                FileRenameReviewItem.conversation_id == conversation_id,
                FileRenameReviewItem.status == "NEEDS_REVIEW",
            )
            .order_by(FileRenameReviewItem.created_at.desc())
            .with_for_update()
            .all()
        )
        reviews = [
            item
            for item in reviews
            if item.review_context_json.get("reason") == "FILENAME_CONFLICT"
        ]
        matched = [
            item
            for item in reviews
            if item.original_filename in message
            or str(item.review_context_json.get("target_filename") or "") in message
        ]
        if len(matched) == 1:
            review = matched[0]
        elif len(reviews) == 1:
            review = reviews[0]
        else:
            raise ValueError("当前没有唯一可取消的同名冲突，请写出具体文件名。")
        review.status = "CANCELLED"
        review.decision_json = {"action": "CANCEL"}
        review.updated_at = utcnow()
        self.db.flush()
        return "已取消本次同名文件处理，现有文件均未改变。"

    def _create_trash_plan(
        self,
        *,
        user: User,
        document_ids: list[str],
        conversation_id: str,
        agent_run_id: str,
    ):
        """把后端确定的附件解析为活动工作副本，并只创建回收站计划。"""

        copies = self._resolve_working_copies(document_ids=document_ids, workspace_id=get_shared_workspace_id(self.db))
        if not copies:
            raise ValueError("请明确选择要移入回收站的当前会话文件。")
        already_trashed = [
            item.filename for item in copies if item.status == "TRASHED"
        ]
        if already_trashed:
            raise ValueError(
                f"以下文件已经在回收站中，无需再次删除：{'、'.join(already_trashed)}。"
                "如需继续使用，请先恢复文件。"
            )
        unavailable = [
            item.filename
            for item in copies
            if item.status not in {"ACTIVE", "TRASHED"}
        ]
        if unavailable:
            raise ValueError(
                f"以下文件仍在后台处理中，暂时不能移入回收站：{'、'.join(unavailable)}。"
                "请等待文件处理完成后重试。"
            )
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

    def _create_category_move_plan(
        self,
        *,
        user: User,
        document_ids: list[str],
        conversation_id: str,
        agent_run_id: str,
        reason: str = "按当前明确分类指令直接整理共享工作副本位置",
    ):
        """按正式分类的受控 organization_path 创建共享移动计划。"""

        shared_workspace_id = get_shared_workspace_id(self.db)
        copies = self._resolve_working_copies(
            document_ids=document_ids,
            workspace_id=shared_workspace_id,
        )
        if not copies:
            raise ValueError("请明确选择要按分类整理的共享文件。")
        path_resolver = CategoryOrganizationPathResolver(self.storage)
        items: list[OperationPlanItem] = []
        for working_copy in copies:
            if working_copy.status != "ACTIVE" or not working_copy.current_version_id:
                raise ValueError(
                    f"文件“{working_copy.filename}”仍在处理或已经删除，不能整理。"
                )
            relations = (
                self.db.query(DocumentCategory)
                .filter(
                    DocumentCategory.working_copy_id == working_copy.id,
                    DocumentCategory.document_version_id
                    == working_copy.current_version_id,
                    DocumentCategory.status.in_(["AUTO_APPLIED", "CONFIRMED"]),
                    DocumentCategory.relation_role != "DOCUMENT_TYPE",
                )
                .order_by(
                    DocumentCategory.relation_role.asc(),
                    DocumentCategory.created_at.asc(),
                )
                .all()
            )
            primary = [
                relation
                for relation in relations
                if relation.relation_role == "PRIMARY"
            ]
            if len(primary) != 1:
                if not primary:
                    raise ValueError(
                        f"文件“{working_copy.filename}”尚未确认唯一主分类，"
                        "请先在分类卡中确认一个主分类。"
                    )
                raise ValueError(
                    f"文件“{working_copy.filename}”存在多个主分类，请先选择一个整理目标。"
                )
            working_root = self.db.get(
                WorkingCopyRoot, working_copy.working_copy_root_id
            )
            if working_root is None:
                raise ValueError(
                    f"文件“{working_copy.filename}”缺少共享工作目录配置。"
                )
            candidates: list[tuple[DocumentCategory, Any]] = []
            for relation in primary:
                try:
                    candidates.append(
                        (
                            relation,
                            path_resolver.resolve(
                                relation=relation,
                                working_copy=working_copy,
                                working_root=working_root,
                            ),
                        )
                    )
                except CategoryOrganizationPathError:
                    continue
            if len(candidates) != 1:
                if not candidates:
                    raise ValueError(
                        f"文件“{working_copy.filename}”没有可用于整理的已确认分类。"
                    )
                raise ValueError(
                    f"文件“{working_copy.filename}”有多个可整理分类，请先选择主分类。"
                )
            relation, target = candidates[0]
            if target.target_relative_path == working_copy.relative_path:
                continue
            occupied = (
                self.db.query(WorkingCopy)
                .filter(
                    WorkingCopy.working_copy_root_id
                    == working_copy.working_copy_root_id,
                    WorkingCopy.relative_path == target.target_relative_path,
                    WorkingCopy.status == "ACTIVE",
                    WorkingCopy.id != working_copy.id,
                )
                .all()
            )
            if occupied:
                existing = occupied[0]
                review = (
                    self.db.query(FileRenameReviewItem)
                    .filter(
                        FileRenameReviewItem.agent_run_id == agent_run_id,
                        FileRenameReviewItem.managed_file_id
                        == working_copy.managed_file_id,
                    )
                    .one_or_none()
                )
                if review is None:
                    review = FileRenameReviewItem(
                        conversation_id=conversation_id,
                        agent_run_id=agent_run_id,
                        user_id=user.id,
                        managed_file_id=working_copy.managed_file_id,
                        document_id=working_copy.document_id,
                        root_key=working_root.root_key,
                        original_relative_path=working_copy.relative_path,
                        original_filename=working_copy.filename,
                        source_sha256=working_copy.content_sha256,
                        status="NEEDS_REVIEW",
                        review_context_json={
                            "reason": "FILENAME_CONFLICT",
                            "working_copy_id": working_copy.id,
                            "existing_working_copy_ids": [existing.id],
                            "filename": working_copy.filename,
                            "target_filename": working_copy.filename,
                            "target_relative_path": target.target_relative_path,
                            "source": "MOVE_BY_CONFIRMED_CATEGORY",
                        },
                    )
                    self.db.add(review)
                    self.db.flush()
                raise WorkingCopyConflictPending(filename=working_copy.filename)
            items.append(
                OperationPlanItem(
                    working_copy_id=working_copy.id,
                    after={"relative_path": target.target_relative_path},
                    rename_metadata={
                        "document_category_id": relation.id,
                        "category_id": relation.category_id,
                        "taxonomy_key": relation.taxonomy_key,
                        "taxonomy_version": relation.taxonomy_version,
                        "organization_path": list(target.organization_path),
                        "working_copy_root_id": working_copy.working_copy_root_id,
                        "shared_impact": True,
                    },
                )
            )
        if not items:
            return {
                "ok": True,
                "kind": "classification_move_not_required",
                "status": "COMPLETED",
                "item_count": 0,
                "file_position_changed": False,
                "message": "分类已保存，文件已经位于对应分类目录。",
            }
        plan = self.operations.create_plan(
            current_user=user,
            request=OperationPlanCreateRequest(
                conversation_id=conversation_id,
                operation_type="MOVE_WORKING_COPIES",
                risk_level="high",
                reason=reason,
                items=items,
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

        shared_workspace_id = get_shared_workspace_id(self.db)
        copies = self._resolve_working_copies(document_ids=document_ids, workspace_id=shared_workspace_id)
        trashed = [item for item in copies if item.status == "TRASHED"]
        if len(trashed) != 1:
            raise ValueError("请在当前对话中明确指定一个已移入回收站的文件。")
        entries = (
            self.db.query(TrashEntry)
            .filter(
                TrashEntry.workspace_id == shared_workspace_id,
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
            .with_for_update()
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
        existing_ids = [str(value) for value in context.get("existing_working_copy_ids", []) if value]
        pending_id = str(context.get("working_copy_id") or "")
        locked_ids = sorted({pending_id, *(existing_ids if len(existing_ids) == 1 else [])})
        locked_copies = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.id.in_(locked_ids))
            .order_by(WorkingCopy.id.asc())
            .with_for_update()
            .all()
            if pending_id and len(existing_ids) == 1
            else []
        )
        copies_by_id = {item.id: item for item in locked_copies}
        pending_copy = copies_by_id.get(pending_id)
        existing_copy = copies_by_id.get(existing_ids[0]) if len(existing_ids) == 1 else None
        if pending_copy is None or existing_copy is None:
            raise ValueError("同名冲突记录已失效，请重新整理文件。")
        if (
            pending_copy.id == existing_copy.id
            or pending_copy.content_sha256 != review.source_sha256
        ):
            raise ValueError("同名冲突文件状态已经变化，请重新整理文件。")
        target_filename = Path(str(context.get("target_filename") or "")).name
        if not target_filename or target_filename in {".", ".."}:
            raise ValueError("同名冲突缺少有效目标文件名。")
        recorded_target = str(context.get("target_relative_path") or "")
        target_parent = (
            PurePosixPath(existing_copy.relative_path).parent
            if pending_copy.working_copy_root_id == existing_copy.working_copy_root_id
            else (
                PurePosixPath(recorded_target).parent
                if recorded_target
                else PurePosixPath(pending_copy.relative_path).parent
            )
        )
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
        """把会话已确定的文档 ID 解析为共享工作副本，保持输入顺序去重。

        调用方已由 AgentRun 与会话附件上下文约束，不能再用 Document.user_id 排除
        共享资料；否则不同用户会看到同一文件却无法在对话中创建确认计划。
        """

        if not document_ids:
            return []
        resolver = CanonicalWorkingFileResolver(self.db)
        resolved: list[WorkingCopy] = []
        for document_id in document_ids:
            # 共享工作副本 Document 对全部用户可用；如果传入的是上传暂存 Document，
            # 仍必须属于当前用户，避免借规范映射读取其他用户的私有上传来源。
            direct = (
                self.db.query(WorkingCopy)
                .filter(
                    WorkingCopy.document_id == document_id,
                    WorkingCopy.workspace_id == workspace_id,
                )
                .one_or_none()
            )
            if direct is None:
                source_document = self.db.get(Document, document_id)
                if source_document is None or source_document.user_id != self.user_id:
                    raise ValueError("部分文件不存在或不属于当前用户。")
            try:
                canonical = resolver.resolve_document(
                    document_id=document_id,
                    allow_trashed=True,
                )
            except CanonicalWorkingFileError as exc:
                raise ValueError(str(exc)) from exc
            resolved.append(canonical.working_copy)
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
            indexed = self.operations.find_active_filename_conflicts(
                workspace_id=pending_copy.workspace_id,
                target_filename=filename,
                exclude_working_copy_ids={pending_copy.id},
            )
            physical = self.storage.working_copy_path(f"{root.relative_storage_path}/{relative_path}")
            if not indexed and not physical.exists():
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
