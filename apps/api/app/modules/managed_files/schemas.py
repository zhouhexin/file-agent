"""受管目录 API schema。

所有对外响应只暴露 root_key 和 relative_path，不返回容器路径或宿主机绝对路径。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ManagedRootCreateRequest(BaseModel):
    """管理员启用部署层预定义目录的请求。"""

    model_config = ConfigDict(extra="forbid")

    root_key: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    classification_mode: Literal["NONE", "PATH_AS_CATEGORY", "PATH_AS_WEAK_LABEL"] = "NONE"


class ManagedRootResponse(BaseModel):
    """受管目录对外响应，不包含 container_path。"""

    id: str
    root_key: str
    display_name: str
    classification_mode: str
    enabled: bool
    read_only: bool
    allowed_operations: list[str]


class FilesystemJobResponse(BaseModel):
    """文件系统异步任务响应。"""

    id: str
    job_type: str
    queue_name: str
    root_id: Optional[str]
    status: str
    progress_current: int
    progress_total: int
    result: dict
    error_message: Optional[str]
    attempt_count: int
    max_attempts: int
    available_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class FilesystemJobEventResponse(BaseModel):
    """文件系统任务事件响应；事件只包含状态摘要，不包含文件正文或绝对路径。"""

    id: str
    job_id: str
    level: str
    message: str
    details: dict
    created_at: datetime


class ManagedFileResponse(BaseModel):
    """受管文件查询响应，只返回逻辑路径信息。"""

    root_key: str
    display_name: str
    relative_path: str
    category_path: Optional[str]
    filename: str
    extension: str
    size_bytes: int
    modified_at: Optional[datetime]
    status: str


class ManagedCategoryResponse(BaseModel):
    """已分类受管目录中的分类路径响应。"""

    root_key: str
    display_name: str
    category_path: str
    file_count: int
