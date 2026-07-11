"""受管目录业务服务。

服务层负责角色校验、部署层 mount_key 校验、异步扫描任务创建和只读文件查询。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from mimetypes import guess_type
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.models import FilesystemJob, ManagedFile, ManagedRoot, User
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.path_policy import PathPolicyError, resolve_managed_relative_path
from app.modules.managed_files.repository import FilesystemJobRepository, ManagedFileRepository
from app.modules.managed_files.scanner import ManagedFileScanner
from app.modules.managed_files.schemas import (
    FilesystemJobResponse,
    ManagedCategoryResponse,
    ManagedFileResponse,
    ManagedRootCreateRequest,
    ManagedRootResponse,
)


@dataclass(frozen=True)
class ManagedFileQueryScope:
    """受管文件查询的最终 root 与子路径范围。"""

    root_key: str | None
    path_prefix: str | None
    configured_root_keys: list[str]
    unresolved_root_key: str | None = None


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
            classification_mode=request.classification_mode,
            created_by=current_user.id,
        )
        self.db.commit()
        self.db.refresh(root)
        return self.to_root_response(root)

    def list_roots(self, *, current_user: User) -> list[ManagedRootResponse]:
        """列出受管目录。"""

        _require_role(current_user, {"admin", "ops"})
        sync_configured_managed_roots(self.db, scan=False)
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
        path_prefix: str | None = None,
        extension: str | None = None,
        filename_contains: str | None = None,
        category_path: str | None = None,
        classification_mode: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ManagedFileResponse]:
        """按元数据查询受管文件。"""

        _require_role(current_user, {"user", "ops", "admin"})
        scope = resolve_managed_file_query_scope(root_key=root_key, path_prefix=path_prefix)
        sync_configured_managed_roots(self.db, root_key=scope.root_key, scan=True)
        self.db.commit()
        if scope.unresolved_root_key:
            return []
        rows = self.repository.list_files(
            root_key=scope.root_key,
            root_keys=scope.configured_root_keys if scope.root_key is None else None,
            path_prefix=scope.path_prefix,
            extension=extension,
            filename_contains=filename_contains,
            category_path=category_path,
            classification_mode=classification_mode,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [self.to_file_response(file=file, root=root) for file, root in rows]

    def list_categories(self, *, current_user: User, root_key: str | None = None) -> list[ManagedCategoryResponse]:
        """列出父目录作为分类的受管目录分类树。"""

        _require_role(current_user, {"user", "ops", "admin"})
        sync_configured_managed_roots(self.db, root_key=root_key, scan=True)
        self.db.commit()
        return [
            ManagedCategoryResponse(
                root_key=row_root_key,
                display_name=display_name,
                category_path=category_path,
                file_count=int(file_count),
            )
            for row_root_key, display_name, category_path, file_count in self.repository.list_category_paths(root_key=root_key)
        ]

    def get_preview_response(
        self,
        *,
        current_user: User,
        root_key: str,
        relative_path: str,
    ) -> FileResponse:
        """返回受管文件内容供浏览器预览或下载，路径必须保持在受管根目录内。"""

        _require_role(current_user, {"user", "ops", "admin"})
        roots = sync_configured_managed_roots(self.db, root_key=root_key, scan=False)
        self.db.commit()
        root = roots[0] if roots else self.repository.get_root_by_key(root_key.strip().lower())
        if root is None or not root.enabled:
            raise HTTPException(status_code=404, detail="Managed root not found")
        try:
            file_path = resolve_managed_relative_path(
                root_path=Path(root.container_path),
                relative_path=relative_path,
            )
        except PathPolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        media_type = guess_type(file_path.name)[0] or "application/octet-stream"
        return FileResponse(
            path=file_path,
            media_type=media_type,
            filename=file_path.name,
        )

    @staticmethod
    def to_root_response(root: ManagedRoot) -> ManagedRootResponse:
        """转换受管目录响应，隐藏 container_path。"""

        return ManagedRootResponse(
            id=root.id,
            root_key=root.root_key,
            display_name=root.display_name,
            classification_mode=root.classification_mode,
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
            category_path=file.category_path,
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


def resolve_managed_file_query_scope(
    *,
    root_key: str | None,
    path_prefix: str | None,
) -> ManagedFileQueryScope:
    """把用户提到的逻辑目录或子目录解析成当前 env 配置内的查询范围。"""

    configured_root_keys = _configured_root_keys(root_key=None)
    normalized_root_key = root_key.strip().lower() if root_key else None
    normalized_path_prefix = _join_path_parts(path_prefix)

    if not normalized_root_key:
        return ManagedFileQueryScope(
            root_key=None,
            path_prefix=normalized_path_prefix,
            configured_root_keys=configured_root_keys,
        )

    if _configured_container_path(normalized_root_key) is not None:
        return ManagedFileQueryScope(
            root_key=normalized_root_key,
            path_prefix=normalized_path_prefix,
            configured_root_keys=[normalized_root_key],
        )

    if len(configured_root_keys) == 1:
        # 用户可能把唯一受管根目录下的子目录名说成了 root_key，例如：
        # env 只配置 downloads，但用户说“列出 file_agent_spreadsheet_patch_files 下的文件”。
        return ManagedFileQueryScope(
            root_key=configured_root_keys[0],
            path_prefix=_join_path_parts(normalized_root_key, normalized_path_prefix),
            configured_root_keys=configured_root_keys,
        )

    return ManagedFileQueryScope(
        root_key=normalized_root_key,
        path_prefix=normalized_path_prefix,
        configured_root_keys=configured_root_keys,
        unresolved_root_key=normalized_root_key,
    )


def _join_path_parts(*parts: str | None) -> str | None:
    """拼接受管目录内的 POSIX 相对路径片段。"""

    cleaned = [part.replace("\\", "/").strip().strip("/") for part in parts if part]
    joined = "/".join(part for part in cleaned if part)
    return joined or None


def sync_configured_managed_roots(
    db: Session,
    *,
    root_key: str | None = None,
    scan: bool = False,
    created_by: str | None = None,
) -> list[ManagedRoot]:
    """把 env 中声明的受管目录同步到数据库，并按需执行只读扫描。

    env 是受管目录的唯一配置入口；数据库只保存运行时索引和扫描结果。
    因此普通用户查询时也会自动同步，避免必须先由管理员手动登记。
    """

    repository = ManagedFileRepository(db)
    roots: list[ManagedRoot] = []
    for configured_root_key in _configured_root_keys(root_key=root_key):
        container_path = _configured_container_path(configured_root_key)
        if container_path is None:
            continue
        existing_root = repository.get_root_by_key(configured_root_key)
        config_changed = existing_root is None or _root_config_changed(
            root=existing_root,
            display_name=configured_root_key,
            container_path=container_path,
            classification_mode=_configured_classification_mode(configured_root_key),
        )
        root = repository.upsert_root(
            root_key=configured_root_key,
            display_name=configured_root_key,
            container_path=container_path,
            classification_mode=_configured_classification_mode(configured_root_key),
            created_by=created_by,
        )
        roots.append(root)
        if scan and (config_changed or not _has_active_managed_files(db, root_id=root.id)):
            ManagedFileScanner(db).scan_root(root)
    db.flush()
    return roots


def _root_config_changed(
    *,
    root: ManagedRoot,
    display_name: str,
    container_path: str,
    classification_mode: str,
) -> bool:
    """判断 env 配置是否相对数据库索引发生变化。"""

    return (
        root.display_name != display_name
        or root.container_path != container_path
        or root.classification_mode != classification_mode
        or root.enabled is not True
        or root.read_only is not True
    )


def _has_active_managed_files(db: Session, *, root_id: str) -> bool:
    """判断受管目录是否已有可用文件索引，用于避免每次查询都全量扫描。"""

    return (
        db.query(ManagedFile.id)
        .filter(ManagedFile.root_id == root_id, ManagedFile.status == "ACTIVE")
        .first()
        is not None
    )


def _configured_root_keys(*, root_key: str | None = None) -> list[str]:
    """从环境变量中提取受管目录 root_key，忽略分类模式等元数据配置。"""

    if root_key:
        return [root_key] if _configured_container_path(root_key) is not None else []

    prefix = "MANAGED_ROOT_"
    ignored_suffixes = {"_CLASSIFICATION_MODE", "_NAME", "_DISPLAY_NAME"}
    keys: list[str] = []
    for env_key in os.environ:
        if not env_key.startswith(prefix):
            continue
        if any(env_key.endswith(suffix) for suffix in ignored_suffixes):
            continue
        keys.append(env_key[len(prefix):].lower())
    return sorted(set(keys))


def _configured_classification_mode(root_key: str) -> str:
    """读取受管目录分类模式；未配置或非法时默认 NONE。"""

    env_key = f"MANAGED_ROOT_{root_key.upper()}_CLASSIFICATION_MODE"
    value = os.getenv(env_key, "NONE").upper()
    if value not in {"NONE", "PATH_AS_CATEGORY"}:
        return "NONE"
    return value


def _require_role(current_user: User, allowed_roles: set[str]) -> None:
    """校验当前用户角色。"""

    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Insufficient role")
