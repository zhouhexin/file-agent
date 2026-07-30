"""Planner Shadow 对比指标读取服务和管理员接口。

本模块只聚合脱敏后的 ``planner_shadow_comparisons``，不返回 Prompt、文件正文、Tool 输入或用户消息。
指标用于判断是否可以进入灰度，不会自动修改 Adaptive Planner 模式或灰度比例。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import PlannerShadowComparison, User
from app.modules.auth.dependencies import require_ops_or_admin


class PlannerShadowMetricsResponse(BaseModel):
    """管理员可查看的 Planner Shadow 脱敏聚合指标。"""

    model_config = ConfigDict(extra="forbid")

    catalog_fingerprint: str = ""
    schema_version: str = ""
    sample_count: int = 0
    validation_success_count: int = 0
    validation_success_rate: float = Field(default=0, ge=0, le=1)
    decision_match_rate: float = Field(default=0, ge=0, le=1)
    scope_match_rate: float = Field(default=0, ge=0, le=1)
    risk_match_rate: float = Field(default=0, ge=0, le=1)
    confirmation_match_rate: float = Field(default=0, ge=0, le=1)
    adaptive_error_counts: dict[str, int] = Field(default_factory=dict)


class PlannerShadowMetricsService:
    """从已持久化 Shadow 比较记录计算安全指标。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def summarize(self, *, limit: int = 5000) -> PlannerShadowMetricsResponse:
        """聚合最近记录；零样本时所有比率返回 0，禁止伪造达标状态。"""

        base_query = self.db.query(PlannerShadowComparison).order_by(
            PlannerShadowComparison.created_at.desc(),
            PlannerShadowComparison.id.desc(),
        )
        latest = base_query.first()
        if latest is None:
            return PlannerShadowMetricsResponse()
        # 只比较当前最新 Catalog 与 schema 的同一批样本，避免旧能力集的历史
        # 结果稀释或抬高本次灰度门槛。
        rows = (
            self.db.query(PlannerShadowComparison)
            .filter(
                PlannerShadowComparison.catalog_fingerprint
                == latest.catalog_fingerprint,
                PlannerShadowComparison.schema_version
                == latest.schema_version,
            )
            .order_by(
                PlannerShadowComparison.created_at.desc(),
                PlannerShadowComparison.id.desc(),
            )
            .limit(limit)
            .all()
        )
        total = len(rows)
        success_count = sum(
            row.adaptive_validation_status == "COMPLETED"
            for row in rows
        )
        error_counts: dict[str, int] = {}
        for row in rows:
            if row.adaptive_error_code:
                error_counts[row.adaptive_error_code] = (
                    error_counts.get(row.adaptive_error_code, 0) + 1
                )
        return PlannerShadowMetricsResponse(
            catalog_fingerprint=latest.catalog_fingerprint,
            schema_version=latest.schema_version,
            sample_count=total,
            validation_success_count=success_count,
            validation_success_rate=success_count / total,
            decision_match_rate=(
                sum(
                    row.legacy_decision_type
                    == row.adaptive_decision_type
                    for row in rows
                )
                / total
            ),
            scope_match_rate=sum(row.scope_match for row in rows) / total,
            risk_match_rate=sum(row.risk_match for row in rows) / total,
            confirmation_match_rate=(
                sum(row.confirmation_match for row in rows) / total
            ),
            adaptive_error_counts=error_counts,
        )


router = APIRouter(
    prefix="/api/admin/planner-shadow",
    tags=["admin-planner-shadow"],
)


@router.get("/metrics", response_model=PlannerShadowMetricsResponse)
def read_planner_shadow_metrics(
    limit: int = Query(default=5000, ge=1, le=10000),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_ops_or_admin),
) -> PlannerShadowMetricsResponse:
    """允许 ops/admin 查看 Shadow 聚合指标，但不能通过该接口切换灰度。"""

    return PlannerShadowMetricsService(db).summarize(limit=limit)
