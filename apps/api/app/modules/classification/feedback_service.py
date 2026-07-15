"""分类反馈的追加写入、版本关联和冷启动统计。"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    DocumentCategoryFeedback,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    User,
)
from app.modules.classification.feedback_schemas import (
    ClassificationFeedbackRequest,
    ClassificationFeedbackResponse,
    ClassificationFeedbackSummaryResponse,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.schemas import CategoryNode


class ClassificationFeedbackService:
    """只接受用户明确操作，不从沉默或打开文件推断标签。"""

    def __init__(self, db: Session, *, evaluation_min_samples: int = 100) -> None:
        self.db = db
        self.evaluation_min_samples = max(1, evaluation_min_samples)

    def record(
        self,
        *,
        suggestion_id: str,
        request: ClassificationFeedbackRequest,
        current_user: User,
    ) -> ClassificationFeedbackResponse:
        """追加一条反馈，并停用同一用户对该建议的上一条反馈。"""

        row = (
            self.db.query(DocumentCategorySuggestion, DocumentClassificationRun, AgentRun)
            .join(
                DocumentClassificationRun,
                DocumentCategorySuggestion.classification_run_id == DocumentClassificationRun.id,
            )
            .join(AgentRun, DocumentClassificationRun.agent_run_id == AgentRun.id)
            .filter(DocumentCategorySuggestion.id == suggestion_id)
            .filter(AgentRun.user_id == current_user.id)
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Classification suggestion not found")
        suggestion, _classification_run, _agent_run = row

        corrected_id, corrected_path = self._resolve_correction(request)
        previous = (
            self.db.query(DocumentCategoryFeedback)
            .filter(DocumentCategoryFeedback.suggestion_id == suggestion.id)
            .filter(DocumentCategoryFeedback.user_id == current_user.id)
            .filter(DocumentCategoryFeedback.is_active.is_(True))
            .order_by(DocumentCategoryFeedback.created_at.desc())
            .first()
        )
        if previous is not None:
            previous.is_active = False

        action = {"ACCEPT": "ACCEPTED", "REJECT": "REJECTED", "CORRECT": "CORRECTED"}[request.action]
        feedback = DocumentCategoryFeedback(
            suggestion_id=suggestion.id,
            document_id=suggestion.document_id,
            user_id=current_user.id,
            action=action,
            corrected_category_id=corrected_id,
            corrected_category_path_json=corrected_path,
            supersedes_feedback_id=previous.id if previous is not None else None,
            is_active=True,
            comment=request.comment,
        )
        self.db.add(feedback)
        self.db.flush()
        positive, negative = _sample_labels(
            action=action,
            original_category_id=suggestion.category_id,
            corrected_category_id=corrected_id,
        )
        return ClassificationFeedbackResponse(
            id=feedback.id,
            suggestion_id=suggestion.id,
            document_id=suggestion.document_id,
            action=action,
            corrected_category_id=corrected_id,
            corrected_category_path=corrected_path,
            positive_category_ids=positive,
            negative_category_ids=negative,
            created_at=feedback.created_at,
        )

    def summary(self, *, current_user: User) -> ClassificationFeedbackSummaryResponse:
        """返回当前用户的明确反馈积累量；沉默样本不参与统计。"""

        query = (
            self.db.query(DocumentCategoryFeedback)
            .filter(DocumentCategoryFeedback.user_id == current_user.id)
            .filter(DocumentCategoryFeedback.is_active.is_(True))
        )
        rows = query.all()
        counts = {"ACCEPTED": 0, "REJECTED": 0, "CORRECTED": 0}
        for row in rows:
            if row.action in counts:
                counts[row.action] += 1
        unique_documents = (
            query.with_entities(func.count(func.distinct(DocumentCategoryFeedback.document_id))).scalar() or 0
        )
        return ClassificationFeedbackSummaryResponse(
            total=len(rows),
            accepted=counts["ACCEPTED"],
            rejected=counts["REJECTED"],
            corrected=counts["CORRECTED"],
            unique_documents=int(unique_documents),
            evaluation_min_samples=self.evaluation_min_samples,
            ready_to_freeze_evaluation_set=len(rows) >= self.evaluation_min_samples,
        )

    def _resolve_correction(self, request: ClassificationFeedbackRequest) -> tuple[str | None, list[str]]:
        """将更正目标收敛到当前 taxonomy 的稳定节点。"""

        if request.action != "CORRECT":
            return None, []
        by_id, by_path = _taxonomy_indexes()
        corrected_id = str(request.corrected_category_id or "").strip()
        corrected_path = [str(item).strip() for item in request.corrected_category_path if str(item).strip()]
        if corrected_id:
            known_path = by_id.get(corrected_id)
            if known_path is None:
                dynamic_suggestion = (
                    self.db.query(DocumentCategorySuggestion)
                    .filter(DocumentCategorySuggestion.category_id == corrected_id)
                    .first()
                )
                if dynamic_suggestion is not None:
                    known_path = [
                        str(item).strip()
                        for item in dynamic_suggestion.category_path_json or []
                        if str(item).strip()
                    ]
            if known_path is None:
                raise HTTPException(status_code=422, detail="Unknown corrected category id")
            return corrected_id, known_path
        resolved_id = by_path.get(tuple(corrected_path))
        if resolved_id is None:
            dynamic_suggestion = (
                self.db.query(DocumentCategorySuggestion)
                .filter(DocumentCategorySuggestion.category_path_json == corrected_path)
                .first()
            )
            resolved_id = (
                str(dynamic_suggestion.category_id or "").strip()
                if dynamic_suggestion is not None
                else None
            )
        if resolved_id is None:
            raise HTTPException(status_code=422, detail="Unknown corrected category path")
        return resolved_id, corrected_path


def _taxonomy_indexes() -> tuple[dict[str, list[str]], dict[tuple[str, ...], str]]:
    """构建稳定分类 ID 与路径双向索引。"""

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
    """把明确反馈转换为可解释的正负样本标签。"""

    original = str(original_category_id or "")
    if action == "ACCEPTED":
        return ([original] if original else []), []
    if action == "REJECTED":
        return [], ([original] if original else [])
    return (
        [corrected_category_id] if corrected_category_id else [],
        [original] if original else [],
    )
