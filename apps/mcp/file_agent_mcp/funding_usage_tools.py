"""WorkBuddy 成果资助手写登记预览和发布工具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from mcp.types import CallToolResult, ResourceLink, TextContent
from pydantic import BaseModel, ConfigDict, Field

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


def register_funding_usage_tools(mcp, client_factory, download_dir):
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
