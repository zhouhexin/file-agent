"""B–I 专用 PDF 区域 OCR 契约；与完整文档契约隔离。"""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT = "workbuddy-scoped-pdf-v1"
REQUIRED_COLUMNS = tuple("BCDEFGHI")


def labeled_b_column_value(quote: str) -> str | None:
    """从带“单位/学院”标签的单行引文取出 B 列实际学院名称。"""

    match = re.fullmatch(r"\s*(?:单位|学院)[ \t\u3000]+([^\r\n]+?)\s*", quote)
    return match.group(1).strip() if match else None


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopedRegionResult(StrictInput):
    region_id: Literal["cover_identity", "basic_profile"]
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=20000)


class ScopedFieldResult(StrictInput):
    column: Literal["B", "C", "D", "E", "F", "G", "H", "I"]
    value: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=1000)
    region_id: Literal["cover_identity", "basic_profile"]
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_range(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("start/end 必须同时提供")
        if self.end is not None and self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


class ScopedSubmit(StrictInput):
    worker_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=20, max_length=500)
    submission_key: str = Field(min_length=1, max_length=200)
    source_version_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_name: str = Field(min_length=1, max_length=120)
    provider_version: str | None = Field(default=None, max_length=120)
    regions: list[ScopedRegionResult] = Field(min_length=2, max_length=2)
    fields: list[ScopedFieldResult] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def validate_coverage(self):
        if {item.region_id for item in self.regions} != {"cover_identity", "basic_profile"}:
            raise ValueError("必须提交两个冻结区域的真实 OCR 结果")
        if tuple(sorted(item.column for item in self.fields)) != REQUIRED_COLUMNS:
            raise ValueError("PDF 提取必须且只能覆盖 B–I 八列")
        return self
