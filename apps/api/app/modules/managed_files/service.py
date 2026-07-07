"""受管目录业务服务。

服务层负责角色校验、部署层 mount_key 校验、异步扫描任务创建和只读文件查询。
"""

from __future__ import annotations

import os

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import FilesystemJob, ManagedFile, ManagedRoot, User
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.repository import FilesystemJobRepository, ManagedFileRepository
from app.modules.managed_files.schemas import (
    FilesystemJobResponse,
    ManagedFileResponse,
    ManagedRootCreateRequest,
    ManagedRootResponse,
)


class ManagedFileService:
    """受管目录 P0 服务。"""

    def __init__(self, db: Session) -> None:
        """注入数据库会话。"""

        self.db = db
        self.repository = ManagedFileRepository(db)

    def create_root(self, *, request: ManagedRootCreateRequest, current_user: User) -> ManagedRootResponse:
        """启用部署层预定义的受管目录。"""

        _require_role(current_user, {"admin"})
        container_path = _configured_container_path(request.root_key)
        if container_path is None:
            raise HTTPException(status_code=400, detail="Managed root is not configured by deployment")
        root = self.repository.upsert_root(
            root_key=request.root_key,
            display_name=request.display_name,
            container_path=container_path,
            created_by=current_user.id,
        )
        self.db.commit()
        self.db.refresh(root)
        return self.to_root_response(root)

    def list_roots(self, *, current_user: User) -> list[ManagedRootResponse]:
        """列出受管目录。"""

        _require_role(current_user, {"admin", "ops"})
        return [self.to_root_response(root) for root in self.repository.list_roots()]

    def create_scan_job(self, *, root_id: str, current_user: User) -> FilesystemJobResponse:
        """创建异步扫描任务，不在 HTTP 请求中同步扫描文件系统。"""

        _require_role(current_user, {"admin", "ops"})
        root = self.repository.get_root(root_id)
        if root is None or not root.enabled:
            raise HTTPException(status_code=404, detail="Managed root not found")
        job = FilesystemJobQueue(self.db).create_job(
            job_type="SCAN_MANAGED_ROOT",
            root_id=root.id,
            created_by=current_user.id,
            payload={"root_key": root.root_key},
        )
        self.db.commit()
        self.db.refresh(job)
        return self.to_job_response(job)

    def get_job(self, *, job_id: str, current_user: User) -> FilesystemJobResponse:
        """查询文件系统任务状态。"""

        _require_role(current_user, {"admin", "ops"})
        job = FilesystemJobRepository(self.db).get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Filesystem job not found")
        return self.to_job_response(job)

    def list_files(
        self,
        *,
        current_user: User,
        root_key: str | None = None,
        extension: str | None = None,
        filename_contains: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ManagedFileResponse]:
        """按元数据查询受管文件。"""

        _require_role(current_user, {"user", "ops", "admin"})
        rows = self.repository.list_files(
            root_key=root_key,
            extension=extension,
            filename_contains=filename_contains,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [self.to_file_response(file=file, root=root) for file, root in rows]

    @staticmethod
    def to_root_response(root: ManagedRoot) -> ManagedRootResponse:
        """转换受管目录响应，隐藏 container_path。"""

        return ManagedRootResponse(
            id=root.id,
            root_key=root.root_key,
            display_name=root.display_name,
            enabled=root.enabled,
            read_only=root.read_only,
            allowed_operations=list(root.allowed_operations_json or []),
        )

    @staticmethod
    def to_job_response(job: FilesystemJob) -> FilesystemJobResponse:
        """转换任务响应。"""

        return FilesystemJobResponse(
            id=job.id,
            job_type=job.job_type,
            root_id=job.root_id,
            status=job.status,
            progress_current=job.progress_current,
            progress_total=job.progress_total,
            result=job.result_json or {},
            error_message=job.error_message,
        )

    @staticmethod
    def to_file_response(*, file: ManagedFile, root: ManagedRoot) -> ManagedFileResponse:
        """转换文件元数据响应。"""

        return ManagedFileResponse(
            root_key=root.root_key,
            display_name=root.display_name,
            relative_path=file.relative_path,
            filename=file.filename,
            extension=file.extension,
            size_bytes=file.size_bytes,
            modified_at=file.modified_at,
            status=file.status,
        )


def _configured_container_path(root_key: str) -> str | None:
    """从部署环境读取 root_key 对应的容器路径。"""

    env_key = f"MANAGED_ROOT_{root_key.upper()}"
    return os.getenv(env_key)


def _require_role(current_user: User, allowed_roles: set[str]) -> None:
    """校验当前用户角色。"""

    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Insufficient role")
