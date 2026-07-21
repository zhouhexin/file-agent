"""三层文件生命周期 HTTP 路由。

路由只暴露逻辑 ID 和相对路径，不返回受管原始目录、工作副本目录或回收站绝对路径。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.file_lifecycle.schemas import (
    ArchiveStatusResponse,
    DocumentVersionResponse,
    DuplicateDecisionRequest,
    DuplicateDecisionResponse,
    DuplicateReviewResponse,
    RestorePlanRequest,
    TrashEntryResponse,
    WorkingCopyLineageResponse,
    WorkingCopyPathRecordResponse,
    WorkingCopyResponse,
)
from app.modules.file_lifecycle.service import UploadLifecycleService, WorkingCopyQueryService
from app.modules.file_lifecycle.operations import WorkingCopyOperationService
from app.modules.operations.schemas import OperationPlanResponse
from app.modules.operations.service import OperationPlanService


router = APIRouter(tags=["file-lifecycle"])


@router.get(
    "/api/uploads/{upload_version_id}/duplicate-review",
    response_model=DuplicateReviewResponse,
)
def get_duplicate_review(
    upload_version_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DuplicateReviewResponse:
    """读取当前用户上传版本的重复确认卡。"""

    return UploadLifecycleService(db).get_review(
        upload_version_id=upload_version_id,
        current_user=current_user,
    )


@router.post(
    "/api/uploads/{upload_version_id}/duplicate-review/decision",
    response_model=DuplicateDecisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def decide_duplicate_review(
    upload_version_id: str,
    request: DuplicateDecisionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DuplicateDecisionResponse:
    """记录用户明确决策；只有继续上传才允许进入异步归档。"""

    return UploadLifecycleService(db).decide(
        upload_version_id=upload_version_id,
        request=request,
        current_user=current_user,
    )


@router.get(
    "/api/uploads/{upload_version_id}/archive-status",
    response_model=ArchiveStatusResponse,
)
def get_archive_status(
    upload_version_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ArchiveStatusResponse:
    """查询上传附件的归档和工作副本导入状态。"""

    return UploadLifecycleService(db).get_archive_status(
        upload_version_id=upload_version_id,
        current_user=current_user,
    )


@router.get("/api/working-copies", response_model=list[WorkingCopyResponse])
def list_working_copies(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[WorkingCopyResponse]:
    """列出当前用户默认工作区的工作副本。"""

    return WorkingCopyQueryService(db).list(current_user)


@router.get("/api/working-copies/{working_copy_id}", response_model=WorkingCopyResponse)
def get_working_copy(
    working_copy_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkingCopyResponse:
    """读取单个工作副本元数据。"""

    return WorkingCopyQueryService(db).get(working_copy_id=working_copy_id, current_user=current_user)


@router.get("/api/working-copies/{working_copy_id}/download", response_class=FileResponse)
def download_working_copy(
    working_copy_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """下载当前工作区工作副本内容。"""

    path, filename, content_type = WorkingCopyQueryService(db).download_path(
        working_copy_id=working_copy_id,
        current_user=current_user,
    )
    return FileResponse(path=path, filename=filename, media_type=content_type)


@router.get(
    "/api/working-copies/{working_copy_id}/lineage",
    response_model=WorkingCopyLineageResponse,
)
def get_working_copy_lineage(
    working_copy_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkingCopyLineageResponse:
    """查询工作副本到原始文件的追溯关系。"""

    return WorkingCopyQueryService(db).lineage(
        working_copy_id=working_copy_id,
        current_user=current_user,
    )


@router.get(
    "/api/working-copies/{working_copy_id}/versions",
    response_model=list[DocumentVersionResponse],
)
def list_working_copy_versions(
    working_copy_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DocumentVersionResponse]:
    """列出工作副本内容版本；重命名和移动不会新增版本。"""

    return WorkingCopyQueryService(db).versions(
        working_copy_id=working_copy_id,
        current_user=current_user,
    )


@router.get(
    "/api/working-copies/{working_copy_id}/path-records",
    response_model=list[WorkingCopyPathRecordResponse],
)
def list_working_copy_path_records(
    working_copy_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[WorkingCopyPathRecordResponse]:
    """列出工作副本重命名和移动的不可变路径记录。"""

    return WorkingCopyQueryService(db).path_records(
        working_copy_id=working_copy_id,
        current_user=current_user,
    )


@router.get("/api/trash-entries", response_model=list[TrashEntryResponse])
def list_trash_entries(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TrashEntryResponse]:
    """列出当前工作区的可恢复回收站条目。"""

    return WorkingCopyQueryService(db).trash_entries(current_user=current_user)


@router.post(
    "/api/trash-entries/{trash_entry_id}/restore-plan",
    response_model=OperationPlanResponse,
)
def create_trash_restore_plan(
    trash_entry_id: str,
    request: RestorePlanRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OperationPlanResponse:
    """为回收站条目创建待确认恢复计划，不直接移动文件。"""

    plan = WorkingCopyOperationService(db).create_restore_plan(
        trash_entry_id=trash_entry_id,
        conversation_id=request.conversation_id,
        current_user=current_user,
    )
    db.commit()
    db.refresh(plan)
    return OperationPlanService.to_response(plan)
