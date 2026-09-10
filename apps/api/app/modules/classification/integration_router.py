"""WorkBuddy 等外部连接器的分类落位路由。

此模块与批量导入路由分开注册：已入库工作副本的明确 SET_PRIMARY/MOVE 不依赖导入试点开关，
但仍必须经过当前用户 JWT、路径绑定请求模型和同一 Placement 协调服务。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.classification.placement_schemas import (
    MoveWorkingCopyRequest,
    PlacementStatusResponse,
    PlacementSubmissionResponse,
    SetPrimaryCategoryRequest,
)
from app.modules.classification.router import (
    _submit_classification_placement,
    get_classification_placement_status,
)


router = APIRouter(prefix="/api/integrations/v1", tags=["integrations"])


@router.post(
    "/working-copies/{working_copy_id}/primary-category",
    response_model=PlacementSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def set_integration_working_copy_primary_category(
    working_copy_id: str,
    payload: SetPrimaryCategoryRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlacementSubmissionResponse:
    """受理连接器的主分类更正；连接器不能传递宿主路径、用户 ID 或确认字段。"""

    return _submit_classification_placement(
        command=payload.to_command(working_copy_id=working_copy_id),
        request=request,
        db=db,
        current_user=current_user,
        client_id="workbuddy-mcp",
        source_event_prefix="integration:workbuddy-mcp",
    )


@router.post(
    "/working-copies/{working_copy_id}/placement",
    response_model=PlacementSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def move_integration_working_copy(
    working_copy_id: str,
    payload: MoveWorkingCopyRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlacementSubmissionResponse:
    """受理连接器的受控 MOVE；目标由后端基于冻结 taxonomy 或登记目录解析。"""

    return _submit_classification_placement(
        command=payload.to_command(working_copy_id=working_copy_id),
        request=request,
        db=db,
        current_user=current_user,
        client_id="workbuddy-mcp",
        source_event_prefix="integration:workbuddy-mcp",
    )


@router.get(
    "/placement-operations/{operation_id}",
    response_model=PlacementStatusResponse,
)
def get_integration_placement_operation(
    operation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlacementStatusResponse:
    """读取发起者可见的真实异步进度，不触发文件移动、分类计算或重试。"""

    return get_classification_placement_status(
        operation_id=operation_id,
        db=db,
        current_user=current_user,
    )
