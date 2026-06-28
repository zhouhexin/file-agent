"""ChangeSet API 响应 schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ChangeItemResponse(BaseModel):
    """单条 ChangeItem 明细。"""

    id: str
    target_type: str
    target_id: str | None
    target_document_id: str | None
    change_type: str
    before_value_json: dict[str, Any]
    after_value_json: dict[str, Any]
    source: str
    confidence: float
    evidence_json: dict[str, Any]
    execution_status: str
    created_at: datetime


class ChangeSetResponse(BaseModel):
    """ChangeSet 查询响应。"""

    id: str
    conversation_id: str
    agent_run_id: str
    user_id: str
    status: str
    summary: str
    created_at: datetime
    updated_at: datetime
    items: list[ChangeItemResponse]
