"""分类主类更正与落位的严格命令、授权上下文和公开状态 schema。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_FORBIDDEN_SEGMENT_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class PlacementAction(StrEnum):
    """公开专用入口允许提交的两种动作。"""

    SET_PRIMARY = "SET_PRIMARY"
    MOVE = "MOVE"


class PlacementAuthorizationMode(StrEnum):
    """服务端核验后的授权来源。"""

    INITIAL_ORGANIZE = "INITIAL_ORGANIZE"
    EXPLICIT_REQUEST = "EXPLICIT_REQUEST"
    CONFIRMED_PLAN = "CONFIRMED_PLAN"
    RECOVERY = "RECOVERY"


class PlacementCommand(BaseModel):
    """客户端可提交的最小命令；额外授权或物理路径字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    working_copy_id: UUID
    action: PlacementAction
    expected_revision: int = Field(gt=0)
    expected_document_version_id: UUID
    target_category_id: str | None = Field(default=None, min_length=1, max_length=255)
    taxonomy_version: str = Field(min_length=1, max_length=80)
    container_segments: list[str] = Field(default_factory=list, max_length=20)
    target_root_key: str | None = Field(default=None, min_length=1, max_length=100)
    target_directory_segments: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("container_segments", "target_directory_segments")
    @classmethod
    def validate_segments(cls, value: list[str]) -> list[str]:
        """拒绝跨目录、隐藏、设备名和跨平台非法路径段。"""

        for segment in value:
            if not _is_safe_segment(segment):
                raise ValueError("目录段不合法")
        return value

    @model_validator(mode="after")
    def validate_target_mode(self) -> "PlacementCommand":
        """SET_PRIMARY 固定分类目标；MOVE 的分类目标与目录目标互斥。"""

        category_mode = self.target_category_id is not None
        directory_mode = self.target_root_key is not None
        if self.action == PlacementAction.SET_PRIMARY:
            if not category_mode:
                raise ValueError("SET_PRIMARY 必须提供 target_category_id")
            if directory_mode or self.target_directory_segments:
                raise ValueError("SET_PRIMARY 不接受目录目标")
            return self
        if category_mode == directory_mode:
            raise ValueError("MOVE 必须且只能提供分类目标或目录目标之一")
        if directory_mode and not self.target_directory_segments:
            raise ValueError("目录形式 MOVE 必须提供完整目标目录段")
        return self

    def request_digest(self) -> str:
        """计算规范请求摘要；幂等键相同但摘要不同必须冲突。"""

        payload = self.model_dump(mode="json", exclude={"idempotency_key"})
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class PlacementAuthorizationContext(BaseModel):
    """只由后端构造并传给提交服务的冻结授权事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    actor_user_id: UUID
    workspace_id: UUID
    client_id: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=1, max_length=120)
    scope: str = Field(min_length=1, max_length=80)
    source_event_ref: str = Field(min_length=1, max_length=160)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_mode: PlacementAuthorizationMode
    policy_version: str = Field(min_length=1, max_length=80)
    authorized_at: datetime
    conversation_id: UUID | None = None

    def audit_snapshot(self) -> dict[str, Any]:
        """输出可持久化的最小授权摘要，不复制原始聊天全文。"""

        return self.model_dump(mode="json")


class PlacementOperationState(StrEnum):
    """可恢复落位操作的完整状态机。"""

    PREPARED = "PREPARED"
    EXECUTING = "EXECUTING"
    FS_APPLIED = "FS_APPLIED"
    COMMITTED = "COMMITTED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    RECONCILING = "RECONCILING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def _is_safe_segment(value: str) -> bool:
    """执行与 Windows/POSIX 一致的便携目录段校验。"""

    segment = value.strip()
    if (
        not segment
        or segment in {".", ".."}
        or segment != value
        or segment.rstrip(" .") != segment
        or _FORBIDDEN_SEGMENT_CHARACTERS.search(segment)
        or segment.startswith(".")
    ):
        return False
    return segment.split(".", 1)[0].upper() not in _RESERVED_BASENAMES
