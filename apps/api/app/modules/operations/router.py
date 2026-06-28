"""OperationPlan HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.operations.schemas import (
    OperationConfirmRequest,
    OperationConfirmResponse,
    OperationPlanCreateRequest,
    OperationPlanResponse,
)
from app.modules.operations.service import OperationPlanService

router = APIRouter(prefix="/api/operations", tags=["operations"])


@router.post("/plans", response_model=OperationPlanResponse)
def create_operation_plan(
    request: OperationPlanCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OperationPlanResponse:
    """创建高风险操作计划，不执行真实文件动作。"""

    return OperationPlanService(db).create_plan(request=request, current_user=current_user)


@router.get("/plans/{plan_id}", response_model=OperationPlanResponse)
def get_operation_plan(
    plan_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OperationPlanResponse:
    """查询当前用户自己的高风险操作计划。"""

    return OperationPlanService(db).get_plan(plan_id=plan_id, current_user=current_user)


@router.post("/plans/{plan_id}/confirm", response_model=OperationConfirmResponse)
def confirm_operation_plan(
    plan_id: str,
    request: OperationConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OperationConfirmResponse:
    """确认高风险操作计划；当前阶段只推进状态，不执行文件动作。"""

    return OperationPlanService(db).confirm_plan(
        plan_id=plan_id,
        request=request,
        current_user=current_user,
    )
