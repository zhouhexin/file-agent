"""OperationPlan 持久化仓库。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import OperationConfirmation, OperationPlan, utcnow


class OperationPlanRepository:
    """封装 OperationPlan 和确认记录读写。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def create_plan(
        self,
        *,
        workspace_id: str,
        conversation_id: str,
        user_id: str,
        operation_type: str,
        risk_level: str,
        reason: str,
        plan_json: dict,
        agent_run_id: str | None = None,
    ) -> OperationPlan:
        """创建等待用户确认的高风险操作计划。"""

        plan = OperationPlan(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            user_id=user_id,
            operation_type=operation_type,
            status="WAITING_CONFIRMATION",
            risk_level=risk_level,
            reason=reason,
            plan_json=plan_json,
        )
        self.db.add(plan)
        self.db.flush()
        return plan

    def get_owned_plan(self, *, plan_id: str, user_id: str) -> OperationPlan | None:
        """按用户边界查询计划，避免越权读取或确认。"""

        return (
            self.db.query(OperationPlan)
            .filter(OperationPlan.id == plan_id, OperationPlan.user_id == user_id)
            .one_or_none()
        )

    def confirm_plan(self, *, plan: OperationPlan, user_id: str, confirmation_text: str) -> OperationConfirmation:
        """记录确认文本，并把计划推进到执行中。"""

        confirmation = OperationConfirmation(
            operation_plan_id=plan.id,
            user_id=user_id,
            confirmation_text=confirmation_text,
        )
        self.db.add(confirmation)
        now = utcnow()
        plan.status = "EXECUTING"
        plan.confirmed_at = now
        plan.updated_at = now
        self.db.flush()
        return confirmation
