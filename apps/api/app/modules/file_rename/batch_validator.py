"""Native 和 F2 共用的批量重命名安全校验。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from app.modules.file_rename.executor_protocol import RenameExecutorError
from app.modules.file_rename.schemas import RenameBatchRequest
from app.modules.managed_files.path_policy import PathPolicyError, resolve_managed_relative_path


_WINDOWS_FORBIDDEN_FILENAME_CHARACTERS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_WINDOWS_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class ValidatedRenamePaths:
    """通过边界校验的源路径和目标路径。"""

    source_path: Path
    target_path: Path


def validate_rename_mapping(
    *,
    root_path: Path,
    before_relative_path: str,
    after_relative_path: str,
) -> ValidatedRenamePaths:
    """验证单个映射只在受管根内修改 basename。"""

    if _is_hidden_path(before_relative_path) or _is_hidden_path(after_relative_path):
        raise RenameExecutorError("HIDDEN_FILE_NOT_ALLOWED", "隐藏文件不允许重命名。")
    before = Path(before_relative_path)
    after = Path(after_relative_path)
    if before.parent != after.parent:
        raise RenameExecutorError("MOVE_NOT_ALLOWED", "只允许在同一目录内修改文件名。")
    if before.suffix.lower() != after.suffix.lower():
        raise RenameExecutorError("EXTENSION_CHANGE_NOT_ALLOWED", "重命名不能改变文件扩展名。")
    if not _is_portable_target_filename(after.name):
        raise RenameExecutorError("INVALID_TARGET_FILENAME", "目标文件名不合法。")
    try:
        source_path = resolve_managed_relative_path(
            root_path=root_path,
            relative_path=before_relative_path,
        )
        target_path = resolve_managed_relative_path(
            root_path=root_path,
            relative_path=after_relative_path,
            must_exist=False,
        )
    except PathPolicyError as exc:
        raise RenameExecutorError("UNSAFE_MANAGED_PATH", str(exc)) from exc
    if not source_path.is_file():
        raise RenameExecutorError("SOURCE_NOT_FILE", "源路径不是普通文件。")
    if target_path.exists() and target_path != source_path:
        raise RenameExecutorError("TARGET_ALREADY_EXISTS", "目标文件已经存在。")
    return ValidatedRenamePaths(source_path=source_path, target_path=target_path)


def validate_rename_batch(request: RenameBatchRequest) -> list[ValidatedRenamePaths]:
    """验证批次内重复源、重复目标和每个安全映射。"""

    before_paths = [item.before_relative_path for item in request.items]
    after_paths = [item.after_relative_path for item in request.items]
    if len(set(before_paths)) != len(before_paths):
        raise RenameExecutorError("DUPLICATE_SOURCE", "重命名批次包含重复源文件。")
    if len(set(after_paths)) != len(after_paths):
        raise RenameExecutorError("DUPLICATE_TARGET", "重命名批次包含重复目标文件。")
    return [
        validate_rename_mapping(
            root_path=request.root_path,
            before_relative_path=item.before_relative_path,
            after_relative_path=item.after_relative_path,
        )
        for item in request.items
    ]


def _is_hidden_path(relative_path: str) -> bool:
    """判断任意相对路径段是否为隐藏项。"""

    return any(part.startswith(".") for part in Path(relative_path).parts)


def _is_portable_target_filename(filename: str) -> bool:
    """校验目标名称在 Windows 与 POSIX 上都可安全创建。

    OperationPlan 可能在不同操作系统执行，因而不能在 Linux 上接受双引号、问号、保留设备名等
    Windows 无法落盘的名称，否则确认后的真实执行会变成平台相关失败。
    """

    if not filename or filename in {".", ".."} or Path(filename).name != filename:
        return False
    if filename.rstrip(" .") != filename or _WINDOWS_FORBIDDEN_FILENAME_CHARACTERS.search(filename):
        return False
    basename = filename.split(".", 1)[0].upper()
    return basename not in _WINDOWS_RESERVED_BASENAMES
