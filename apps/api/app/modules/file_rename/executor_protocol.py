"""文件重命名执行器的统一协议。"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.modules.file_rename.schemas import RenameBatchRequest, RenameBatchResult


class RenameExecutorError(RuntimeError):
    """执行器校验、预演或执行失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RenameExecutorHealth(BaseModel):
    """执行器健康检查结果，不包含本地绝对路径。"""

    model_config = ConfigDict(extra="forbid")

    executor: str
    available: bool
    version: str = ""
    error_code: str | None = None
    message: str = ""


class RenameExecutor(Protocol):
    """批量重命名执行器必须实现的受控接口。"""

    name: str

    def health_check(self) -> RenameExecutorHealth:
        """检查执行器是否可用。"""

    def preview_batch(self, request: RenameBatchRequest) -> RenameBatchResult:
        """预演完整批次，不修改文件。"""

    def execute_batch(self, request: RenameBatchRequest) -> RenameBatchResult:
        """执行已经确认并重新校验的批次。"""

    def compensate_batch(
        self,
        request: RenameBatchRequest,
        result: RenameBatchResult,
    ) -> RenameBatchResult:
        """按执行完成顺序的逆序补偿成功项。"""
