"""分类建议反馈 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.classification.feedback_schemas import (
    ClassificationClarificationResolveRequest,
    ClassificationClarificationResponse,
    ClassificationFeedbackRequest,
    ClassificationFeedbackResponse,
    ClassificationFeedbackSummaryResponse,
    ClassificationTaxonomyOptionsResponse,
)
from app.modules.classification.clarification_service import (
    ClassificationClarificationError,
    ClassificationClarificationService,
)
from app.modules.classification.feedback_service import ClassificationFeedbackService
from app.modules.classification.taxonomy_service import read_default_taxonomy_catalog
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.schemas import CategoryNode
from app.modules.classification.organization_query_service import (
    ClassificationOrganizationQueryService,
    OrganizationQueryError,
)
from app.modules.classification.organization_schemas import (
    OrganizationFilePageResponse,
    OrganizationTreeResponse,
)


router = APIRouter(prefix="/api/classification", tags=["classification"])


@router.get("/organization/tree", response_model=OrganizationTreeResponse)
def get_classification_organization_tree(
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_user),
) -> OrganizationTreeResponse:
    """返回共享活动文件的主分类树和待复核虚拟节点。"""

    return ClassificationOrganizationQueryService(db).tree()


@router.get("/organization/files", response_model=OrganizationFilePageResponse)
def list_classification_organization_files(
    category_id: str | None = None,
    scope: str = Query(default="descendants", pattern="^(direct|descendants)$"),
    review_only: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_user),
) -> OrganizationFilePageResponse:
    """按分类范围或待复核状态分页读取已发布工作副本。"""

    try:
        return ClassificationOrganizationQueryService(db).files(
            category_id=category_id,
            scope=scope,
            review_only=review_only,
            page=page,
            page_size=page_size,
        )
    except OrganizationQueryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/taxonomy/options", response_model=ClassificationTaxonomyOptionsResponse)
def get_classification_taxonomy_options(
    _current_user: User = Depends(get_current_user),
) -> ClassificationTaxonomyOptionsResponse:
    """返回当前 taxonomy 的稳定 ID 和显示路径，不暴露规则与内部信号。"""

    taxonomy = load_default_taxonomy()
    options: list[dict[str, object]] = []

    def walk(node: CategoryNode, parents: list[str]) -> None:
        path = [*parents, node.name]
        if node.id:
            options.append(
                {
                    "category_id": node.id,
                    "label": " / ".join(path),
                    "path": path,
                }
            )
        for child in node.children:
            walk(child, path)

    for root in taxonomy.categories:
        walk(root, [])
    return ClassificationTaxonomyOptionsResponse(
        taxonomy_key=taxonomy.key,
        taxonomy_version=taxonomy.version,
        options=options,
    )


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
    """原子保存明确反馈、正式分类、审计和图谱待办。"""

    settings = get_settings()
    response = ClassificationFeedbackService(
        db,
        evaluation_min_samples=settings.graph_feedback_eval_min_samples,
    ).record(suggestion_id=suggestion_id, request=request, current_user=current_user)
    db.commit()
    return response


@router.get("/taxonomy")
def get_classification_taxonomy(
    _current_user: User = Depends(get_current_user),
) -> dict:
    """返回分类选择卡所需的 ACTIVE taxonomy，不返回内部信号和物理路径。"""

    return read_default_taxonomy_catalog(detail_level="brief", max_depth=8)[
        "taxonomy"
    ]


@router.get(
    "/clarifications/{clarification_id}",
    response_model=ClassificationClarificationResponse,
)
def get_classification_clarification(
    clarification_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClassificationClarificationResponse:
    """恢复当前用户的分类选择卡，不能读取其他用户选择状态。"""

    try:
        payload = ClassificationClarificationService(db).get_public(
            clarification_id=clarification_id,
            user_id=current_user.id,
        )
    except ClassificationClarificationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return ClassificationClarificationResponse.model_validate(payload)


@router.post(
    "/clarifications/{clarification_id}/resolve",
    response_model=ClassificationFeedbackResponse,
)
def resolve_classification_clarification(
    clarification_id: str,
    request: ClassificationClarificationResolveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClassificationFeedbackResponse:
    """消费后端签发的分类选项，并调用与按钮相同的正式分类事务。"""

    clarification_service = ClassificationClarificationService(db)
    try:
        selection = clarification_service.resolve(
            clarification_id=clarification_id,
            user_id=current_user.id,
            option_id=request.option_id,
        )
    except ClassificationClarificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response = ClassificationFeedbackService(
        db,
        evaluation_min_samples=get_settings().graph_feedback_eval_min_samples,
    ).record(
        suggestion_id=selection.suggestion_id,
        request=ClassificationFeedbackRequest(
            action=selection.action,
            corrected_category_id=selection.target_category_id,
            relation_role=selection.relation_role,
            agent_run_id=selection.agent_run_id,
            idempotency_key=f"{clarification_id}:{request.option_id}",
        ),
        current_user=current_user,
    )
    clarification_service.mark_resolved(
        clarification_id=clarification_id,
        user_id=current_user.id,
        option_id=request.option_id,
        feedback_id=response.id,
    )
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
