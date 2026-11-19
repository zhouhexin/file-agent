"""受控成果资助使用登记任务输入。金额由服务端从 OCR 引文解析。"""

from decimal import Decimal
import re
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


class FundingPromptRowEdit(StrictInput):
    action: Literal["ADD", "UPDATE", "DELETE"]
    name: str = Field(min_length=1, max_length=80)
    source_row_id: UUID | None = None
    after_row_id: UUID | None = None
    registration: str | None = Field(default=None, min_length=1, max_length=4000)
    date: str | None = Field(default=None, max_length=500)
    usage_amount: Decimal | None = Field(default=None, ge=0, le=1000000000, decimal_places=2)
    status: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_action(self):
        values = {"registration", "date", "usage_amount", "status"} & self.model_fields_set
        if self.action == "ADD":
            if self.source_row_id is not None or self.registration is None:
                raise ValueError("新增登记必须给出原文，且不能指定已有行 ID")
        elif self.action == "UPDATE":
            if self.source_row_id is None or not values or self.after_row_id is not None:
                raise ValueError("修改登记必须指定行 ID 和至少一个新字段")
        elif (self.source_row_id is None or values or self.after_row_id is not None):
            raise ValueError("删除登记只能指定已有行 ID")
        return self


class FundingPromptTotalEdit(StrictInput):
    name: str = Field(min_length=1, max_length=80)
    field: Literal["other_total"]
    value: Decimal | None = Field(ge=0, le=1000000000, decimal_places=2)


class FundingPromptNameEdit(StrictInput):
    old_name: str = Field(min_length=1, max_length=80)
    new_name: str = Field(min_length=1, max_length=31)


class FundingPromptPersonAdd(StrictInput):
    name: str = Field(min_length=1, max_length=31)
    other_total: Decimal | None = Field(default=None, ge=0, le=1000000000, decimal_places=2)


class FundingUsagePromptAddPeople(StrictInput):
    filename: str = Field(min_length=6, max_length=240)
    base_document_version_id: UUID
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)
    user_request: str = Field(min_length=1, max_length=4000)
    people: list[FundingPromptPersonAdd] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_people(self):
        # Excel 工作表名不区分大小写，避免 Alice/alice 生成两个无法对应的个人页。
        compact = ["".join(person.name.split()).casefold() for person in self.people]
        if any(not person.name.strip() or person.name != person.name.strip()
               or re.search(r'[\x00-\x1f\[\]:*?/\\]', person.name)
               or person.name == "\u4f7f\u7528\u767b\u8bb0\u6c47\u603b" for person in self.people):
            raise ValueError("新增教师姓名不能为空、不能带首尾空格，且必须可用作 Excel 工作表名")
        if len(compact) != len(set(compact)):
            raise ValueError("新增教师姓名不能重复")
        return self


class FundingPromptTarget(StrictInput):
    filename: str = Field(min_length=6, max_length=240)


class FundingUsagePromptEdit(StrictInput):
    filename: str = Field(min_length=6, max_length=240)
    base_document_version_id: UUID
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)
    user_request: str = Field(min_length=1, max_length=4000)
    row_edits: list[FundingPromptRowEdit] = Field(default_factory=list, max_length=100)
    total_edits: list[FundingPromptTotalEdit] = Field(default_factory=list, max_length=100)
    name_edits: list[FundingPromptNameEdit] = Field(default_factory=list, max_length=100)
    person_additions: list[FundingPromptPersonAdd] = Field(default_factory=list, max_length=100)
    person_deletions: list[str] = Field(default_factory=list, max_length=100,
                                        description="需从首页及个人明细页删除的教师姓名；必须先预览并确认。")

    @model_validator(mode="after")
    def check_changes(self):
        if (not self.row_edits and not self.total_edits and not self.name_edits
                and not self.person_additions and not self.person_deletions):
            raise ValueError("至少需要一项登记、资助金额或姓名变更")
        if (any(not name or name != name.strip() for name in self.person_deletions)
                or len(self.person_deletions) != len(set(self.person_deletions))):
            raise ValueError("删除教师名单不能为空或重复，姓名需与首页完全一致")
        return self


class FundingUsagePromptPublish(FundingUsagePromptEdit):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingSourceSummarySync(StrictInput):
    workbook: SourceSnapshot
    request_key: str = Field(min_length=1, max_length=120)


class FundingSourceSummarySyncPublish(FundingSourceSummarySync):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingSourceEntriesImport(StrictInput):
    workbook: SourceSnapshot
    filename: str = Field(min_length=6, max_length=240)
    request_key: str = Field(min_length=1, max_length=120)
    confirm_inferred_owner: bool = False


class FundingSourceEntriesImportPublish(FundingSourceEntriesImport):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
