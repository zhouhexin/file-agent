"""B–I 与 L 定向 PDF 区域 OCR 契约；与完整文档契约隔离。"""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


LEGACY_CONTRACT = "workbuddy-scoped-pdf-v1"
CONTRACT = "workbuddy-scoped-pdf-v2"
DISCOVERY_CONTRACT = "workbuddy-scoped-pdf-v3"
FIXED_25_CONTRACT = "workbuddy-scoped-pdf-v4"
REQUIRED_COLUMNS = tuple("BCDEFGHI")
REVIEW_COLUMN = "L"


_EXCELLENT_RE = re.compile(
    r"(?P<years>(?:(?:19|20)\d{2}\s*年?(?:度)?\s*(?:[、，,及和]\s*)?){1,12})"
    r"考核(?:结果)?\s*(?:为|：|:)?\s*优秀"
)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_REVIEW_HEADING_RE = re.compile(r"任\s*现\s*职\s*以\s*来\s*的?\s*考\s*核\s*情\s*况")


def excellent_years(text: str) -> list[int]:
    """仅从明确写有年度考核优秀的短语取年份；合格等结论不入列。"""
    return sorted({int(year.group()) for phrase in _EXCELLENT_RE.finditer(text)
                   for year in _YEAR_RE.finditer(phrase.group("years"))})


def has_review_heading(text: str) -> bool:
    """年度证据必须来自指定栏目，不能误取其他页面的优秀奖项。"""
    return _REVIEW_HEADING_RE.search(text) is not None


def labeled_b_column_value(quote: str) -> str | None:
    """从带“单位/学院”标签的单行引文取出 B 列实际学院名称。"""

    match = re.fullmatch(r"\s*(?:单位|学院)[ \t\u3000]+([^\r\n]+?)\s*", quote)
    return match.group(1).strip() if match else None


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopedRegionResult(StrictInput):
    region_id: Literal["cover_identity", "basic_profile", "annual_review"]
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(max_length=20000)


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


class AnnualReviewYear(StrictInput):
    """每个优秀年度都要能回到同一块 OCR 原文中的明确优秀结论。"""
    year: int = Field(ge=1900, le=2099)
    quote: str = Field(min_length=1, max_length=1000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_range(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("start/end 必须同时提供")
        if self.end is not None and self.end <= self.start:
            raise ValueError("end 必须大于 start")
        if self.year not in excellent_years(self.quote):
            raise ValueError("年度引文必须明确证明该年度考核优秀")
        return self


class AnnualReviewResult(StrictInput):
    """没有优秀与 OCR 无法辨认分开记录，均不猜测年份。"""
    status: Literal["EXCELLENT_FOUND", "NO_EXCELLENT", "UNREADABLE", "NOT_FOUND"]
    years: list[AnnualReviewYear] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def check_status(self):
        numbers = [item.year for item in self.years]
        if (numbers != sorted(set(numbers)) or
                (self.status == "EXCELLENT_FOUND") != bool(numbers)):
            raise ValueError("优秀年度必须唯一、升序，并与状态一致")
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
    regions: list[ScopedRegionResult] = Field(min_length=2, max_length=3)
    fields: list[ScopedFieldResult] = Field(default_factory=list, max_length=8)
    annual_review: AnnualReviewResult | None = None

    @model_validator(mode="after")
    def validate_coverage(self):
        region_ids = {item.region_id for item in self.regions}
        if region_ids not in ({"cover_identity", "basic_profile"},
                              {"cover_identity", "basic_profile", "annual_review"}) or len(region_ids) != len(self.regions):
            raise ValueError("必须提交本契约冻结区域的真实 OCR 结果")
        if ("annual_review" in region_ids) != (self.annual_review is not None
                and self.annual_review.status != "NOT_FOUND"):
            raise ValueError("L 列年度结论必须与考核区域同时提交")
        columns = [item.column for item in self.fields]
        if len(columns) != len(set(columns)) or any(column not in REQUIRED_COLUMNS for column in columns):
            raise ValueError("PDF 提取字段只能是不重复的 B–I 列")
        return self


class ReviewProbeSubmit(StrictInput):
    """倒序候选页定位结果；图像与页号都由服务端签发。"""
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


class ReviewRegionReject(StrictInput):
    """冻结栏目图缺少真实考核正文时，允许受控回退到更早一页。"""
    worker_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=20, max_length=500)
    submission_key: str = Field(min_length=1, max_length=200)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["HEADING_ABSENT", "SECTION_OUTSIDE_CROP"]
