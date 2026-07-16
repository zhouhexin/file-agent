"""受管目录数据库仓库。

封装 managed_roots、managed_files 和 filesystem_jobs 的基础读写，避免路由直接操作 ORM。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from sqlalchemy import func, or_
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
        classification_mode: str,
        created_by: str | None,
        allow_rename: bool = False,
    ) -> ManagedRoot:
        """创建或更新受管目录配置。"""

        read_only = not allow_rename
        allowed_operations = ["scan", "list", "search"]
        if allow_rename:
            allowed_operations.append("rename")
        root = self.get_root_by_key(root_key)
        if root is None:
            root = ManagedRoot(
                root_key=root_key,
                display_name=display_name,
                container_path=container_path,
                classification_mode=classification_mode,
                enabled=True,
                read_only=read_only,
                allowed_operations_json=allowed_operations,
                created_by=created_by,
            )
            self.db.add(root)
        else:
            changed = (
                root.display_name != display_name
                or root.container_path != container_path
                or root.classification_mode != classification_mode
                or root.enabled is not True
                or root.read_only is not read_only
                or list(root.allowed_operations_json or []) != allowed_operations
            )
            if changed:
                root.display_name = display_name
                root.container_path = container_path
                root.classification_mode = classification_mode
                root.enabled = True
                root.read_only = read_only
                root.allowed_operations_json = allowed_operations
                root.updated_at = utcnow()
        self.db.flush()
        return root

    def list_files(
        self,
        *,
        root_key: str | None = None,
        root_keys: list[str] | None = None,
        path_prefix: str | None = None,
        extension: str | None = None,
        filename_contains: str | None = None,
        category_path: str | None = None,
        classification_mode: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[ManagedFile, ManagedRoot]]:
        """按逻辑目录和文件元数据查询受管文件。"""

        query = self.db.query(ManagedFile, ManagedRoot).join(ManagedRoot, ManagedFile.root_id == ManagedRoot.id)
        if root_key:
            query = query.filter(ManagedRoot.root_key == root_key)
        elif root_keys is not None:
            # env 是受管目录 source of truth；无 root_key 查询时只允许当前配置内的 root，避免旧数据库索引泄漏。
            if not root_keys:
                return []
            query = query.filter(ManagedRoot.root_key.in_(root_keys))
        if classification_mode:
            query = query.filter(ManagedRoot.classification_mode == classification_mode)
        if path_prefix:
            normalized_prefix = _normalize_path_prefix(path_prefix)
            escaped_prefix = _escape_like(normalized_prefix)
            query = query.filter(
                or_(
                    ManagedFile.relative_path == normalized_prefix,
                    ManagedFile.relative_path.like(f"{escaped_prefix}/%", escape="\\"),
                    # 用户可能只说末级目录名，例如“科学发展观下的文件”，真实路径可能是“党办/2026/科学发展观/...”
                    ManagedFile.relative_path.like(f"%/{escaped_prefix}/%", escape="\\"),
                )
            )
        if category_path:
            query = query.filter(ManagedFile.category_path == category_path)
        if extension:
            normalized_extension = extension if extension.startswith(".") else f".{extension}"
            query = query.filter(ManagedFile.extension == normalized_extension.lower())
        if filename_contains:
            # 用户说“2026年的文件”时，年份可能在文件名里，也可能是相对目录的一段。
            query = query.filter(
                or_(
                    ManagedFile.filename.contains(filename_contains, autoescape=True),
                    ManagedFile.relative_path.contains(filename_contains, autoescape=True),
                )
            )
        if status:
            query = query.filter(ManagedFile.status == status)
        query = _exclude_hidden_managed_paths(query)
        return (
            query.order_by(ManagedRoot.root_key.asc(), ManagedFile.relative_path.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def list_directory_paths(
        self,
        *,
        root_key: str | None = None,
        root_keys: list[str] | None = None,
    ) -> list[tuple[str, str]]:
        """从有效文件索引推导可供范围校验的真实相对目录。"""

        query = self.db.query(ManagedRoot.root_key, ManagedFile.relative_path).join(
            ManagedRoot,
            ManagedFile.root_id == ManagedRoot.id,
        )
        if root_key:
            query = query.filter(ManagedRoot.root_key == root_key)
        elif root_keys is not None:
            if not root_keys:
                return []
            query = query.filter(ManagedRoot.root_key.in_(root_keys))
        query = query.filter(ManagedFile.status == "ACTIVE")
        query = _exclude_hidden_managed_paths(query)

        directories: set[tuple[str, str]] = set()
        for matched_root_key, relative_path in query.all():
            parent_parts = PurePosixPath(str(relative_path)).parent.parts
            for depth in range(1, len(parent_parts) + 1):
                directory = PurePosixPath(*parent_parts[:depth]).as_posix()
                if directory not in {"", "."}:
                    directories.add((str(matched_root_key), directory))
        return sorted(directories)

    def list_category_paths(self, *, root_key: str | None = None) -> list[tuple[str, str, str, int]]:
        """列出已分类受管目录中的分类路径和文件数量。"""

        query = (
            self.db.query(
                ManagedRoot.root_key,
                ManagedRoot.display_name,
                ManagedFile.category_path,
                func.count(ManagedFile.id),
            )
            .join(ManagedRoot, ManagedFile.root_id == ManagedRoot.id)
            .filter(ManagedRoot.enabled.is_(True))
            .filter(ManagedRoot.classification_mode == "PATH_AS_CATEGORY")
            .filter(ManagedFile.status == "ACTIVE")
            .filter(ManagedFile.category_path.isnot(None))
        )
        query = _exclude_hidden_managed_paths(query)
        if root_key:
            query = query.filter(ManagedRoot.root_key == root_key)
        return (
            query.group_by(ManagedRoot.root_key, ManagedRoot.display_name, ManagedFile.category_path)
            .order_by(ManagedRoot.root_key.asc(), ManagedFile.category_path.asc())
            .all()
        )

    def count_files(
        self,
        *,
        root_key: str | None = None,
        root_keys: list[str] | None = None,
        path_prefix: str | None = None,
        extension: str | None = None,
        filename_contains: str | None = None,
        status: str | None = None,
    ) -> int:
        """按批量任务过滤条件统计非隐藏受管文件数量。"""

        query = self.db.query(func.count(ManagedFile.id)).join(
            ManagedRoot,
            ManagedFile.root_id == ManagedRoot.id,
        )
        if root_key:
            query = query.filter(ManagedRoot.root_key == root_key)
        elif root_keys is not None:
            if not root_keys:
                return 0
            query = query.filter(ManagedRoot.root_key.in_(root_keys))
        if path_prefix:
            normalized_prefix = _normalize_path_prefix(path_prefix)
            escaped_prefix = _escape_like(normalized_prefix)
            query = query.filter(
                or_(
                    ManagedFile.relative_path == normalized_prefix,
                    ManagedFile.relative_path.like(f"{escaped_prefix}/%", escape="\\"),
                    ManagedFile.relative_path.like(f"%/{escaped_prefix}/%", escape="\\"),
                )
            )
        if extension:
            normalized_extension = extension if extension.startswith(".") else f".{extension}"
            query = query.filter(ManagedFile.extension == normalized_extension.lower())
        if filename_contains:
            query = query.filter(
                or_(
                    ManagedFile.filename.contains(filename_contains, autoescape=True),
                    ManagedFile.relative_path.contains(filename_contains, autoescape=True),
                )
            )
        if status:
            query = query.filter(ManagedFile.status == status)
        query = _exclude_hidden_managed_paths(query)
        return int(query.scalar() or 0)

    def list_graph_folder_paths(
        self,
        *,
        root_key: str | None = None,
    ) -> list[tuple[str, str, str, str, int]]:
        """列出图谱目录投影所需路径，兼容确认分类和弱标签模式。"""

        query = (
            self.db.query(ManagedRoot, ManagedFile.relative_path)
            .join(ManagedFile, ManagedFile.root_id == ManagedRoot.id)
            .filter(ManagedRoot.enabled.is_(True))
            .filter(ManagedRoot.classification_mode.in_({"PATH_AS_CATEGORY", "PATH_AS_WEAK_LABEL"}))
            .filter(ManagedFile.status == "ACTIVE")
        )
        query = _exclude_hidden_managed_paths(query)
        if root_key:
            query = query.filter(ManagedRoot.root_key == root_key)

        counts: dict[tuple[str, str, str, str], int] = {}
        for root, relative_path in query.yield_per(1000):
            normalized_path = _normalize_path_prefix(str(relative_path or ""))
            parent = normalized_path.rsplit("/", 1)[0] if "/" in normalized_path else ""
            if not parent:
                continue
            key = (root.root_key, root.display_name, root.classification_mode, parent)
            counts[key] = counts.get(key, 0) + 1
        return [(*key, count) for key, count in sorted(counts.items())]


def _normalize_path_prefix(path_prefix: str) -> str:
    """把子目录前缀规范化为数据库中的 POSIX 相对路径。"""

    return path_prefix.replace("\\", "/").strip().strip("/")


def _escape_like(value: str) -> str:
    """转义 SQL LIKE 通配符，避免路径中的 %/_ 被当成通配符。"""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _exclude_hidden_managed_paths(query):
    """过滤任意路径段以点号开头的隐藏文件或隐藏目录。"""

    return query.filter(~ManagedFile.relative_path.like(".%")).filter(~ManagedFile.relative_path.like("%/.%"))


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
