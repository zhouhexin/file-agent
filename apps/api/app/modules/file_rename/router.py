"""重命名批次摘要和文件明细查询接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import FileRenameBatchItem, User
from app.modules.auth.dependencies import get_current_user
from app.modules.file_rename.api_schemas import (
    RenameBatchItemResponse,
    RenameBatchItemsResponse,
    RenameBatchResponse,
)
from app.modules.file_rename.batch_service import RenameBatchService


router = APIRouter(prefix="/api/file-renames", tags=["file-renames"])


@router.get("/batches/{batch_id}", response_model=RenameBatchResponse)
def get_rename_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RenameBatchResponse:
    """返回批次统计、待复核优先的少量预览。"""

    service = RenameBatchService(db, current_user.id)
    batch = service.get_owned_batch(batch_id)
    preview = (
        service.list_all_items(batch_id=batch.id, statuses={"NEEDS_REVIEW"})[:10]
        or service.list_all_items(batch_id=batch.id)[:10]
    )
    return _batch_response(batch, preview)


@router.get("/batches/{batch_id}/items", response_model=RenameBatchItemsResponse)
def list_rename_batch_items(
    batch_id: str,
    status: str | None = None,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RenameBatchItemsResponse:
    """按状态和位置游标异步读取批次文件。"""

    service = RenameBatchService(db, current_user.id)
    batch = service.get_owned_batch(batch_id)
    items, next_cursor = service.list_page(batch=batch, status=status, cursor=cursor, limit=limit)
    return RenameBatchItemsResponse(
        items=[_item_response(item) for item in items],
        next_cursor=next_cursor,
    )


def _batch_response(batch: Any, preview: list[FileRenameBatchItem]) -> RenameBatchResponse:
    """把持久化批次转换为不含正文的安全响应。"""

    return RenameBatchResponse(
        id=batch.id,
        conversation_id=batch.conversation_id,
        agent_run_id=batch.agent_run_id,
        operation_plan_id=batch.operation_plan_id,
        status=batch.status,
        scope=batch.scope_json,
        total_count=batch.total_count,
        ready_count=batch.ready_count,
        needs_review_count=batch.needs_review_count,
        excluded_count=batch.excluded_count,
        completed_count=batch.completed_count,
        failed_count=batch.failed_count,
        preview_items=[_item_response(item) for item in preview],
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


def _item_response(item: FileRenameBatchItem) -> RenameBatchItemResponse:
    """只暴露逻辑相对路径、名称和状态。"""

    suggestion = item.metadata_json.get("suggestion", {}) if isinstance(item.metadata_json, dict) else {}
    warnings = suggestion.get("warnings", []) if isinstance(suggestion, dict) else []
    return RenameBatchItemResponse(
        id=item.id,
        managed_file_id=item.managed_file_id,
        root_key=item.root_key,
        original_relative_path=item.original_relative_path,
        original_filename=item.original_filename,
        proposed_filename=item.proposed_filename,
        status=item.status,
        position=item.position,
        warnings=[str(value) for value in warnings if value],
    )
