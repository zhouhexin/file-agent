"""分类建议反馈 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import ClassificationPlacementOperation, User, WorkingCopy
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
from app.modules.classification.placement_authorization import PlacementAuthorizationService
from app.modules.classification.placement_schemas import (
    MoveWorkingCopyRequest,
    PlacementCommand,
    PlacementStatusResponse,
    PlacementSubmissionResponse,
    SetPrimaryCategoryRequest,
)
from app.modules.classification.placement_service import (
    ClassificationPlacementService,
    PlacementServiceError,
)
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


router = APIRouter(prefix="/api/classification", tags=["classification"])


_PLACEMENT_CONFLICT_CODES = {
    "IDEMPOTENCY_CONFLICT",
    "PLACEMENT_IN_PROGRESS",
    "PLACEMENT_RECONCILIATION_REQUIRED",
    "TARGET_NAME_CONFLICT",
    "TAXONOMY_VERSION_STALE",
    "WORKING_COPY_REVISION_CONFLICT",
    "WORKING_COPY_VERSION_CONFLICT",
}
_PLACEMENT_UNPROCESSABLE_CODES = {
    "AUTHORIZATION_MODE_INVALID",
    "CATEGORY_NOT_PLACEABLE",
    "CONTAINER_CROSSES_CATEGORY_BOUNDARY",
    "OPERATION_NOT_AUTHORIZED",
    "TARGET_OUTSIDE_CLASSIFICATION_TREE",
}


def _placement_http_exception(error: PlacementServiceError) -> HTTPException:
    """将受控落位服务错误映射为统一 HTTP 错误信封。"""

    if error.code == "WORKING_COPY_NOT_FOUND":
        status_code = status.HTTP_404_NOT_FOUND
    elif error.code == "FORBIDDEN":
        status_code = status.HTTP_403_FORBIDDEN
    elif error.code in _PLACEMENT_UNPROCESSABLE_CODES:
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    elif error.code in _PLACEMENT_CONFLICT_CODES:
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": error.code, "message": str(error)}},
    )


def _placement_status_response(
    operation: ClassificationPlacementOperation,
) -> PlacementStatusResponse:
    """投影单个操作的安全状态；仅返回逻辑 ID、状态和受控结果摘要。"""

    result = dict(operation.result_json or {})
    error = dict(operation.error_json or {})
    placement_status = str(result.get("placement_status") or "")
    if not placement_status:
        placement_status = {
            "EXECUTING": "APPLYING",
            "FS_APPLIED": "RECONCILING",
            "RECONCILING": "RECONCILING",
            "FAILED": "ERROR",
        }.get(operation.state, "PENDING")
    return PlacementStatusResponse(
        operation_id=operation.id,
        status=operation.state,
        working_copy_id=operation.working_copy_id,
        operation_type=operation.operation_type,
        job_id=operation.job_id,
        operation_plan_id=operation.operation_plan_id,
        changeset_id=operation.changeset_id,
        placement_status=placement_status,
        result=result,
        error_code=str(error["code"]) if error.get("code") else None,
    )


@router.post(
    "/placements",
    response_model=PlacementSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_classification_placement(
    command: PlacementCommand,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlacementSubmissionResponse:
    """受理明确 SET_PRIMARY/MOVE；只接受稳定 ID，且不需要第二次确认。"""

    return _submit_classification_placement(
        command=command,
        request=request,
        db=db,
        current_user=current_user,
    )


def _submit_classification_placement(
    *,
    command: PlacementCommand,
    request: Request,
    db: Session,
    current_user: User,
) -> PlacementSubmissionResponse:
    """复用专用路径和兼容入口的服务端对象校验、授权快照与异步受理。"""

    shared_workspace_id = get_shared_workspace_id(db)
    working_copy = db.get(WorkingCopy, str(command.working_copy_id))
    if (
        working_copy is None
        or working_copy.status != "ACTIVE"
        or working_copy.workspace_id != shared_workspace_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "WORKING_COPY_NOT_FOUND", "message": "活动工作副本不存在"}},
        )
    request_id = str(getattr(request.state, "request_id", "") or "classification-api")
    authorization = PlacementAuthorizationService(db).authorize_structured_request(
        command=command,
        current_user=current_user,
        workspace_id=shared_workspace_id,
        client_id="classification-api",
        request_id=request_id,
        source_event_ref=f"http:{request_id}",
    )
    try:
        submission = ClassificationPlacementService(db).submit(
            command=command,
            authorization=authorization,
        )
        db.commit()
    except PlacementServiceError as exc:
        db.rollback()
        raise _placement_http_exception(exc) from exc
    return PlacementSubmissionResponse.model_validate(submission.as_dict())


@router.post(
    "/working-copies/{working_copy_id}/primary-category",
    response_model=PlacementSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def set_working_copy_primary_category(
    working_copy_id: str,
    payload: SetPrimaryCategoryRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlacementSubmissionResponse:
    """受理路径指定副本的 SET_PRIMARY；正文没有对象 ID、动作或授权越权字段。"""

    return _submit_classification_placement(
        command=payload.to_command(working_copy_id=working_copy_id),
        request=request,
        db=db,
        current_user=current_user,
    )


@router.post(
    "/working-copies/{working_copy_id}/placement",
    response_model=PlacementSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def move_working_copy_by_classification(
    working_copy_id: str,
    payload: MoveWorkingCopyRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlacementSubmissionResponse:
    """受理路径指定副本的 MOVE；仅允许分类或登记目录的冻结目标。"""

    return _submit_classification_placement(
        command=payload.to_command(working_copy_id=working_copy_id),
        request=request,
        db=db,
        current_user=current_user,
    )


@router.get("/placements/{operation_id}", response_model=PlacementStatusResponse)
def get_classification_placement_status(
    operation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlacementStatusResponse:
    """读取本人提交的分类落位操作状态；查询不触发任何文件或分类写入。"""

    operation = db.get(ClassificationPlacementOperation, operation_id)
    if operation is None or operation.actor_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "PLACEMENT_NOT_FOUND", "message": "分类落位操作不存在"}},
        )
    return _placement_status_response(operation)


@router.get(
    "/placement-operations/{operation_id}",
    response_model=PlacementStatusResponse,
)
def get_classification_placement_operation(
    operation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlacementStatusResponse:
    """提供文档约定的状态路径，复用同一按发起人隐藏存在性的只读投影。"""

    return get_classification_placement_status(
        operation_id=operation_id,
        db=db,
        current_user=current_user,
    )


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
