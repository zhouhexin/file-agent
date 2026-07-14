"""按请求构造文件重命名执行器。"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.modules.file_rename.executor_protocol import RenameExecutor, RenameExecutorError
from app.modules.file_rename.f2_executor import F2RenameExecutor
from app.modules.file_rename.native_executor import NativeRenameExecutor


def create_rename_executor(settings: Settings | None = None) -> RenameExecutor:
    """只允许 Native 或 F2，禁止未知执行器和隐式回退。"""

    effective_settings = settings or get_settings()
    executor_name = effective_settings.file_rename_executor.strip().lower()
    if executor_name == "native":
        return NativeRenameExecutor()
    if executor_name == "f2":
        executor = F2RenameExecutor(
            binary_path=effective_settings.f2_binary_path,
            expected_version=effective_settings.f2_expected_version,
            stdout_max_bytes=effective_settings.f2_stdout_max_bytes,
        )
        health = executor.health_check()
        if not health.available:
            if effective_settings.f2_fallback_to_native:
                return NativeRenameExecutor()
            raise RenameExecutorError(health.error_code or "F2_NOT_AVAILABLE", health.message)
        return executor
    raise RenameExecutorError("INVALID_RENAME_EXECUTOR", "文件重命名执行器配置不受支持。")
