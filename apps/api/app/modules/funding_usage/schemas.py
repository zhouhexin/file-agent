"""受控成果资助使用登记任务输入。金额由服务端从 OCR 引文解析。"""

from uuid import UUID
from typing import Literal

from pydantic import Field, model_validator

from app.modules.structured_datasets.schemas import StrictInput


class SourceSnapshot(StrictInput):
    document_id: UUID
    document_version_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImageSnapshot(SourceSnapshot):
    extraction_run_id: UUID


class TextSpan(StrictInput):
    quote: str = Field(min_length=1, max_length=4000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def check_length(self):
        if self.end - self.start != len(self.quote):
            raise ValueError("引文长度与 start/end 不一致")
        return self


class AmountCandidate(TextSpan):
    quote: str = Field(min_length=1, max_length=80)
    decision: Literal["CONFIRMED_SPEND", "EXCLUDED", "UNCERTAIN"]
    reason: str = Field(min_length=1, max_length=300)


class UsageRow(StrictInput):
    page_number: int = Field(ge=1, le=20)
    name: str = Field(min_length=1, max_length=80)
    name_ref: TextSpan
    registration_ref: TextSpan | None = None
    no_usage: bool = False
    amounts: list[AmountCandidate] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def check_registration(self):
        if self.no_usage and any(item.decision != "EXCLUDED" for item in self.amounts):
            raise ValueError("无使用登记不能同时有支出或不确定金额")
        if not self.no_usage and self.registration_ref is None:
            raise ValueError("有使用登记必须提供可定位的登记引文；确认无登记时使用 no_usage")
        if self.amounts and self.registration_ref is None:
            raise ValueError("金额必须来自使用登记引文")
        return self


class FundingUsageRequest(StrictInput):
    image: ImageSnapshot
    workbook: SourceSnapshot
    filename: str = Field(min_length=6, max_length=240)
    request_key: str = Field(min_length=1, max_length=120)
    rows: list[UsageRow] = Field(min_length=1, max_length=100)


class FundingUsagePublish(FundingUsageRequest):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingUsageContinue(StrictInput):
    image: ImageSnapshot
    filename: str = Field(min_length=6, max_length=240)
    base_document_version_id: UUID
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)
    rows: list[UsageRow] = Field(min_length=1, max_length=100)


class FundingUsageContinuePublish(FundingUsageContinue):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingUsageRefresh(StrictInput):
    workbook: SourceSnapshot
    filename: str = Field(min_length=6, max_length=240)
    base_document_version_id: UUID
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)


class FundingUsageRefreshPublish(FundingUsageRefresh):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingSourceSummarySync(StrictInput):
    workbook: SourceSnapshot
    request_key: str = Field(min_length=1, max_length=120)


class FundingSourceSummarySyncPublish(FundingSourceSummarySync):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
