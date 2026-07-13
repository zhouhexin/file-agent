"""确认后执行受管文件原地重命名。"""

from __future__ import annotations

from pathlib import Path

from app.modules.managed_files.path_policy import PathPolicyError, resolve_managed_relative_path


class NativeRenameError(RuntimeError):
    """Native 重命名校验或执行失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NativeRenameExecutor:
    """使用 Python 文件系统 API 执行同目录 basename 重命名。"""

    def preview(
        self,
        *,
        root_path: Path,
        before_relative_path: str,
        after_relative_path: str,
    ) -> tuple[Path, Path]:
        """校验源和目标路径，不修改文件。"""

        if _is_hidden_path(before_relative_path) or _is_hidden_path(after_relative_path):
            raise NativeRenameError("HIDDEN_FILE_NOT_ALLOWED", "隐藏文件不允许重命名。")
        before = Path(before_relative_path)
        after = Path(after_relative_path)
        if before.parent != after.parent:
            raise NativeRenameError("MOVE_NOT_ALLOWED", "第一版只允许同一目录内修改文件名。")
        if before.suffix.lower() != after.suffix.lower():
            raise NativeRenameError("EXTENSION_CHANGE_NOT_ALLOWED", "重命名不能改变文件扩展名。")
        if not after.name or after.name in {".", ".."} or Path(after.name).name != after.name:
            raise NativeRenameError("INVALID_TARGET_FILENAME", "目标文件名不合法。")
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
            raise NativeRenameError("UNSAFE_MANAGED_PATH", str(exc)) from exc
        if not source_path.is_file():
            raise NativeRenameError("SOURCE_NOT_FILE", "源路径不是普通文件。")
        if target_path.exists() and target_path != source_path:
            raise NativeRenameError("TARGET_ALREADY_EXISTS", "目标文件已经存在。")
        return source_path, target_path

    def execute(
        self,
        *,
        root_path: Path,
        before_relative_path: str,
        after_relative_path: str,
    ) -> tuple[Path, Path]:
        """校验后执行原子同目录 rename。"""

        source_path, target_path = self.preview(
            root_path=root_path,
            before_relative_path=before_relative_path,
            after_relative_path=after_relative_path,
        )
        if source_path == target_path:
            return source_path, target_path
        source_path.rename(target_path)
        return source_path, target_path

    def compensate(self, *, source_path: Path, target_path: Path) -> None:
        """数据库写入失败时尽力恢复原文件名。"""

        if target_path.exists() and not source_path.exists():
            target_path.rename(source_path)


def _is_hidden_path(relative_path: str) -> bool:
    """判断任意相对路径段是否为隐藏项。"""

    return any(part.startswith(".") for part in Path(relative_path).parts)

