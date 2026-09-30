"""与 API 同步的 WorkBuddy 定向区域 OCR 强类型输入。"""

from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Region(StrictInput):
    region_id: Literal["cover_identity", "basic_profile"]
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=20000)


class FieldResult(StrictInput):
    column: Literal["B", "C", "D", "E", "F", "G", "H", "I"]
    value: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=1000)
    region_id: Literal["cover_identity", "basic_profile"]
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)


class ScopedSubmitPayload(StrictInput):
    worker_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=20, max_length=500)
    submission_key: str = Field(min_length=1, max_length=200)
    source_version_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_name: str = Field(min_length=1, max_length=120)
    provider_version: str | None = Field(default=None, max_length=120)
    regions: list[Region] = Field(min_length=2, max_length=2)
    fields: list[FieldResult] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def validate_coverage(self):
        if {region.region_id for region in self.regions} != {"cover_identity", "basic_profile"}:
            raise ValueError("区域必须恰好覆盖两处冻结图像")
        if tuple(sorted(field.column for field in self.fields)) != tuple("BCDEFGHI"):
            raise ValueError("字段必须恰好覆盖 B–I")
        return self
