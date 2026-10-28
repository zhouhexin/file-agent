"""WorkBuddy 成果资助手写登记预览和发布工具。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from mcp.types import CallToolResult, ResourceLink, TextContent
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .client import FileAgentIntegrationClient


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


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


class FundingUsagePayload(StrictInput):
    image: ImageSnapshot
    workbook: SourceSnapshot
    filename: str = Field(min_length=6, max_length=240)
    request_key: str = Field(min_length=1, max_length=120)
    rows: list[UsageRow] = Field(min_length=1, max_length=100)


class FundingUsagePublishPayload(FundingUsagePayload):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingUsageContinuePayload(StrictInput):
    image: ImageSnapshot
    filename: str = Field(min_length=6, max_length=240)
    base_document_version_id: UUID
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)
    rows: list[UsageRow] = Field(min_length=1, max_length=100)


class FundingUsageContinuePublishPayload(FundingUsageContinuePayload):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingUsageRefreshPayload(StrictInput):
    workbook: SourceSnapshot
    filename: str = Field(min_length=6, max_length=240)
    base_document_version_id: UUID
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)


class FundingUsageRefreshPublishPayload(FundingUsageRefreshPayload):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingPromptTargetPayload(StrictInput):
    filename: str = Field(min_length=6, max_length=240)


class FundingPromptRowEditPayload(StrictInput):
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


class FundingPromptTotalEditPayload(StrictInput):
    name: str = Field(min_length=1, max_length=80)
    field: Literal["research_total", "other_total"]
    value: Decimal | None = Field(ge=0, le=1000000000, decimal_places=2)


class FundingPromptNameEditPayload(StrictInput):
    old_name: str = Field(min_length=1, max_length=80)
    new_name: str = Field(min_length=1, max_length=31)


class FundingUsagePromptPayload(StrictInput):
    filename: str = Field(min_length=6, max_length=240)
    base_document_version_id: UUID
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)
    user_request: str = Field(min_length=1, max_length=4000)
    row_edits: list[FundingPromptRowEditPayload] = Field(default_factory=list, max_length=100)
    total_edits: list[FundingPromptTotalEditPayload] = Field(default_factory=list, max_length=100)
    name_edits: list[FundingPromptNameEditPayload] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check_changes(self):
        if not self.row_edits and not self.total_edits and not self.name_edits:
            raise ValueError("至少需要一项登记、资助金额或姓名变更")
        return self


class FundingUsagePromptPublishPayload(FundingUsagePromptPayload):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingSourceSummarySyncPayload(StrictInput):
    workbook: SourceSnapshot
    request_key: str = Field(min_length=1, max_length=120)


class FundingSourceSummarySyncPublishPayload(FundingSourceSummarySyncPayload):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FundingSourceEntriesImportPayload(StrictInput):
    workbook: SourceSnapshot
    filename: str = Field(min_length=6, max_length=240)
    request_key: str = Field(min_length=1, max_length=120)
    confirm_inferred_owner: bool = False


class FundingSourceEntriesImportPublishPayload(FundingSourceEntriesImportPayload):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def register_funding_usage_tools(mcp, client_factory, download_dir):
    @mcp.tool(name="funding_source_entries_preview", description=(
        "预览本轮上传单人资助 Excel 第 1 张论文页、第 3 张会议页向同名教师成果明细汇总表的追加；"
        "逐行报告重复、缺字段和申请人推断，不修改文件。"), structured_output=True)
    async def source_entries_preview(payload: FundingSourceEntriesImportPayload) -> dict[str, Any]:
        client: FileAgentIntegrationClient = client_factory()
        try:
            return await client.funding_usage_request(
                "POST", "/source-entries/preview", payload.model_dump(mode="json"))
        finally:
            await client.close()

    @mcp.tool(name="funding_source_entries_publish", description=(
        "只发布与预览一致的单人资助新增成果；同步个人页与汇总页并返回同名新版本。"
        "缺申请人关联必须显式确认。"), structured_output=False)
    async def source_entries_publish(payload: FundingSourceEntriesImportPublishPayload):
        client: FileAgentIntegrationClient = client_factory()
        try:
            result = await client.funding_usage_request(
                "POST", "/source-entries/publish", payload.model_dump(mode="json"))
            if not result.get("excel_published") or result.get("status") != "COMPLETED":
                return result
            resource = await client.download_dataset_export(
                dataset_id=result["dataset_id"], export_id=result["export_id"],
                output_dir=Path(download_dir()))
            receipt = {**result, "download_resource_uri": resource["resource_uri"]}
            return CallToolResult(content=[
                TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False)),
                ResourceLink(type="resource_link", name=result["filename"],
                             uri=resource["resource_uri"],
                             mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             size=resource["size_bytes"])], structuredContent=receipt)
        finally:
            await client.close()

    @mcp.tool(name="funding_source_summary_preview", description=(
        "核对已归档教师成果明细表个人页合计与汇总页资助总额的差异；仅预览，不修改文件。"),
        structured_output=True)
    async def source_summary_preview(payload: FundingSourceSummarySyncPayload) -> dict[str, Any]:
        client: FileAgentIntegrationClient = client_factory()
        try:
            return await client.funding_usage_request(
                "POST", "/source-summary/preview", payload.model_dump(mode="json"))
        finally:
            await client.close()

    @mcp.tool(name="funding_source_summary_publish", description=(
        "按已核对的个人成果页合计，同步来源表汇总页资助总额并发布来源表同名新版本。"),
        structured_output=False)
    async def source_summary_publish(payload: FundingSourceSummarySyncPublishPayload):
        client: FileAgentIntegrationClient = client_factory()
        try:
            result = await client.funding_usage_request(
                "POST", "/source-summary/publish", payload.model_dump(mode="json"))
            if not result.get("excel_published") or result.get("status") != "COMPLETED":
                return result
            resource = await client.download_dataset_export(
                dataset_id=result["dataset_id"], export_id=result["export_id"],
                output_dir=Path(download_dir()))
            receipt = {**result, "download_resource_uri": resource["resource_uri"]}
            return CallToolResult(content=[
                TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False)),
                ResourceLink(type="resource_link", name=result["filename"],
                             uri=resource["resource_uri"],
                             mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             size=resource["size_bytes"])], structuredContent=receipt)
        finally:
            await client.close()

    @mcp.tool(name="funding_usage_preview", description=(
        "核对已归档图片 OCR 的姓名、手写使用登记和金额引文，只读旧 Excel 汇总资助总额；"
        "返回使用金额与余额预览。印刷金额绝不能作为使用金额。"), structured_output=True)
    async def preview(payload: FundingUsagePayload) -> dict[str, Any]:
        client: FileAgentIntegrationClient = client_factory()
        try:
            return await client.funding_usage_request("POST", "/preview", payload.model_dump(mode="json"))
        finally:
            await client.close()

    @mcp.tool(name="funding_usage_publish", description=(
        "仅发布与 funding_usage_preview 指纹一致的手写登记结果。"
        "服务端重验 OCR 引文与旧 Excel 金额，生成新多工作表 XLSX 与正式可下载版本。"),
        structured_output=False)
    async def publish(payload: FundingUsagePublishPayload):
        client: FileAgentIntegrationClient = client_factory()
        try:
            result = await client.funding_usage_request("POST", "/publish", payload.model_dump(mode="json"))
            if not result.get("excel_published") or result.get("status") != "COMPLETED":
                return result
            resource = await client.download_dataset_export(
                dataset_id=result["dataset_id"], export_id=result["export_id"],
                output_dir=Path(download_dir()))
            receipt = {**result, "download_resource_uri": resource["resource_uri"]}
            return CallToolResult(content=[
                TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False)),
                ResourceLink(type="resource_link", name=result["filename"],
                             uri=resource["resource_uri"],
                             mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             size=resource["size_bytes"])],
                structuredContent=receipt)
        finally:
            await client.close()

    @mcp.tool(name="funding_usage_continue_preview", description=(
        "将新图片登记预览为同名已发布成果资助余额表的新增行。"
        "从当前版本余额继续扣减，检查重复来源与版本冲突。"), structured_output=True)
    async def continue_preview(payload: FundingUsageContinuePayload) -> dict[str, Any]:
        client: FileAgentIntegrationClient = client_factory()
        try:
            return await client.funding_usage_request(
                "POST", "/continue/preview", payload.model_dump(mode="json"))
        finally:
            await client.close()

    @mcp.tool(name="funding_usage_continue_publish", description=(
        "只发布与成果资助追加预览指纹一致的新增登记，"
        "保留旧行并更新同一 Excel 的个人页、余额和文件版本。"), structured_output=False)
    async def continue_publish(payload: FundingUsageContinuePublishPayload):
        client: FileAgentIntegrationClient = client_factory()
        try:
            result = await client.funding_usage_request(
                "POST", "/continue/publish", payload.model_dump(mode="json"))
            if not result.get("excel_published") or result.get("status") != "COMPLETED":
                return result
            resource = await client.download_dataset_export(
                dataset_id=result["dataset_id"], export_id=result["export_id"],
                output_dir=Path(download_dir()))
            receipt = {**result, "download_resource_uri": resource["resource_uri"]}
            return CallToolResult(content=[
                TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False)),
                ResourceLink(type="resource_link", name=result["filename"],
                             uri=resource["resource_uri"],
                             mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             size=resource["size_bytes"])], structuredContent=receipt)
        finally:
            await client.close()

    @mcp.tool(name="funding_usage_refresh_preview", description=(
        "按本轮归档的新版教师成果明细汇总表资助总额，预览同名已发布余额表每位教师的更新；"
        "支出仅从余额表既有个人登记重新汇总。"), structured_output=True)
    async def refresh_preview(payload: FundingUsageRefreshPayload) -> dict[str, Any]:
        client: FileAgentIntegrationClient = client_factory()
        try:
            return await client.funding_usage_request(
                "POST", "/refresh/preview", payload.model_dump(mode="json"))
        finally:
            await client.close()

    @mcp.tool(name="funding_usage_refresh_publish", description=(
        "按刷新预览指纹更新余额表科研成果资助总额；重算已有登记总支出、余额，"
        "同步首页和个人页，并发布同名新版本。"), structured_output=False)
    async def refresh_publish(payload: FundingUsageRefreshPublishPayload):
        client: FileAgentIntegrationClient = client_factory()
        try:
            result = await client.funding_usage_request(
                "POST", "/refresh/publish", payload.model_dump(mode="json"))
            if not result.get("excel_published") or result.get("status") != "COMPLETED":
                return result
            resource = await client.download_dataset_export(
                dataset_id=result["dataset_id"], export_id=result["export_id"],
                output_dir=Path(download_dir()))
            receipt = {**result, "download_resource_uri": resource["resource_uri"]}
            return CallToolResult(content=[
                TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False)),
                ResourceLink(type="resource_link", name=result["filename"],
                             uri=resource["resource_uri"],
                             mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             size=resource["size_bytes"])], structuredContent=receipt)
        finally:
            await client.close()

    @mcp.tool(name="funding_usage_prompt_target", description=(
        "只读已发布新版成果资助余额表的当前版本、教师名单、个人明细稳定行 ID 和金额；"
        "供本轮明确的登记增改删、资助金额调整或教师姓名更正定位目标。"), structured_output=True)
    async def prompt_target(payload: FundingPromptTargetPayload) -> dict[str, Any]:
        client: FileAgentIntegrationClient = client_factory()
        try:
            return await client.funding_usage_request(
                "POST", "/prompt/target", payload.model_dump(mode="json"))
        finally:
            await client.close()

    @mcp.tool(name="funding_usage_prompt_preview", description=(
        "只预览用户明确要求的余额表登记增改删、资助总额修改或教师姓名更正；"
        "按个人登记重算首页支出、其他资助金额和余额，不修改文件。"), structured_output=True)
    async def prompt_preview(payload: FundingUsagePromptPayload) -> dict[str, Any]:
        client: FileAgentIntegrationClient = client_factory()
        try:
            return await client.funding_usage_request(
                "POST", "/prompt/preview", payload.model_dump(mode="json", exclude_unset=True))
        finally:
            await client.close()

    @mcp.tool(name="funding_usage_prompt_publish", description=(
        "只发布与提示词预览指纹一致的登记、金额或姓名变更；保留来源审计，"
        "重算个人页及首页并发布同名 Excel 新版本。"), structured_output=False)
    async def prompt_publish(payload: FundingUsagePromptPublishPayload):
        client: FileAgentIntegrationClient = client_factory()
        try:
            result = await client.funding_usage_request(
                "POST", "/prompt/publish", payload.model_dump(mode="json", exclude_unset=True))
            if not result.get("excel_published") or result.get("status") != "COMPLETED":
                return result
            resource = await client.download_dataset_export(
                dataset_id=result["dataset_id"], export_id=result["export_id"],
                output_dir=Path(download_dir()))
            receipt = {**result, "download_resource_uri": resource["resource_uri"]}
            return CallToolResult(content=[
                TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False)),
                ResourceLink(type="resource_link", name=result["filename"],
                             uri=resource["resource_uri"],
                             mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             size=resource["size_bytes"])], structuredContent=receipt)
        finally:
            await client.close()
