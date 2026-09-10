"""把 PRIMARY 分类反馈接入受控异步落位。

本模块只处理用户对已有分类建议的 PRIMARY ``ACCEPT``/``CORRECT``。事务 A
只保存“已收到”的反馈和冻结落位操作；正式 ``DocumentCategory``、确认来源和
物理目录均由 placement worker 的事务 B 一起提交。RELATED、SECONDARY 等辅助
关系仍由既有反馈服务处理，不能因为本模块而意外移动文件。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    ClassificationPlacementOperation,
    DocumentCategoryFeedback,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    User,
    WorkingCopy,
)
from app.modules.classification.feedback_schemas import (
    ClassificationFeedbackRequest,
    ClassificationFeedbackResponse,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.placement_authorization import (
    PlacementAuthorizationService,
)
from app.modules.classification.placement_schemas import (
    PlacementAction,
    PlacementAuthorizationContext,
    PlacementCommand,
)
from app.modules.classification.placement_service import (
    ClassificationPlacementService,
    PlacementServiceError,
)
from app.modules.classification.schemas import CategoryNode
from app.modules.file_lifecycle.shared_access import (
    CanonicalWorkingFileError,
    CanonicalWorkingFileResolver,
)


@dataclass(frozen=True, slots=True)
class _TaxonomyTarget:
    """经当前 taxonomy 验证过的 PRIMARY 目标。"""

    category_id: str
    category_path: list[str]


class PrimaryFeedbackPlacementService:
    """记录 PRIMARY 反馈并向同一落位协调器提交异步操作。

    ``authorization_factory`` 必须由 HTTP、聊天或选择卡入口在服务端构造。客户端
    不能提交 actor、授权模式、物理路径或“跳过确认”标记。
    """

    def __init__(self, db: Session) -> None:
        """保存请求级 session；不得跨用户或跨请求复用。"""

        self.db = db
        self.resolver = CanonicalWorkingFileResolver(db)

    def submit(
        self,
        *,
        suggestion_id: str,
        request: ClassificationFeedbackRequest,
        current_user: User,
        client_id: str,
        request_id: str,
        source_event_ref: str,
        conversation_id: str | None = None,
        authorization_factory: Callable[[PlacementCommand, str], PlacementAuthorizationContext]
        | None = None,
    ) -> ClassificationFeedbackResponse:
        """保存 PENDING 反馈并创建 SET_PRIMARY 操作，受理不代表文件已移动。"""

        if request.relation_role != "PRIMARY" or request.action not in {"ACCEPT", "CORRECT"}:
            raise ValueError("PRIMARY 落位只接受 ACCEPT 或 CORRECT")
        suggestion = self._load_suggestion(suggestion_id)
        run = self._resolve_actor_run(
            suggestion=suggestion,
            requested_agent_run_id=request.agent_run_id,
            current_user=current_user,
        )
        canonical = self._resolve_canonical(suggestion)
        target = self._resolve_target(suggestion=suggestion, request=request)
        idempotency_key = self._feedback_idempotency_key(
            user_id=current_user.id,
            agent_run_id=run.id,
            working_copy_id=canonical.working_copy.id,
            suggestion_id=suggestion.id,
            action=request.action,
            target_category_id=target.category_id,
            client_key=request.idempotency_key,
        )
        existing = (
            self.db.query(DocumentCategoryFeedback)
            .filter(DocumentCategoryFeedback.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            return self._response(feedback=existing, suggestion=suggestion)

        locked_copy = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.id == canonical.working_copy.id)
            .with_for_update()
            .one_or_none()
        )
        if (
            locked_copy is None
            or locked_copy.status != "ACTIVE"
            or locked_copy.current_version_id != canonical.document_version.id
            or locked_copy.content_sha256 != canonical.document_version.sha256
        ):
            raise HTTPException(status_code=409, detail="文件状态已经变化，请重新读取并确认分类。")

        previous_feedback = (
            self.db.query(DocumentCategoryFeedback)
            .filter(
                DocumentCategoryFeedback.suggestion_id == suggestion.id,
                DocumentCategoryFeedback.user_id == current_user.id,
                DocumentCategoryFeedback.is_active.is_(True),
            )
            .order_by(DocumentCategoryFeedback.created_at.desc())
            .with_for_update()
            .first()
        )
        if previous_feedback is not None:
            previous_feedback.is_active = False
        feedback = DocumentCategoryFeedback(
            suggestion_id=suggestion.id,
            document_id=locked_copy.document_id,
            document_version_id=canonical.document_version.id,
            working_copy_id=locked_copy.id,
            user_id=current_user.id,
            action="ACCEPTED" if request.action == "ACCEPT" else "CORRECTED",
            corrected_category_id=(target.category_id if request.action == "CORRECT" else None),
            corrected_category_path_json=(target.category_path if request.action == "CORRECT" else []),
            supersedes_feedback_id=(previous_feedback.id if previous_feedback is not None else None),
            is_active=True,
            idempotency_key=idempotency_key,
            comment=request.comment,
            application_status="PENDING",
        )
        self.db.add(feedback)
        self.db.flush()

        command = PlacementCommand(
            working_copy_id=locked_copy.id,
            action=PlacementAction.SET_PRIMARY,
            expected_revision=locked_copy.revision,
            expected_document_version_id=canonical.document_version.id,
            target_category_id=target.category_id,
            taxonomy_version=load_default_taxonomy().version,
            idempotency_key=f"feedback:{feedback.id}",
        )
        authorization = (
            authorization_factory(command, locked_copy.workspace_id)
            if authorization_factory is not None
            else PlacementAuthorizationService(self.db).authorize_structured_request(
                command=command,
                current_user=current_user,
                workspace_id=locked_copy.workspace_id,
                client_id=client_id,
                request_id=request_id,
                source_event_ref=source_event_ref,
                conversation_id=conversation_id or run.conversation_id,
            )
        )
        try:
            submission = ClassificationPlacementService(self.db).submit(
                command=command,
                authorization=authorization,
                feedback_context={
                    "feedback_id": feedback.id,
                    "suggestion_id": suggestion.id,
                    "action": feedback.action,
                    "relation_role": "PRIMARY",
                    "target_category_id": target.category_id,
                },
            )
        except PlacementServiceError:
            # 当前外层请求会回滚本次反馈，避免把参数/冲突错误伪装成“已收到”。
            raise
        feedback.placement_operation_id = submission.operation_id
        return self._response(
            feedback=feedback,
            suggestion=suggestion,
            placement_status=submission.placement_status,
        )

    def _load_suggestion(self, suggestion_id: str) -> DocumentCategorySuggestion:
        """锁定仍可反馈的建议，拒绝历史或失效候选。"""

        suggestion = (
            self.db.query(DocumentCategorySuggestion)
            .filter(DocumentCategorySuggestion.id == suggestion_id)
            .with_for_update()
            .one_or_none()
        )
        if suggestion is None:
            raise HTTPException(status_code=404, detail="Classification suggestion not found")
        if suggestion.status not in {"SUGGESTED", "NEEDS_REVIEW", "AUTO_APPLIED", "CONFIRMED"}:
            raise HTTPException(status_code=409, detail="Classification suggestion is no longer active")
        return suggestion

    def _resolve_actor_run(
        self,
        *,
        suggestion: DocumentCategorySuggestion,
        requested_agent_run_id: str | None,
        current_user: User,
    ) -> AgentRun:
        """只使用当前用户的真实 AgentRun，不能借用其他用户会话。"""

        if requested_agent_run_id:
            run = self.db.get(AgentRun, requested_agent_run_id)
            if run is None or run.user_id != current_user.id:
                raise HTTPException(status_code=404, detail="Agent run not found")
            return run
        row = (
            self.db.query(DocumentClassificationRun, AgentRun)
            .join(AgentRun, AgentRun.id == DocumentClassificationRun.agent_run_id)
            .filter(
                DocumentClassificationRun.id == suggestion.classification_run_id,
                AgentRun.user_id == current_user.id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=422, detail="请在当前对话中重新选择该共享文件后再确认分类。")
        return row[1]

    def _resolve_canonical(self, suggestion: DocumentCategorySuggestion):
        """将建议绑定到唯一活动工作副本和当前内容版本。"""

        try:
            return self.resolver.resolve_suggestion(suggestion)
        except CanonicalWorkingFileError as exc:
            status_code = 409 if exc.code != "WORKING_COPY_NOT_FOUND" else 404
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    def _resolve_target(
        self,
        *,
        suggestion: DocumentCategorySuggestion,
        request: ClassificationFeedbackRequest,
    ) -> _TaxonomyTarget:
        """解析建议原目标或用户明确更正的稳定 taxonomy ID。"""

        category_id = (
            request.corrected_category_id if request.action == "CORRECT" else suggestion.category_id
        )
        category_path = (
            list(request.corrected_category_path or [])
            if request.action == "CORRECT"
            else list(suggestion.category_path_json or [])
        )
        by_id, by_path = _taxonomy_indexes()
        normalized_id = str(category_id or "").strip()
        if normalized_id:
            resolved_path = by_id.get(normalized_id)
            if resolved_path is None:
                raise HTTPException(status_code=422, detail="Unknown category id")
            return _TaxonomyTarget(category_id=normalized_id, category_path=resolved_path)
        normalized_path = tuple(str(item).strip() for item in category_path if str(item).strip())
        resolved_id = by_path.get(normalized_path)
        if resolved_id is None:
            raise HTTPException(status_code=422, detail="Unknown category path")
        return _TaxonomyTarget(category_id=resolved_id, category_path=list(normalized_path))

    @staticmethod
    def _feedback_idempotency_key(
        *,
        user_id: str,
        agent_run_id: str,
        working_copy_id: str,
        suggestion_id: str,
        action: str,
        target_category_id: str,
        client_key: str | None,
    ) -> str:
        """生成不含正文的反馈幂等键，保证重复点击复用同一操作。"""

        raw = "\0".join(
            [
                user_id,
                agent_run_id,
                working_copy_id,
                suggestion_id,
                action,
                target_category_id,
                str(client_key or ""),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _response(
        self,
        *,
        feedback: DocumentCategoryFeedback,
        suggestion: DocumentCategorySuggestion,
        placement_status: str | None = None,
    ) -> ClassificationFeedbackResponse:
        """返回真实受理状态；绝不把 PENDING 描述为文件已经移动。"""

        positive, negative = _sample_labels(
            action=feedback.action,
            original_category_id=suggestion.category_id,
            corrected_category_id=feedback.corrected_category_id,
        )
        operation_id = feedback.placement_operation_id
        if placement_status is None and operation_id:
            # 重放请求从持久化操作读取真实状态，避免根据反馈字段猜测完成度。
            placement = self.db.get(ClassificationPlacementOperation, operation_id)
            if placement is not None:
                placement_status = str(
                    (placement.result_json or {}).get("placement_status")
                    or {
                        "EXECUTING": "APPLYING",
                        "FS_APPLIED": "RECONCILING",
                        "RECONCILING": "RECONCILING",
                        "FAILED": "ERROR",
                    }.get(placement.state, "PENDING")
                )
        return ClassificationFeedbackResponse(
            id=feedback.id,
            suggestion_id=suggestion.id,
            document_id=feedback.document_id,
            document_version_id=feedback.document_version_id,
            working_copy_id=feedback.working_copy_id,
            action=feedback.action,
            corrected_category_id=feedback.corrected_category_id,
            corrected_category_path=list(feedback.corrected_category_path_json or []),
            positive_category_ids=positive,
            negative_category_ids=negative,
            file_position_changed=False,
            user_message=(
                "已收到主分类确认，正在按受控目录处理文件。"
                if feedback.application_status == "PENDING"
                else "主分类及文件位置已按确认结果完成处理。"
            ),
            application_status=feedback.application_status,
            placement_operation_id=operation_id,
            placement_status=placement_status,
            created_at=feedback.created_at,
        )


def _taxonomy_indexes() -> tuple[dict[str, list[str]], dict[tuple[str, ...], str]]:
    """构造启用 taxonomy 的稳定 ID/显示路径索引。"""

    by_id: dict[str, list[str]] = {}
    by_path: dict[tuple[str, ...], str] = {}

    def walk(node: CategoryNode, parent: list[str]) -> None:
        path = [*parent, node.name]
        if node.id:
            by_id[node.id] = path
            by_path[tuple(path)] = node.id
        for child in node.children:
            walk(child, path)

    for root in load_default_taxonomy().categories:
        walk(root, [])
    return by_id, by_path


def _sample_labels(
    *, action: str, original_category_id: str, corrected_category_id: str | None
) -> tuple[list[str], list[str]]:
    """将真实用户动作投影为评测所需正负标签。"""

    if action == "ACCEPTED":
        return ([original_category_id] if original_category_id else []), []
    return (
        [corrected_category_id] if corrected_category_id else [],
        [original_category_id] if original_category_id else [],
    )
