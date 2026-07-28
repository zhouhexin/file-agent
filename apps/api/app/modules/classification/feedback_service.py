"""分类反馈的追加写入、版本关联和冷启动统计。"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    DocumentCategoryFeedback,
    User,
)
from app.modules.classification.decision_service import ClassificationDecisionService
from app.modules.classification.feedback_schemas import (
    ClassificationFeedbackRequest,
    ClassificationFeedbackResponse,
    ClassificationFeedbackSummaryResponse,
)


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
        """通过统一事务服务保存反馈和正式分类，不再只写反馈样本。"""

        return ClassificationDecisionService(self.db).decide(
            suggestion_id=suggestion_id,
            request=request,
            current_user=current_user,
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
