"""正式分类决定的统一事务服务。

按钮反馈、自然语言纠正和后续管理端人工决定都必须调用本服务。服务只接受
规范共享工作副本和 ACTIVE taxonomy 节点，并在同一 PostgreSQL 事务中写入
反馈、正式关系、确认来源、ChangeSet 和 Neo4j Outbox。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.db.models import (
    AgentRun,
    ChangeItem,
    ChangeSet,
    ClassificationGraphOutbox,
    DocumentCategory,
    DocumentCategoryConfirmationSource,
    DocumentCategoryFeedback,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    User,
    WorkingCopy,
    utcnow,
)
from app.modules.classification.feedback_schemas import (
    ClassificationFeedbackRequest,
    ClassificationFeedbackResponse,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.schemas import CategoryNode
from app.modules.file_lifecycle.shared_access import (
    CanonicalWorkingFileError,
    CanonicalWorkingFileResolver,
)
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


@dataclass(frozen=True)
class _TaxonomyTarget:
    """当前 taxonomy 中经过后端验证的稳定分类节点。"""

    category_id: str
    category_path: list[str]


class ClassificationDecisionService:
    """原子执行 ACCEPT、REJECT 和 CORRECT 分类决定。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db
        self.resolver = CanonicalWorkingFileResolver(db)

    def decide(
        self,
        *,
        suggestion_id: str,
        request: ClassificationFeedbackRequest,
        current_user: User,
    ) -> ClassificationFeedbackResponse:
        """执行一次幂等、可审计的正式分类决定。"""

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

        run = self._resolve_actor_run(
            suggestion=suggestion,
            requested_agent_run_id=request.agent_run_id,
            current_user=current_user,
        )
        try:
            canonical = self.resolver.resolve_suggestion(suggestion)
        except CanonicalWorkingFileError as exc:
            status_code = 409 if exc.code != "WORKING_COPY_NOT_FOUND" else 404
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        # PostgreSQL 下锁定规范工作副本，使同一文件的并发接受/纠正串行化。
        # 否则两个请求可能同时观察不到正式关系，最后由唯一索引抛出未处理异常。
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
            raise HTTPException(
                status_code=409,
                detail="文件状态已经变化，请重新读取并确认分类。",
            )

        original_target = self._resolve_taxonomy_target(
            category_id=suggestion.category_id,
            category_path=list(suggestion.category_path_json or []),
        )
        corrected_target = (
            self._resolve_taxonomy_target(
                category_id=request.corrected_category_id,
                category_path=request.corrected_category_path,
            )
            if request.action == "CORRECT"
            else None
        )
        relation_role = request.relation_role
        idempotency_key = self._idempotency_key(
            user_id=current_user.id,
            agent_run_id=run.id,
            working_copy_id=canonical.working_copy.id,
            suggestion_id=suggestion.id,
            action=request.action,
            corrected_category_id=(
                corrected_target.category_id if corrected_target else None
            ),
            relation_role=relation_role,
            client_key=request.idempotency_key,
        )
        existing = (
            self.db.query(DocumentCategoryFeedback)
            .filter(DocumentCategoryFeedback.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            return self._response(
                feedback=existing,
                suggestion=suggestion,
                changeset_id=run.changeset_id,
            )

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

        action = {
            "ACCEPT": "ACCEPTED",
            "REJECT": "REJECTED",
            "CORRECT": "CORRECTED",
        }[request.action]
        feedback = DocumentCategoryFeedback(
            suggestion_id=suggestion.id,
            document_id=canonical.working_copy.document_id,
            document_version_id=canonical.document_version.id,
            working_copy_id=canonical.working_copy.id,
            user_id=current_user.id,
            action=action,
            corrected_category_id=(
                corrected_target.category_id if corrected_target else None
            ),
            corrected_category_path_json=(
                corrected_target.category_path if corrected_target else []
            ),
            supersedes_feedback_id=(
                previous_feedback.id if previous_feedback is not None else None
            ),
            is_active=True,
            idempotency_key=idempotency_key,
            comment=request.comment,
        )
        self.db.add(feedback)
        self.db.flush()

        changeset = self._get_or_create_changeset(
            run=run,
            current_user=current_user,
            summary="已记录文件分类决定，文件位置未改变。",
        )
        changed_relations: list[DocumentCategory] = []
        if request.action == "ACCEPT":
            relation, created = self._confirm_relation(
                suggestion=suggestion,
                target=original_target,
                relation_role=relation_role,
                feedback=feedback,
                current_user=current_user,
                canonical=canonical,
            )
            changed_relations.append(relation)
            self._write_change_item(
                changeset=changeset,
                relation=relation,
                change_type="CATEGORY_ADDED" if created else "CATEGORY_CONFIRMED",
                before_value={},
                after_value={"status": "CONFIRMED"},
            )
        elif request.action == "REJECT":
            ended = self._withdraw_user_sources(
                working_copy_id=canonical.working_copy.id,
                document_version_id=canonical.document_version.id,
                category_id=original_target.category_id,
                user_id=current_user.id,
            )
            for relation in ended:
                changed_relations.append(relation)
                self._write_change_item(
                    changeset=changeset,
                    relation=relation,
                    change_type="CATEGORY_REMOVED",
                    before_value={"status": "CONFIRMED"},
                    after_value={"status": relation.status},
                )
        else:
            ended = self._withdraw_user_sources(
                working_copy_id=canonical.working_copy.id,
                document_version_id=canonical.document_version.id,
                category_id=original_target.category_id,
                user_id=current_user.id,
            )
            changed_relations.extend(ended)
            relation, _created = self._confirm_relation(
                suggestion=suggestion,
                target=corrected_target,
                relation_role=relation_role,
                feedback=feedback,
                current_user=current_user,
                canonical=canonical,
            )
            changed_relations.append(relation)
            self._write_change_item(
                changeset=changeset,
                relation=relation,
                change_type="CATEGORY_CORRECTED",
                before_value={
                    "category_id": original_target.category_id,
                    "category_path": original_target.category_path,
                },
                after_value={
                    "category_id": corrected_target.category_id,
                    "category_path": corrected_target.category_path,
                    "status": "CONFIRMED",
                },
            )

        for relation in {item.id: item for item in changed_relations}.values():
            self._enqueue_graph_projection(relation=relation, feedback=feedback)
        self.db.flush()
        return self._response(
            feedback=feedback,
            suggestion=suggestion,
            changeset_id=changeset.id,
        )

    def _resolve_actor_run(
        self,
        *,
        suggestion: DocumentCategorySuggestion,
        requested_agent_run_id: str | None,
        current_user: User,
    ) -> AgentRun:
        """取得当前用户的审计 AgentRun，不能借用其他用户的会话。"""

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
            raise HTTPException(
                status_code=422,
                detail="请在当前对话中重新选择该共享文件后再确认分类。",
            )
        return row[1]

    def _resolve_taxonomy_target(
        self,
        *,
        category_id: str | None,
        category_path: list[str],
    ) -> _TaxonomyTarget:
        """只允许当前默认 taxonomy 中的稳定节点成为正式分类。"""

        by_id, by_path = _taxonomy_indexes()
        normalized_id = str(category_id or "").strip()
        normalized_path = [
            str(item).strip() for item in category_path if str(item).strip()
        ]
        if normalized_id:
            known = by_id.get(normalized_id)
            if known is None:
                raise HTTPException(status_code=422, detail="Unknown category id")
            return _TaxonomyTarget(normalized_id, known)
        resolved_id = by_path.get(tuple(normalized_path))
        if resolved_id is None:
            raise HTTPException(status_code=422, detail="Unknown category path")
        return _TaxonomyTarget(resolved_id, normalized_path)

    def _confirm_relation(
        self,
        *,
        suggestion: DocumentCategorySuggestion,
        target: _TaxonomyTarget | None,
        relation_role: str,
        feedback: DocumentCategoryFeedback,
        current_user: User,
        canonical: Any,
    ) -> tuple[DocumentCategory, bool]:
        """创建或复用正式关系，并增加当前用户的确认来源。"""

        if target is None:
            raise HTTPException(status_code=422, detail="Missing corrected category")
        if relation_role == "PRIMARY":
            conflicting = (
                self.db.query(DocumentCategory)
                .filter(
                    DocumentCategory.working_copy_id == canonical.working_copy.id,
                    DocumentCategory.document_version_id == canonical.document_version.id,
                    DocumentCategory.relation_role == "PRIMARY",
                    DocumentCategory.status.in_(["AUTO_APPLIED", "CONFIRMED"]),
                    DocumentCategory.category_id != target.category_id,
                )
                .with_for_update()
                .first()
            )
            if conflicting is not None and conflicting.status == "CONFIRMED":
                raise HTTPException(
                    status_code=409,
                    detail="该文件已有其他人工确认的主分类，请先选择要作为整理目录的分类。",
                )
            if conflicting is not None:
                # 用户明确更正可以结束系统自动主分类事实；本事务只写分类事实，
                # 对话应用服务随后根据同一条显式指令创建并直接执行受控 MOVE 计划。
                conflicting.status = "REJECTED"
                conflicting.ended_at = utcnow()
        relation = (
            self.db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id == canonical.working_copy.id,
                DocumentCategory.document_version_id == canonical.document_version.id,
                DocumentCategory.category_id == target.category_id,
                DocumentCategory.relation_role == relation_role,
                DocumentCategory.status.in_(["AUTO_APPLIED", "CONFIRMED"]),
            )
            .with_for_update()
            .one_or_none()
        )
        created = relation is None
        if relation is None:
            taxonomy = load_default_taxonomy()
            classification_run = self.db.get(
                DocumentClassificationRun, suggestion.classification_run_id
            )
            relation = DocumentCategory(
                working_copy_id=canonical.working_copy.id,
                document_id=canonical.working_copy.document_id,
                document_version_id=canonical.document_version.id,
                category_id=target.category_id,
                category_path_json=target.category_path,
                relation_role=relation_role,
                status="CONFIRMED",
                taxonomy_key=taxonomy.key,
                taxonomy_version=taxonomy.version,
                classifier_version=(
                    classification_run.classifier_version
                    if classification_run is not None
                    else ""
                ),
                source="user_confirmed",
                source_suggestion_id=suggestion.id,
                evidence_json=list(suggestion.evidence_json or []),
            )
            self.db.add(relation)
            self.db.flush()
        elif relation.status == "AUTO_APPLIED":
            relation.status = "CONFIRMED"
            relation.source = "user_confirmed"
            relation.updated_at = utcnow()
        existing_source = (
            self.db.query(DocumentCategoryConfirmationSource)
            .filter(
                DocumentCategoryConfirmationSource.document_category_id == relation.id,
                DocumentCategoryConfirmationSource.user_id == current_user.id,
                DocumentCategoryConfirmationSource.status == "ACTIVE",
            )
            .one_or_none()
        )
        if existing_source is None:
            self.db.add(
                DocumentCategoryConfirmationSource(
                    document_category_id=relation.id,
                    user_id=current_user.id,
                    feedback_id=feedback.id,
                    suggestion_id=suggestion.id,
                    status="ACTIVE",
                )
            )
            self.db.flush()
        else:
            # 同一用户后续用新的 AgentRun 再次确认时，正式关系仍保持单条，
            # 但有效来源必须指向最新反馈，避免审计链停留在已被 supersede 的记录上。
            existing_source.feedback_id = feedback.id
            existing_source.suggestion_id = suggestion.id
        return relation, created

    def _withdraw_user_sources(
        self,
        *,
        working_copy_id: str,
        document_version_id: str,
        category_id: str,
        user_id: str,
    ) -> list[DocumentCategory]:
        """结束当前用户对分类的来源，保留其他用户仍有效的关系。"""

        relations = (
            self.db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id == working_copy_id,
                DocumentCategory.document_version_id == document_version_id,
                DocumentCategory.category_id == category_id,
                DocumentCategory.status.in_(["AUTO_APPLIED", "CONFIRMED"]),
            )
            .with_for_update()
            .all()
        )
        changed: list[DocumentCategory] = []
        now = utcnow()
        for relation in relations:
            if relation.status == "AUTO_APPLIED":
                relation.status = "REJECTED"
                relation.ended_at = now
                relation.updated_at = now
                changed.append(relation)
                continue
            sources = (
                self.db.query(DocumentCategoryConfirmationSource)
                .filter(
                    DocumentCategoryConfirmationSource.document_category_id
                    == relation.id,
                    DocumentCategoryConfirmationSource.status == "ACTIVE",
                )
                .with_for_update()
                .all()
            )
            user_sources = [item for item in sources if item.user_id == user_id]
            if not user_sources:
                continue
            for source in user_sources:
                source.status = "WITHDRAWN"
                source.ended_at = now
            if not any(item.user_id != user_id for item in sources):
                relation.status = "ENDED"
                relation.ended_at = now
            relation.updated_at = now
            changed.append(relation)
        return changed

    def _get_or_create_changeset(
        self,
        *,
        run: AgentRun,
        current_user: User,
        summary: str,
    ) -> ChangeSet:
        """复用当前 AgentRun 的变更集，不删除原有文件处理审计。"""

        changeset = (
            self.db.get(ChangeSet, run.changeset_id) if run.changeset_id else None
        )
        if changeset is None:
            changeset = (
                self.db.query(ChangeSet)
                .filter(ChangeSet.agent_run_id == run.id)
                .order_by(ChangeSet.created_at.asc())
                .first()
            )
        if changeset is None:
            changeset = ChangeSet(
                workspace_id=get_shared_workspace_id(self.db),
                conversation_id=run.conversation_id,
                agent_run_id=run.id,
                user_id=current_user.id,
                status="COMPLETED",
                summary=summary,
            )
            self.db.add(changeset)
            self.db.flush()
            run.changeset_id = changeset.id
        else:
            changeset.summary = summary
            changeset.updated_at = utcnow()
        return changeset

    def _write_change_item(
        self,
        *,
        changeset: ChangeSet,
        relation: DocumentCategory,
        change_type: str,
        before_value: dict[str, Any],
        after_value: dict[str, Any],
    ) -> None:
        """写入不含正文和内部路径的正式分类审计。"""

        self.db.add(
            ChangeItem(
                changeset_id=changeset.id,
                target_type="DOCUMENT_CATEGORY",
                target_id=relation.id,
                target_document_id=relation.document_id,
                change_type=change_type,
                before_value_json=before_value,
                after_value_json={
                    **after_value,
                    "category_id": relation.category_id,
                    "category_path": list(relation.category_path_json or []),
                    "relation_role": relation.relation_role,
                },
                source="classification-decision",
                confidence=1.0,
                evidence_json={"source_suggestion_id": relation.source_suggestion_id},
                execution_status="COMPLETED",
            )
        )

    def _enqueue_graph_projection(
        self,
        *,
        relation: DocumentCategory,
        feedback: DocumentCategoryFeedback,
    ) -> None:
        """在主事务内创建幂等 Neo4j 投影待办。"""

        latest_version = (
            self.db.query(func.max(ClassificationGraphOutbox.state_version))
            .filter(
                ClassificationGraphOutbox.document_category_id == relation.id
            )
            .scalar()
            or 0
        )
        state_version = int(latest_version) + 1
        key = f"{relation.id}:{state_version}:{relation.status}:{feedback.id}"
        if (
            self.db.query(ClassificationGraphOutbox)
            .filter(ClassificationGraphOutbox.deduplication_key == key)
            .first()
            is not None
        ):
            return
        self.db.add(
            ClassificationGraphOutbox(
                document_category_id=relation.id,
                working_copy_id=relation.working_copy_id,
                document_version_id=relation.document_version_id,
                expected_status=relation.status,
                state_version=state_version,
                deduplication_key=key,
                status="PENDING",
            )
        )

    @staticmethod
    def _idempotency_key(
        *,
        user_id: str,
        agent_run_id: str,
        working_copy_id: str,
        suggestion_id: str,
        action: str,
        corrected_category_id: str | None,
        relation_role: str,
        client_key: str | None,
    ) -> str:
        """生成不包含正文的稳定幂等键。"""

        raw = "\0".join(
            [
                user_id,
                agent_run_id,
                working_copy_id,
                suggestion_id,
                action,
                corrected_category_id or "",
                relation_role,
                str(client_key or ""),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _response(
        *,
        feedback: DocumentCategoryFeedback,
        suggestion: DocumentCategorySuggestion,
        changeset_id: str | None,
    ) -> ClassificationFeedbackResponse:
        """构造兼容旧前端并补充正式分类状态的响应。"""

        positive, negative = _sample_labels(
            action=feedback.action,
            original_category_id=suggestion.category_id,
            corrected_category_id=feedback.corrected_category_id,
        )
        return ClassificationFeedbackResponse(
            id=feedback.id,
            suggestion_id=suggestion.id,
            document_id=feedback.document_id,
            document_version_id=feedback.document_version_id,
            working_copy_id=feedback.working_copy_id,
            action=feedback.action,
            corrected_category_id=feedback.corrected_category_id,
            corrected_category_path=list(
                feedback.corrected_category_path_json or []
            ),
            positive_category_ids=positive,
            negative_category_ids=negative,
            changeset_id=changeset_id,
            file_position_changed=False,
            user_message="分类决定已保存，文件位置未改变。",
            created_at=feedback.created_at,
        )


def _taxonomy_indexes() -> tuple[dict[str, list[str]], dict[tuple[str, ...], str]]:
    """构建 ACTIVE taxonomy 的稳定 ID 与路径索引。"""

    by_id: dict[str, list[str]] = {}
    by_path: dict[tuple[str, ...], str] = {}

    def walk(node: CategoryNode, parent: list[str]) -> None:
        path = [*parent, node.name]
        if node.id:
            by_id[node.id] = path
            by_path[tuple(path)] = node.id
        for child in node.children:
            walk(child, path)

    taxonomy = load_default_taxonomy()
    for root in taxonomy.categories:
        walk(root, [])
    return by_id, by_path


def _sample_labels(
    *,
    action: str,
    original_category_id: str,
    corrected_category_id: str | None,
) -> tuple[list[str], list[str]]:
    """把明确决定投影为训练评测所需的正负分类 ID。"""

    original = str(original_category_id or "")
    if action == "ACCEPTED":
        return ([original] if original else []), []
    if action == "REJECTED":
        return [], ([original] if original else [])
    return (
        [corrected_category_id] if corrected_category_id else [],
        [original] if original else [],
    )
