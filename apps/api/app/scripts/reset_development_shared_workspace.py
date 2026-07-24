"""重置共享工作目录开发数据的受控命令。

本工具只用于用户明确授权的开发环境：清空业务表、共享工作副本、回收站、上传
暂存和上传归档原件，并保留 ``MANAGED_ROOT_*`` 外部受管资料目录及 Alembic 版本。
必须使用显式确认参数，不能作为应用启动时的隐式副作用。
"""

from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal, engine
from app.db.base import Base
from app.db import models  # noqa: F401  # 注册业务表，供受控清空使用。
from app.modules.file_lifecycle.shared_workspace import get_or_create_shared_workspace


@dataclass(frozen=True)
class ResetTarget:
    """一个经过校验、允许清空内容的精确目录目标。"""

    label: str
    path: Path


def configured_external_managed_roots(environ: dict[str, str] | None = None) -> list[Path]:
    """读取部署层外部受管原始资料目录，作为永不删除的保护范围。"""

    values = environ or dict(os.environ)
    protected: list[Path] = []
    for key, value in values.items():
        if not key.startswith("MANAGED_ROOT_") or not value.strip():
            continue
        suffix = key.removeprefix("MANAGED_ROOT_")
        if suffix in {
            "ARCHIVE_WRITE_PATH",
            "ARCHIVE_ENABLED",
            "WATCH_ENABLED",
            "WATCH_POLL_SECONDS",
            "RECONCILE_INTERVAL_SECONDS",
            "RECONCILE_ON_STARTUP",
            "SCAN_BATCH_SIZE",
            "SCAN_BATCH_MAX_SECONDS",
        } or suffix.endswith("_CLASSIFICATION_MODE"):
            continue
        protected.append(Path(value).expanduser().resolve())
    return protected


def build_reset_targets(settings: Settings, *, project_root: Path) -> list[ResetTarget]:
    """构造开发重置的精确目录清单，禁止把整个 FILE_STORAGE_ROOT 当作目标。"""

    storage_root = Path(settings.file_storage_root).expanduser().resolve()
    return [
        ResetTarget("共享工作副本", Path(settings.working_copy_storage_root).expanduser().resolve()),
        ResetTarget("共享回收站", Path(settings.trash_storage_root).expanduser().resolve()),
        ResetTarget("上传暂存", storage_root / "uploads"),
        ResetTarget("隔离暂存", storage_root / "quarantine"),
        ResetTarget("临时处理目录", storage_root / "temp"),
        ResetTarget("上传归档原件", Path(settings.managed_root_archive_write_path).expanduser().resolve()),
    ]


def validate_reset_targets(targets: list[ResetTarget], *, project_root: Path, protected_roots: list[Path]) -> None:
    """校验删除目标不为空、不重叠外部资料、项目根或文件系统根。

    重置命令仅允许清空已经配置的精确目录内容；若归档目录与学校原始资料目录
    重叠，宁可停止并要求人工调整配置，也绝不能继续删除。
    """

    resolved_project = project_root.expanduser().resolve()
    seen: set[Path] = set()
    for target in targets:
        path = target.path.expanduser().resolve()
        if not str(path) or path == path.anchor or path == resolved_project:
            raise ValueError(f"重置目标不安全：{target.label}")
        if path in seen:
            raise ValueError(f"重置目标重复：{target.label}")
        seen.add(path)
        if _is_within(path, resolved_project) and path == resolved_project:
            raise ValueError(f"重置目标不能是项目根：{target.label}")
        for protected in protected_roots:
            if _overlaps(path, protected):
                raise ValueError(f"重置目标与外部受管原始资料目录重叠：{target.label}")


def clear_directory_contents(target: ResetTarget) -> None:
    """清空一个已验证目录的直接内容，不删除目录本身或追随目录外符号链接。"""

    path = target.path
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            raise RuntimeError(f"无法安全清理未知目录项：{target.label}")


def clear_business_tables(db: Session, *, database_engine: Engine) -> None:
    """清空应用业务表而保留 alembic_version，随后立即重建唯一共享工作区。"""

    table_names = [table.name for table in Base.metadata.sorted_tables]
    if database_engine.dialect.name == "postgresql":
        quoted = ", ".join(f'"{name}"' for name in table_names)
        if quoted:
            db.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
    else:
        # SQLite 测试环境没有 TRUNCATE；按逆依赖顺序删除，仍不触碰 alembic_version。
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
    get_or_create_shared_workspace(db)


def run_reset(*, settings: Settings, project_root: Path, db: Session, database_engine: Engine) -> list[str]:
    """执行已经授权且预校验通过的重置，并返回不含文件名的完成项。"""

    if not settings.managed_root_archive_write_path:
        raise ValueError("MANAGED_ROOT_ARCHIVE_WRITE_PATH 未配置，拒绝猜测上传归档原件位置")
    targets = build_reset_targets(settings, project_root=project_root)
    validate_reset_targets(
        targets,
        project_root=project_root,
        protected_roots=configured_external_managed_roots(),
    )
    # 必须先确认数据库可连接，再触碰任何本地目录；否则网络或认证故障会造成
    # “文件已清空、数据库仍保留旧索引”的半重置状态。
    db.connection()
    try:
        # PostgreSQL TRUNCATE 位于未提交事务内。目录清理失败时回滚数据库，避免
        # 把数据库清空而遗留旧文件索引；文件系统无法参与事务，所以仍要求停止服务。
        clear_business_tables(db, database_engine=database_engine)
        for target in targets:
            clear_directory_contents(target)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return [target.label for target in targets] + ["数据库业务数据（已保留 Alembic 版本）"]


def _is_within(path: Path, parent: Path) -> bool:
    """判断路径是否位于父路径内，用于保护项目和受管原始目录。"""

    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _overlaps(first: Path, second: Path) -> bool:
    """判断两个目录是否存在包含关系。"""

    return _is_within(first, second) or _is_within(second, first)


def main() -> None:
    """解析显式确认参数后执行开发重置。"""

    parser = argparse.ArgumentParser(description="重置 File Agent 共享工作目录开发数据")
    parser.add_argument(
        "--confirm-reset-shared-workspace",
        action="store_true",
        help="确认清空业务数据库、共享工作副本、上传暂存和上传归档原件",
    )
    args = parser.parse_args()
    if not args.confirm_reset_shared_workspace:
        parser.error("必须提供 --confirm-reset-shared-workspace；本命令不会隐式删除开发数据")
    settings = get_settings()
    project_root = Path.cwd().resolve()
    with SessionLocal() as db:
        completed = run_reset(settings=settings, project_root=project_root, db=db, database_engine=engine)
    print("开发重置完成：" + "、".join(completed))


if __name__ == "__main__":
    main()
