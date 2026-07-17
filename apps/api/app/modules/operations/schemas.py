"""OperationPlan API schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class OperationPlanItem(BaseModel):
    """单个计划项。"""

    document_id: str = Field(min_length=1)
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    rename_metadata: dict[str, Any] = Field(default_factory=dict)
    execution_status: str = "PLANNED"


class OperationPlanCreateRequest(BaseModel):
    """创建 OperationPlan 的请求。"""

    conversation_id: str = Field(min_length=1)
    operation_type: str = Field(min_length=1)
    reason: str = ""
    risk_level: str = "medium"
    items: list[OperationPlanItem] = Field(min_length=1)


class OperationPlanResponse(BaseModel):
    """OperationPlan 查询响应。"""

    id: str
    conversation_id: str
    user_id: str
    operation_type: str
    status: str
    requires_confirmation: bool
    risk_level: str
    reason: str
    items: list[OperationPlanItem]
    total_item_count: int = 0
    items_truncated: bool = False
    skipped_items: list[dict[str, Any]] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    executed_at: datetime | None


class OperationConfirmRequest(BaseModel):
    """确认 OperationPlan 的请求。"""

    confirmation: str = Field(min_length=1)
    excluded_rename_batch_item_ids: list[str] = Field(default_factory=list, max_length=500)


class OperationConfirmResponse(BaseModel):
    """确认 OperationPlan 后的响应。"""

    id: str
    status: str
    changeset_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
