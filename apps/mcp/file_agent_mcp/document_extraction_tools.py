"""WorkBuddy 完整文档提取 MCP 工具。

本模块只编排固定任务、受控下载、租约和强类型回传；正文读取仍由 WorkBuddy
宿主的真实图片/Word/PDF 工具完成。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.types import CallToolResult, ResourceLink, TextContent
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """拒绝未声明参数和非有限浮点数。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExtractionError(StrictModel):
    """宿主读取失败摘要，不包含正文或本地路径。"""

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False


class ExtractionCoverage(StrictModel):
    """宿主声明的完整、部分或失败覆盖。"""

    status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    unit_count: int = Field(ge=0, le=500)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("coverage warnings 必须是 1 到 500 字符")
        return value


class ExtractionBbox(StrictModel):
    """真实 PDF/图像归一化坐标。"""

    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_rectangle(self):
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("bbox 必须是有效矩形")
        return self


class ExtractionBlock(StrictModel):
    """宿主返回的可定位段落或表格元素。"""

    block_id: str = Field(min_length=1, max_length=120)
    kind: Literal["PARAGRAPH", "TABLE_ROW", "TABLE_CELL", "TEXT"]
    text: str = Field(min_length=1, max_length=20_000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    table_id: str | None = Field(default=None, min_length=1, max_length=120)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)
    bbox: ExtractionBbox | None = None

    @model_validator(mode="after")
    def validate_range(self):
        if self.end <= self.start:
            raise ValueError("block end 必须大于 start")
        return self


class ExtractionUnit(StrictModel):
    """一个真实页面或 Word 有序正文区段。"""

    unit_id: str = Field(min_length=1, max_length=120)
    kind: Literal["IMAGE_PAGE", "PDF_PAGE", "TEXT_SECTION"]
    page_number: int | None = Field(default=None, ge=1)
    order: int = Field(ge=0)
    status: Literal["READ", "BLANK", "FAILED", "UNSUPPORTED"]
    text: str = Field(default="", max_length=100_000)
    blocks: list[ExtractionBlock] = Field(default_factory=list, max_length=2_000)
    blank_reason: str | None = Field(default=None, min_length=1, max_length=500)
    error: ExtractionError | None = None

    @model_validator(mode="after")
    def validate_content(self):
        if self.kind == "TEXT_SECTION" and self.page_number is not None:
            raise ValueError("Word 文本区段不能伪造 page_number")
        if self.kind in {"IMAGE_PAGE", "PDF_PAGE"} and self.page_number is None:
            raise ValueError("图片和 PDF 区段必须提供真实 page_number")
        if self.status == "READ" and not self.text.strip():
            raise ValueError("READ 区段必须包含非空正文")
        if self.status == "BLANK" and (self.text or not self.blank_reason):
            raise ValueError("BLANK 区段必须为空且说明 blank_reason")
        if self.status in {"FAILED", "UNSUPPORTED"} and self.error is None:
            raise ValueError("失败区段必须提供结构化 error")
        if self.status != "READ" and self.blocks:
            raise ValueError("只有 READ 区段可以带 blocks")
        for block in self.blocks:
            if block.end > len(self.text) or self.text[block.start:block.end] != block.text:
                raise ValueError("block 文本或字符范围与 unit 正文不一致")
            if block.bbox is not None and self.kind == "TEXT_SECTION":
                raise ValueError("Word 文本区段不能伪造 bbox")
        return self


def register_document_extraction_tools(mcp: Any, client_factory: Any, download_dir: Any) -> None:
    """同时启用文档与累计表开关时注册四个新增工具。"""

    @mcp.tool(name="document_extraction_get", description="读取 WorkBuddy 文档提取任务状态和覆盖摘要，不读取正文。", structured_output=True)
    async def get(task_id: UUID) -> dict[str, Any]:
        """恢复任务状态。"""

        client = client_factory()
        try:
            return await client.document_extraction_get(task_id=str(task_id))
        finally:
            await client.close()

    @mcp.tool(name="document_extraction_claim", description="领取固定文档任务并下载、校验原文件。返回的文件资源交给 WorkBuddy 真实图片、Word 或 PDF 工具读取。", structured_output=False)
    async def claim(task_id: UUID, worker_id: Annotated[str, Field(min_length=1, max_length=160)]) -> CallToolResult:
        """claim 成功后必须确认文件下载和哈希校验也成功。"""

        client = client_factory()
        try:
            result = await client.document_extraction_claim(
                task_id=str(task_id), worker_id=worker_id, output_dir=Path(download_dir())
            )
            source = dict(result.get("source") or {})
            resource_uri = str(source.pop("resource_uri"))
            structured = {**result, "source": source, "downloaded": True}
            return CallToolResult(
                content=[
                    TextContent(type="text", text=f"已领取并校验文档：{source['filename']}"),
                    ResourceLink(type="resource_link", name=str(source["filename"]), uri=resource_uri,
                                 mimeType=str(source.get("content_type") or "application/octet-stream"),
                                 size=int(source.get("size_bytes") or 0)),
                ],
                structuredContent=structured,
            )
        finally:
            await client.close()

    @mcp.tool(name="document_extraction_renew", description="宿主读取耗时较长且租约即将到期时续租；过期 token 不能恢复。", structured_output=True)
    async def renew(task_id: UUID, worker_id: Annotated[str, Field(min_length=1, max_length=160)],
                    lease_token: Annotated[str, Field(min_length=20, max_length=500)]) -> dict[str, Any]:
        """续期固定 Worker 的当前租约。"""

        client = client_factory()
        try:
            return await client.document_extraction_renew(
                task_id=str(task_id), worker_id=worker_id, lease_token=lease_token
            )
        finally:
            await client.close()

    @mcp.tool(name="document_extraction_submit", description="提交 WorkBuddy 真实工具读取的完整文档结果。禁止编造 provider、页码、表格坐标或正文。", structured_output=True)
    async def submit(
        task_id: UUID,
        worker_id: Annotated[str, Field(min_length=1, max_length=160)],
        lease_token: Annotated[str, Field(min_length=20, max_length=500)],
        submission_key: Annotated[str, Field(min_length=1, max_length=200)],
        source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        source_version_id: UUID,
        provider_name: Annotated[str, Field(min_length=1, max_length=120)],
        coverage: ExtractionCoverage,
        units: list[ExtractionUnit],
        provider_version: Annotated[str | None, Field(max_length=120)] = None,
        provider_request_id: Annotated[str | None, Field(max_length=240)] = None,
        error: ExtractionError | None = None,
    ) -> dict[str, Any]:
        """嵌套 schema 与 API 对齐，结构错误在调用后端前拒绝。"""

        client = client_factory()
        try:
            return await client.document_extraction_submit(
                task_id=str(task_id),
                payload={
                    "worker_id": worker_id,
                    "lease_token": lease_token,
                    "submission_key": submission_key,
                    "source_sha256": source_sha256,
                    "source_version_id": str(source_version_id),
                    "provider_name": provider_name,
                    "provider_version": provider_version,
                    "provider_request_id": provider_request_id,
                    "coverage": coverage.model_dump(mode="json"),
                    "units": [unit.model_dump(mode="json") for unit in units],
                    "error": error.model_dump(mode="json") if error else None,
                },
            )
        finally:
            await client.close()
