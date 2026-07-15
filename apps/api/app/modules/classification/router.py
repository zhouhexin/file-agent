"""分类建议反馈 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.classification.feedback_schemas import (
    ClassificationFeedbackRequest,
    ClassificationFeedbackResponse,
    ClassificationFeedbackSummaryResponse,
)
from app.modules.classification.feedback_service import ClassificationFeedbackService


router = APIRouter(prefix="/api/classification", tags=["classification"])


@router.post(
    "/suggestions/{suggestion_id}/feedback",
    response_model=ClassificationFeedbackResponse,
)
def record_classification_feedback(
    suggestion_id: str,
    request: ClassificationFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClassificationFeedbackResponse:
    """保存用户对自己 AgentRun 分类建议的明确反馈。"""

    settings = get_settings()
    if not settings.graph_feedback_collection_enabled:
        raise HTTPException(status_code=409, detail="Classification feedback collection is disabled")
    response = ClassificationFeedbackService(
        db,
        evaluation_min_samples=settings.graph_feedback_eval_min_samples,
    ).record(suggestion_id=suggestion_id, request=request, current_user=current_user)
    db.commit()
    return response


@router.get("/feedback/summary", response_model=ClassificationFeedbackSummaryResponse)
def get_classification_feedback_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClassificationFeedbackSummaryResponse:
    """查询当前用户可用于冷启动评测的明确反馈数量。"""

    return ClassificationFeedbackService(
        db,
        evaluation_min_samples=get_settings().graph_feedback_eval_min_samples,
    ).summary(current_user=current_user)
