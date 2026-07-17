"""重命名批次查询 API schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RenameBatchItemResponse(BaseModel):
    """聊天页面需要的一条重命名文件摘要。"""

    id: str
    managed_file_id: str
    root_key: str
    original_relative_path: str
    original_filename: str
    proposed_filename: str | None
    status: str
    position: int
    warnings: list[str] = Field(default_factory=list)


class RenameBatchResponse(BaseModel):
    """重命名批次统计和少量预览。"""

    id: str
    conversation_id: str
    agent_run_id: str
    operation_plan_id: str | None
    status: str
    scope: dict[str, Any]
    total_count: int
    ready_count: int
    needs_review_count: int
    excluded_count: int
    completed_count: int
    failed_count: int
    preview_items: list[RenameBatchItemResponse]
    created_at: datetime
    updated_at: datetime


class RenameBatchItemsResponse(BaseModel):
    """游标分页的重命名文件明细。"""

    items: list[RenameBatchItemResponse]
    next_cursor: int | None
