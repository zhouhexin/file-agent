"""与 API 同步的 WorkBuddy 定向区域 OCR 强类型输入。"""

from typing import Literal
import re
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Region(StrictInput):
    region_id: Literal["cover_identity", "basic_profile", "annual_review"]
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(max_length=20000)


class FieldResult(StrictInput):
    column: Literal["B", "C", "D", "E", "F", "G", "H", "I"]
    value: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=1000)
    region_id: Literal["cover_identity", "basic_profile"]
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)


class AnnualReviewYear(StrictInput):
    """宿主先核对年份引文，服务器随后再与真实区域文字比对。"""
    year: int = Field(ge=1900, le=2099)
    quote: str = Field(min_length=1, max_length=1000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_quote(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("start/end 必须同时提供")
        if self.end is not None and self.end <= self.start:
            raise ValueError("end 必须大于 start")
        if str(self.year) not in self.quote or not re.search(r"考核.{0,8}优秀", self.quote):
            raise ValueError("年度引文必须明确包含该年份和考核优秀结论")
        return self


class AnnualReviewResult(StrictInput):
    """状态与年度列表互斥，防止无优秀时误写 L 列。"""
    status: Literal["EXCELLENT_FOUND", "NO_EXCELLENT", "UNREADABLE", "NOT_FOUND"]
    years: list[AnnualReviewYear] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def check_status(self):
        numbers = [item.year for item in self.years]
        if numbers != sorted(set(numbers)) or (self.status == "EXCELLENT_FOUND") != bool(numbers):
            raise ValueError("优秀年度必须唯一、升序，并与状态一致")
        return self


class ScopedSubmitPayload(StrictInput):
    worker_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=20, max_length=500)
    submission_key: str = Field(min_length=1, max_length=200)
    source_version_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_name: str = Field(min_length=1, max_length=120)
    provider_version: str | None = Field(default=None, max_length=120)
    regions: list[Region] = Field(min_length=2, max_length=3)
    fields: list[FieldResult] = Field(default_factory=list, max_length=8)
    annual_review: AnnualReviewResult | None = None

    @model_validator(mode="after")
    def validate_coverage(self):
        regions = {region.region_id for region in self.regions}
        if regions not in ({"cover_identity", "basic_profile"},
                           {"cover_identity", "basic_profile", "annual_review"}) or len(regions) != len(self.regions):
            raise ValueError("区域必须恰好覆盖冻结图像")
        if ("annual_review" in regions) != (self.annual_review is not None
                and self.annual_review.status != "NOT_FOUND"):
            raise ValueError("L 列结论必须与考核区域同时提交")
        columns = [field.column for field in self.fields]
        if len(columns) != len(set(columns)) or any(column not in "BCDEFGHI" for column in columns):
            raise ValueError("字段只能是不重复的 B–I 列")
        return self


class ReviewProbePayload(StrictInput):
    """只向服务端回传当前候选页的真实 OCR 结果。"""
    worker_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=20, max_length=500)
    submission_key: str = Field(min_length=1, max_length=200)
    page_number: int = Field(ge=1)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(max_length=30000)
    provider_name: str = Field(min_length=1, max_length=120)
    section_confirmed: bool = False
    heading_bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def validate_bbox(self):
        if self.section_confirmed and self.heading_bbox is None:
            raise ValueError("确认找到考核栏目时必须提交真实标题坐标")
        if self.heading_bbox is not None:
            left, top, right, bottom = self.heading_bbox
            if not (0 <= left < right <= 1 and 0.1 <= top < bottom <= 0.95
                    and right - left <= 0.8 and bottom - top <= 0.12):
                raise ValueError("标题坐标必须是页内真实的单行栏目标题矩形")
        return self


class ReviewRegionRejectPayload(StrictInput):
    """回报已冻结 L 图像缺少真实栏目，要求服务端恢复倒序定位。"""
    worker_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=20, max_length=500)
    submission_key: str = Field(min_length=1, max_length=200)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["HEADING_ABSENT", "SECTION_OUTSIDE_CROP"]
