"""受管目录路径安全策略。

所有服务器受管文件访问都必须使用 root_key + relative_path，本模块负责把相对路径
解析到已授权根目录内，并拒绝绝对路径、路径逃逸和符号链接逃逸。
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


class PathPolicyError(ValueError):
    """路径不符合受管目录安全策略。"""


def resolve_managed_relative_path(
    *,
    root_path: Path,
    relative_path: str,
    must_exist: bool = True,
) -> Path:
    """把受管目录相对路径解析成安全的容器内路径。"""

    if not relative_path or "\x00" in relative_path:
        raise PathPolicyError("relative_path 不能为空或包含空字节。")
    if _looks_absolute_or_windows_path(relative_path):
        raise PathPolicyError("relative_path 不能是绝对路径。")

    normalized_parts = Path(relative_path).parts
    if any(part == ".." for part in normalized_parts):
        raise PathPolicyError("relative_path 不能包含 ..。")

    root = root_path.resolve(strict=True) if root_path.exists() else root_path.resolve(strict=False)
    candidate = root.joinpath(*normalized_parts)
    if must_exist:
        if not candidate.exists():
            raise PathPolicyError("relative_path 指向的文件不存在。")
        resolved = candidate.resolve(strict=True)
    else:
        resolved = candidate.resolve(strict=False)

    if not _is_relative_to(resolved, root):
        raise PathPolicyError("relative_path 逃逸出受管根目录。")
    if candidate.is_symlink():
        raise PathPolicyError("relative_path 不能指向符号链接。")
    return resolved


def _looks_absolute_or_windows_path(value: str) -> bool:
    """判断输入是否像 POSIX 或 Windows 绝对路径。"""

    if os.path.isabs(value):
        return True
    windows_path = PureWindowsPath(value)
    return bool(windows_path.drive or windows_path.root)


def _is_relative_to(path: Path, root: Path) -> bool:
    """兼容旧 Python 风格的 relative_to 判断。"""

    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
