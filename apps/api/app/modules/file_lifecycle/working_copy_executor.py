"""工作副本分类落位的无覆盖文件执行器。

本模块只处理已经冻结的相对路径和内容身份；它不知道分类、用户、OperationPlan
或数据库关系。实际文件变更使用同一文件系统上的 ``link + unlink``，而不是
``os.replace``，从而确保目标已存在时绝不覆盖。操作日志位于工作副本根内的受控
目录，供数据库提交中断后的恢复器验证文件身份。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.modules.file_lifecycle.storage import FileLifecycleStorageService


_JOURNAL_DIRECTORY = ".file-agent-operations"
_LOCK_DIRECTORY = ".file-agent-locks"


class WorkingCopyExecutionError(RuntimeError):
    """执行器的稳定业务错误；调用方不得把底层绝对路径回传给用户。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkingCopyFileIdentity:
    """可验证文件身份，不只依赖易碰撞的文件名或 SHA-256。"""

    sha256: str
    size_bytes: int
    device: int
    inode: int

    @classmethod
    def from_path(cls, path: Path) -> "WorkingCopyFileIdentity":
        """读取实际文件身份；目录、链接和不存在对象均不能作为工作副本。"""

        if not path.is_file():
            raise WorkingCopyExecutionError("SOURCE_FILE_MISSING", "工作副本文件不存在")
        stat = path.stat()
        return cls(
            sha256=FileLifecycleStorageService.sha256_file(path),
            size_bytes=int(stat.st_size),
            device=int(stat.st_dev),
            inode=int(stat.st_ino),
        )

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "WorkingCopyFileIdentity":
        """从冻结快照恢复身份，拒绝缺失或异常字段。"""

        try:
            sha256 = str(value["sha256"])
            size_bytes = int(value["size_bytes"])
            device = int(value["device"])
            inode = int(value["inode"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkingCopyExecutionError(
                "SOURCE_IDENTITY_INVALID", "冻结的工作副本身份不完整"
            ) from exc
        if len(sha256) != 64 or size_bytes < 0 or device < 0 or inode < 0:
            raise WorkingCopyExecutionError(
                "SOURCE_IDENTITY_INVALID", "冻结的工作副本身份不合法"
            )
        return cls(sha256=sha256, size_bytes=size_bytes, device=device, inode=inode)

    def as_json(self) -> dict[str, int | str]:
        """输出可写入操作快照和日志的身份摘要。"""

        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "device": self.device,
            "inode": self.inode,
        }

    def matches(self, actual: "WorkingCopyFileIdentity") -> bool:
        """必须同时匹配内容、长度与稳定文件身份，禁止仅按同 hash 认领。"""

        return self == actual


@dataclass(frozen=True, slots=True)
class WorkingCopyMoveResult:
    """物理发布结果，供事务 B 或恢复器判断下一阶段。"""

    operation_id: str
    source_relative_path: str
    target_relative_path: str
    target_identity: WorkingCopyFileIdentity
    performed_move: bool
    recovered_after_filesystem_apply: bool


class WorkingCopyExecutionLock(AbstractContextManager["WorkingCopyExecutionLock"]):
    """跨进程工作副本互斥锁。

    锁由操作系统在进程退出时释放；租约过期的旧 worker 若仍在运行会继续持锁，
    新 worker 因而不能仅凭数据库租约并发写入同一副本。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any | None = None

    def __enter__(self) -> "WorkingCopyExecutionLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise WorkingCopyExecutionError(
                        "PLACEMENT_LOCK_UNAVAILABLE", "工作副本正由其他执行器处理"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise WorkingCopyExecutionError(
                        "PLACEMENT_LOCK_UNAVAILABLE", "工作副本正由其他执行器处理"
                    ) from exc
        except Exception:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        return None


class WorkingCopyExecutor:
    """在已授权、已冻结的源/目标之间执行无覆盖移动及恢复。"""

    def __init__(self, storage: FileLifecycleStorageService | None = None) -> None:
        self.storage = storage or FileLifecycleStorageService()

    def capture_source_identity(
        self,
        *,
        root_relative_path: str,
        source_relative_path: str,
    ) -> WorkingCopyFileIdentity:
        """事务 A 读取当前受控工作副本的稳定身份。"""

        source = self._working_path(root_relative_path, source_relative_path)
        return WorkingCopyFileIdentity.from_path(source)

    def capture_or_recover_source_identity(
        self,
        *,
        operation_id: str,
        root_relative_path: str,
        source_relative_path: str,
        target_relative_path: str,
    ) -> WorkingCopyFileIdentity:
        """读取源身份；源已被同一操作发布时仅从匹配的私有日志恢复。

        首次发布在文件系统提交和数据库提交之间崩溃时，暂存源文件已经不存在。
        这时不能只凭目标文件 hash 继续，而要先核对该操作的 journal 中冻结的
        source/target 和完整文件身份，之后再由 ``apply_move`` 复核目标 inode。
        """

        try:
            return self.capture_source_identity(
                root_relative_path=root_relative_path,
                source_relative_path=source_relative_path,
            )
        except WorkingCopyExecutionError as exc:
            if exc.code != "SOURCE_FILE_MISSING":
                raise
        journal_path = self._journal_path(root_relative_path, operation_id)
        try:
            loaded = json.loads(journal_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or (
                loaded.get("operation_id") != operation_id
                or loaded.get("source_relative_path") != source_relative_path
                or loaded.get("target_relative_path") != target_relative_path
            ):
                raise ValueError("journal mismatch")
            return WorkingCopyFileIdentity.from_json(
                dict(loaded.get("source_identity") or {})
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as recovery_error:
            raise WorkingCopyExecutionError(
                "PLACEMENT_RECONCILIATION_REQUIRED",
                "暂存源文件已不存在，且没有可验证的发布身份日志",
            ) from recovery_error

    def apply_move(
        self,
        *,
        operation_id: str,
        working_copy_id: str,
        root_relative_path: str,
        source_relative_path: str,
        target_relative_path: str,
        expected_identity: WorkingCopyFileIdentity,
    ) -> WorkingCopyMoveResult:
        """写入发布意图后执行不覆盖移动，并可收敛于已发生的同一文件动作。"""

        source = self._working_path(root_relative_path, source_relative_path)
        target = self._working_path(root_relative_path, target_relative_path)
        if source == target:
            actual = WorkingCopyFileIdentity.from_path(source)
            if not expected_identity.matches(actual):
                raise WorkingCopyExecutionError(
                    "SOURCE_IDENTITY_CHANGED", "工作副本内容或身份已变化"
                )
            return WorkingCopyMoveResult(
                operation_id=operation_id,
                source_relative_path=source_relative_path,
                target_relative_path=target_relative_path,
                target_identity=actual,
                performed_move=False,
                recovered_after_filesystem_apply=False,
            )

        journal_path = self._journal_path(root_relative_path, operation_id)
        lock_path = self._lock_path(root_relative_path, working_copy_id)
        with WorkingCopyExecutionLock(lock_path):
            journal = self._load_or_create_journal(
                path=journal_path,
                operation_id=operation_id,
                source_relative_path=source_relative_path,
                target_relative_path=target_relative_path,
                expected_identity=expected_identity,
            )
            source_exists = source.is_file()
            target_exists = target.is_file()
            if source_exists and target_exists:
                source_identity = WorkingCopyFileIdentity.from_path(source)
                target_identity = WorkingCopyFileIdentity.from_path(target)
                if (
                    expected_identity.matches(source_identity)
                    and expected_identity.matches(target_identity)
                    and source_identity.device == target_identity.device
                    and source_identity.inode == target_identity.inode
                ):
                    # link 成功后、unlink 前中断：目标可由同一 inode 和预写日志证明归属。
                    self._write_journal(journal_path, {**journal, "phase": "TARGET_LINKED"})
                    self._unlink_source(source)
                    self._write_journal(journal_path, {**journal, "phase": "FS_APPLIED"})
                    return self._result(
                        operation_id=operation_id,
                        source_relative_path=source_relative_path,
                        target_relative_path=target_relative_path,
                        target_identity=target_identity,
                        performed_move=False,
                        recovered=True,
                    )
                raise WorkingCopyExecutionError(
                    "PLACEMENT_RECONCILIATION_REQUIRED",
                    "源和目标同时存在，不能根据文件名或相同哈希自动认领",
                )
            if not source_exists and target_exists:
                target_identity = WorkingCopyFileIdentity.from_path(target)
                if not expected_identity.matches(target_identity):
                    raise WorkingCopyExecutionError(
                        "PLACEMENT_RECONCILIATION_REQUIRED",
                        "目标文件身份与冻结操作不一致",
                    )
                self._write_journal(journal_path, {**journal, "phase": "FS_APPLIED"})
                return self._result(
                    operation_id=operation_id,
                    source_relative_path=source_relative_path,
                    target_relative_path=target_relative_path,
                    target_identity=target_identity,
                    performed_move=False,
                    recovered=True,
                )
            if not source_exists and not target_exists:
                raise WorkingCopyExecutionError(
                    "PLACEMENT_RECONCILIATION_REQUIRED",
                    "源和目标均不存在，无法安全恢复工作副本",
                )

            source_identity = WorkingCopyFileIdentity.from_path(source)
            if not expected_identity.matches(source_identity):
                raise WorkingCopyExecutionError(
                    "SOURCE_IDENTITY_CHANGED", "工作副本内容或身份已变化"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            self._write_journal(journal_path, {**journal, "phase": "INTENT"})
            try:
                # ``link`` 对既有目标以 EEXIST 失败；随后 unlink 源文件，所以从不覆盖。
                os.link(source, target)
            except FileExistsError as exc:
                raise WorkingCopyExecutionError(
                    "TARGET_NAME_CONFLICT", "目标工作副本路径已存在"
                ) from exc
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise WorkingCopyExecutionError(
                        "CROSS_DEVICE_MOVE_UNSUPPORTED", "分类落位只支持同一文件系统移动"
                    ) from exc
                if exc.errno in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP}:
                    raise WorkingCopyExecutionError(
                        "FILE_PUBLISH_CAPABILITY_UNAVAILABLE",
                        "当前文件系统不支持安全的无覆盖发布"
                    ) from exc
                raise
            target_identity = WorkingCopyFileIdentity.from_path(target)
            if not expected_identity.matches(target_identity):
                raise WorkingCopyExecutionError(
                    "PLACEMENT_RECONCILIATION_REQUIRED",
                    "发布后的目标文件身份无法验证",
                )
            self._write_journal(journal_path, {**journal, "phase": "TARGET_LINKED"})
            self._unlink_source(source)
            self._write_journal(journal_path, {**journal, "phase": "FS_APPLIED"})
            return self._result(
                operation_id=operation_id,
                source_relative_path=source_relative_path,
                target_relative_path=target_relative_path,
                target_identity=target_identity,
                performed_move=True,
                recovered=False,
            )

    def _result(
        self,
        *,
        operation_id: str,
        source_relative_path: str,
        target_relative_path: str,
        target_identity: WorkingCopyFileIdentity,
        performed_move: bool,
        recovered: bool,
    ) -> WorkingCopyMoveResult:
        return WorkingCopyMoveResult(
            operation_id=operation_id,
            source_relative_path=source_relative_path,
            target_relative_path=target_relative_path,
            target_identity=target_identity,
            performed_move=performed_move,
            recovered_after_filesystem_apply=recovered,
        )

    def _working_path(self, root_relative_path: str, relative_path: str) -> Path:
        root = _safe_relative_path(root_relative_path)
        item = _safe_relative_path(relative_path)
        if root is None or item is None:
            raise WorkingCopyExecutionError("PATH_INVALID", "工作副本相对路径不完整")
        return self.storage.working_copy_path(f"{root}/{item}")

    def _journal_path(self, root_relative_path: str, operation_id: str) -> Path:
        return self._working_path(root_relative_path, f"{_JOURNAL_DIRECTORY}/{operation_id}.json")

    def _lock_path(self, root_relative_path: str, working_copy_id: str) -> Path:
        lock_key = hashlib.sha256(working_copy_id.encode("utf-8")).hexdigest()
        return self._working_path(root_relative_path, f"{_LOCK_DIRECTORY}/{lock_key}.lock")

    def _load_or_create_journal(
        self,
        *,
        path: Path,
        operation_id: str,
        source_relative_path: str,
        target_relative_path: str,
        expected_identity: WorkingCopyFileIdentity,
    ) -> dict[str, Any]:
        expected = {
            "schema_version": "working-copy-placement-journal-v1",
            "operation_id": operation_id,
            "source_relative_path": source_relative_path,
            "target_relative_path": target_relative_path,
            "source_identity": expected_identity.as_json(),
        }
        if not path.exists():
            self._write_journal(path, {**expected, "phase": "PREPARED"})
            return expected
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkingCopyExecutionError(
                "PLACEMENT_RECONCILIATION_REQUIRED", "操作身份日志不可读取"
            ) from exc
        if not isinstance(loaded, dict) or any(loaded.get(key) != value for key, value in expected.items()):
            raise WorkingCopyExecutionError(
                "PLACEMENT_RECONCILIATION_REQUIRED", "操作身份日志与冻结操作不一致"
            )
        return loaded

    @staticmethod
    def _write_journal(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # 仅替换本操作私有的日志文件；不用于用户工作副本文件。
        os.replace(temporary, path)

    @staticmethod
    def _unlink_source(source: Path) -> None:
        try:
            source.unlink()
        except FileNotFoundError:
            # 已发生实际移动的重试会进入 target-only 路径；这里同样保持幂等。
            return


def _safe_relative_path(value: str) -> str | None:
    """只接受工作副本根内的规范相对路径。

    StorageService 会在最终解析时再次阻止路径穿越；这里在执行器边界提前拒绝，
    避免内部日志或锁文件因错误的 operation 快照被创建到意外位置。
    """

    normalized = str(value or "").replace("\\", "/").strip("/")
    if not normalized:
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()
