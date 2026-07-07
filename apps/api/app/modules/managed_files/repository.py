"""受管目录数据库仓库。

封装 managed_roots、managed_files 和 filesystem_jobs 的基础读写，避免路由直接操作 ORM。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import FilesystemJob, FilesystemJobEvent, ManagedFile, ManagedRoot, utcnow


class ManagedFileRepository:
    """服务器受管目录和文件元数据仓库。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def get_root(self, root_id: str) -> ManagedRoot | None:
        """按 id 查询受管目录。"""

        return self.db.get(ManagedRoot, root_id)

    def get_root_by_key(self, root_key: str) -> ManagedRoot | None:
        """按 root_key 查询受管目录。"""

        return self.db.query(ManagedRoot).filter(ManagedRoot.root_key == root_key).one_or_none()

    def list_roots(self) -> list[ManagedRoot]:
        """列出全部受管目录。"""

        return self.db.query(ManagedRoot).order_by(ManagedRoot.created_at.asc()).all()

    def upsert_root(
        self,
        *,
        root_key: str,
        display_name: str,
        container_path: str,
        created_by: str | None,
    ) -> ManagedRoot:
        """创建或更新受管目录配置。"""

        root = self.get_root_by_key(root_key)
        if root is None:
            root = ManagedRoot(
                root_key=root_key,
                display_name=display_name,
                container_path=container_path,
                enabled=True,
                read_only=True,
                allowed_operations_json=["scan", "list", "search"],
                created_by=created_by,
            )
            self.db.add(root)
        else:
            root.display_name = display_name
            root.container_path = container_path
            root.enabled = True
            root.read_only = True
            root.updated_at = utcnow()
        self.db.flush()
        return root

    def list_files(
        self,
        *,
        root_key: str | None = None,
        extension: str | None = None,
        filename_contains: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[ManagedFile, ManagedRoot]]:
        """按逻辑目录和文件元数据查询受管文件。"""

        query = self.db.query(ManagedFile, ManagedRoot).join(ManagedRoot, ManagedFile.root_id == ManagedRoot.id)
        if root_key:
            query = query.filter(ManagedRoot.root_key == root_key)
        if extension:
            normalized_extension = extension if extension.startswith(".") else f".{extension}"
            query = query.filter(ManagedFile.extension == normalized_extension.lower())
        if filename_contains:
            query = query.filter(ManagedFile.filename.contains(filename_contains))
        if status:
            query = query.filter(ManagedFile.status == status)
        return (
            query.order_by(ManagedRoot.root_key.asc(), ManagedFile.relative_path.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )


class FilesystemJobRepository:
    """文件系统异步任务仓库。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def create_event(self, *, job_id: str, level: str, message: str, details: dict | None = None) -> FilesystemJobEvent:
        """记录文件系统任务事件。"""

        event = FilesystemJobEvent(
            job_id=job_id,
            level=level,
            message=message,
            details_json=details or {},
        )
        self.db.add(event)
        self.db.flush()
        return event

    def get_job(self, job_id: str) -> FilesystemJob | None:
        """按 id 查询任务。"""

        return self.db.get(FilesystemJob, job_id)
