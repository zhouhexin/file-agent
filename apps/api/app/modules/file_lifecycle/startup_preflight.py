"""Windows worker 启动前的受管目录配置同步与可访问性预检。

启动脚本必须先把当前机器 `.env` 中的受管目录写入运行时索引，再允许扫描
worker 领取历史任务。这样 Windows 不会使用数据库里残留的 macOS 或旧机器路径。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.models import FilesystemJob, ManagedRoot
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.service import (
    MANAGED_ROOT_GLOBAL_CONFIG_KEYS,
    sync_configured_managed_roots,
)


class WorkerStartupPreflightError(RuntimeError):
    """表示 worker 尚未启动时即可确定的安全配置错误。"""

    def __init__(self, *, code: str, root_key: str, message: str) -> None:
        """保存不包含绝对路径的错误码、逻辑根和运维提示。"""

        super().__init__(message)
        self.code = code
        self.root_key = root_key


@dataclass(frozen=True)
class WorkerStartupPreflightResult:
    """启动预检成功后的精简结果，不向控制台泄漏绝对路径。"""

    managed_root_keys: tuple[str, ...]

    @property
    def managed_root_count(self) -> int:
        """返回本次已经同步并校验的普通受管根数量。"""

        return len(self.managed_root_keys)


def prepare_worker_startup(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> WorkerStartupPreflightResult:
    """同步当前机器目录配置并验证可读性，然后原子提交数据库路径。

    `ManagedRoot.container_path` 是运行时索引，不是跨机器配置源。共享开发数据库
    可能保留另一台机器的绝对路径，因此必须在任何扫描 worker 启动前由当前 `.env`
    覆盖。验证失败时回滚，扫描 worker 不应启动或领取任务。
    """

    # 先触发项目根 `.env` 加载；受管目录枚举只允许读取已经进入进程环境的配置。
    get_settings()
    db = session_factory()
    try:
        _disable_legacy_pseudo_roots(db)
        roots = sync_configured_managed_roots(db, scan=False)
        for root in roots:
            _validate_managed_root(root_key=root.root_key, container_path=root.container_path)
        db.commit()
        return WorkerStartupPreflightResult(
            managed_root_keys=tuple(sorted(root.root_key for root in roots)),
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _disable_legacy_pseudo_roots(db: Session) -> None:
    """停用旧版本把全局配置键误登记成的伪目录及其待执行任务。

    旧代码可能把 `MANAGED_ROOT_SCAN_BATCH_SIZE=100` 登记为逻辑根
    `scan_batch_size`。这些记录不是真实用户目录，可以在 worker 启动前安全停用；
    上传归档根和其他正常受管根不受影响。
    """

    pseudo_root_keys = {
        env_key.removeprefix("MANAGED_ROOT_").lower()
        for env_key in MANAGED_ROOT_GLOBAL_CONFIG_KEYS
    }
    pseudo_roots = (
        db.query(ManagedRoot)
        .filter(ManagedRoot.root_key.in_(pseudo_root_keys))
        .all()
    )
    if not pseudo_roots:
        return

    queue = FilesystemJobQueue(db)
    for root in pseudo_roots:
        root.enabled = False
        pending_jobs = (
            db.query(FilesystemJob)
            .filter(
                FilesystemJob.root_id == root.id,
                FilesystemJob.job_type.in_(
                    {"RECONCILE_MANAGED_ROOT", "SCAN_MANAGED_ROOT"}
                ),
                FilesystemJob.status == "PENDING",
            )
            .all()
        )
        for job in pending_jobs:
            queue.mark_failed(
                job=job,
                error_message="旧版本误登记的全局配置项已停用，不再作为受管目录扫描。",
            )


def _validate_managed_root(*, root_key: str, container_path: str) -> None:
    """真实打开一个受管目录，区分不存在、类型错误和读取权限不足。

    `Path.exists()` 在部分 Windows 文件系统上会把权限异常表现为 False，因此这里
    使用 `stat` 和 `scandir` 两步验证，不能再把所有失败都笼统描述为“没有权限”。
    """

    root_path = Path(container_path)
    try:
        metadata = root_path.stat()
    except FileNotFoundError as exc:
        raise WorkerStartupPreflightError(
            code="MANAGED_ROOT_NOT_FOUND",
            root_key=root_key,
            message=f"MANAGED_ROOT_{root_key.upper()} 指向的目录不存在。",
        ) from exc
    except PermissionError as exc:
        raise WorkerStartupPreflightError(
            code="MANAGED_ROOT_PERMISSION_DENIED",
            root_key=root_key,
            message=f"当前 Windows 账户无权读取 MANAGED_ROOT_{root_key.upper()}。",
        ) from exc
    except OSError as exc:
        raise WorkerStartupPreflightError(
            code="MANAGED_ROOT_UNAVAILABLE",
            root_key=root_key,
            message=f"MANAGED_ROOT_{root_key.upper()} 暂时不可访问。",
        ) from exc

    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkerStartupPreflightError(
            code="MANAGED_ROOT_NOT_DIRECTORY",
            root_key=root_key,
            message=f"MANAGED_ROOT_{root_key.upper()} 配置的目标不是目录。",
        )

    try:
        # 打开枚举句柄即可验证目录读取能力；不在预检阶段遍历文件或计算哈希。
        with os.scandir(root_path):
            pass
    except PermissionError as exc:
        raise WorkerStartupPreflightError(
            code="MANAGED_ROOT_PERMISSION_DENIED",
            root_key=root_key,
            message=f"当前 Windows 账户无权枚举 MANAGED_ROOT_{root_key.upper()}。",
        ) from exc
    except OSError as exc:
        raise WorkerStartupPreflightError(
            code="MANAGED_ROOT_UNAVAILABLE",
            root_key=root_key,
            message=f"MANAGED_ROOT_{root_key.upper()} 无法打开。",
        ) from exc


def main() -> None:
    """执行一次启动预检；失败时用非零退出码阻止 CMD 继续启动 worker。"""

    try:
        result = prepare_worker_startup()
    except WorkerStartupPreflightError as exc:
        print(
            (
                "[File Agent Startup] 配置检查失败 "
                f"code={exc.code} root_key={exc.root_key} message={exc}"
            ),
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from exc
    except Exception as exc:
        # 数据库迁移、连接或未知启动错误只公开异常类型，详细连接信息不能进入控制台。
        print(
            (
                "[File Agent Startup] 初始化失败 "
                f"error_type={exc.__class__.__name__}，请检查数据库迁移和服务器日志。"
            ),
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from exc

    root_keys = ",".join(result.managed_root_keys) or "NONE"
    print(
        (
            "[File Agent Startup] 配置检查通过 "
            f"managed_roots={result.managed_root_count} root_keys={root_keys}"
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
