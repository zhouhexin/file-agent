"""受管目录 API schema。

所有对外响应只暴露 root_key 和 relative_path，不返回容器路径或宿主机绝对路径。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ManagedRootCreateRequest(BaseModel):
    """管理员启用部署层预定义目录的请求。"""

    model_config = ConfigDict(extra="forbid")

    root_key: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)


class ManagedRootResponse(BaseModel):
    """受管目录对外响应，不包含 container_path。"""

    id: str
    root_key: str
    display_name: str
    enabled: bool
    read_only: bool
    allowed_operations: list[str]


class FilesystemJobResponse(BaseModel):
    """文件系统异步任务响应。"""

    id: str
    job_type: str
    root_id: Optional[str]
    status: str
    progress_current: int
    progress_total: int
    result: dict
    error_message: Optional[str]


class ManagedFileResponse(BaseModel):
    """受管文件查询响应，只返回逻辑路径信息。"""

    root_key: str
    display_name: str
    relative_path: str
    filename: str
    extension: str
    size_bytes: int
    modified_at: Optional[datetime]
    status: str
