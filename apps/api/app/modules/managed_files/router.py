"""受管目录 HTTP 路由。

P0 只提供只读目录配置、异步扫描任务创建和文件元数据查询。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.managed_files.schemas import FilesystemJobResponse, ManagedFileResponse, ManagedRootCreateRequest, ManagedRootResponse
from app.modules.managed_files.service import ManagedFileService


router = APIRouter(tags=["managed-files"])


@router.post("/api/admin/managed-roots", response_model=ManagedRootResponse)
def create_managed_root(
    request: ManagedRootCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ManagedRootResponse:
    """管理员启用部署层预定义的逻辑目录。"""

    return ManagedFileService(db).create_root(request=request, current_user=current_user)


@router.get("/api/admin/managed-roots", response_model=list[ManagedRootResponse])
def list_managed_roots(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ManagedRootResponse]:
    """列出受管目录。"""

    return ManagedFileService(db).list_roots(current_user=current_user)


@router.post("/api/admin/managed-roots/{root_id}/scan", response_model=FilesystemJobResponse)
def create_scan_job(
    root_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FilesystemJobResponse:
    """创建异步扫描任务。"""

    return ManagedFileService(db).create_scan_job(root_id=root_id, current_user=current_user)


@router.get("/api/admin/filesystem-jobs/{job_id}", response_model=FilesystemJobResponse)
def get_filesystem_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FilesystemJobResponse:
    """查询异步扫描任务状态。"""

    return ManagedFileService(db).get_job(job_id=job_id, current_user=current_user)


@router.get("/api/managed-files", response_model=list[ManagedFileResponse])
def list_managed_files(
    root_key: str | None = None,
    extension: str | None = None,
    filename_contains: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ManagedFileResponse]:
    """按元数据只读查询受管文件清单。"""

    return ManagedFileService(db).list_files(
        current_user=current_user,
        root_key=root_key,
        extension=extension,
        filename_contains=filename_contains,
        status=status,
        limit=limit,
        offset=offset,
    )
