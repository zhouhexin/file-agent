"""WorkBuddy 完整文档提取 v1 的严格契约。

本模块只接收宿主已经读取出的内容；它不提供服务器 OCR、Word/PDF 解析或任意路径读取能力。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schemas import ExternalExtractionClaimRequest, ExternalExtractionRenewRequest


DOCUMENT_EXTRACTION_CONTRACT_VERSION = "workbuddy-document-v1"
MAX_DOCUMENT_EXTRACTION_TEXT_CHARS = 100_000
MAX_DOCUMENT_EXTRACTION_UNITS = 500
MAX_DOCUMENT_EXTRACTION_BLOCKS = 2_000
MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES = 4 * 1024 * 1024


class StrictDocumentExtractionInput(BaseModel):
    """拒绝未知字段和非有限数值，避免宿主输出被解释成执行参数。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DocumentExtractionError(StrictDocumentExtractionInput):
    """可恢复失败只保存有限的诊断摘要，正文不放入错误对象。"""

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False


class DocumentExtractionCoverage(StrictDocumentExtractionInput):
    """宿主声明的读取覆盖范围；服务端仍会校验页号和结构。"""

    status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    unit_count: int = Field(ge=0, le=MAX_DOCUMENT_EXTRACTION_UNITS)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("coverage warnings 必须是 1 到 500 字符")
        return value


class DocumentExtractionBbox(StrictDocumentExtractionInput):
    """仅保存真实图像或 PDF 坐标，坐标统一为归一化范围。"""

    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_rectangle(self) -> "DocumentExtractionBbox":
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("bbox 必须是有效矩形")
        return self


class DocumentExtractionBlock(StrictDocumentExtractionInput):
    """可定位的正文、表格行或单元格；服务端核对切片，不相信客户端偏移。"""

    block_id: str = Field(min_length=1, max_length=120)
    kind: Literal["PARAGRAPH", "TABLE_ROW", "TABLE_CELL", "TEXT"]
    text: str = Field(min_length=1, max_length=20_000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    table_id: str | None = Field(default=None, min_length=1, max_length=120)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)
    bbox: DocumentExtractionBbox | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "DocumentExtractionBlock":
        if self.end <= self.start:
            raise ValueError("block end 必须大于 start")
        return self


class DocumentExtractionUnit(StrictDocumentExtractionInput):
    """一个真实页面或 Word 有序文本区段，不能伪造 Word 物理页码。"""

    unit_id: str = Field(min_length=1, max_length=120)
    kind: Literal["IMAGE_PAGE", "PDF_PAGE", "TEXT_SECTION"]
    page_number: int | None = Field(default=None, ge=1)
    order: int = Field(ge=0)
    status: Literal["READ", "BLANK", "FAILED", "UNSUPPORTED"]
    text: str = Field(default="", max_length=MAX_DOCUMENT_EXTRACTION_TEXT_CHARS)
    blocks: list[DocumentExtractionBlock] = Field(default_factory=list, max_length=MAX_DOCUMENT_EXTRACTION_BLOCKS)
    blank_reason: str | None = Field(default=None, min_length=1, max_length=500)
    error: DocumentExtractionError | None = None

    @model_validator(mode="after")
    def validate_content(self) -> "DocumentExtractionUnit":
        if self.kind == "TEXT_SECTION" and self.page_number is not None:
            raise ValueError("Word 文本区段不能伪造 page_number")
        if self.kind in {"IMAGE_PAGE", "PDF_PAGE"} and self.page_number is None:
            raise ValueError("图片和 PDF 区段必须提供真实 page_number")
        if self.status == "READ" and not self.text:
            raise ValueError("READ 区段必须包含正文")
        if self.status == "BLANK" and (self.text or not self.blank_reason):
            raise ValueError("BLANK 区段必须为空且说明 blank_reason")
        if self.status in {"FAILED", "UNSUPPORTED"} and self.error is None:
            raise ValueError("失败区段必须提供结构化 error")
        if self.status != "READ" and self.blocks:
            raise ValueError("只有 READ 区段可以带 blocks")
        seen = set()
        for block in self.blocks:
            if block.block_id in seen:
                raise ValueError("同一区段的 block_id 不能重复")
            seen.add(block.block_id)
            if block.end > len(self.text) or self.text[block.start:block.end] != block.text:
                raise ValueError("block 文本或字符范围与 unit 正文不一致")
            if block.bbox is not None and self.kind == "TEXT_SECTION":
                raise ValueError("Word 文本区段不能伪造 bbox")
        return self


class DocumentExtractionClaimResponse(StrictDocumentExtractionInput):
    """领取后返回固定源文件与租约，MCP 必须下载并校验后才交给宿主读取。"""

    task_id: str
    provider_contract_version: Literal[DOCUMENT_EXTRACTION_CONTRACT_VERSION]
    source_version_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_token: str
    lease_expires_at: datetime
    source: dict
    limits: dict


class DocumentExtractionSubmitRequest(StrictDocumentExtractionInput):
    """整文件幂等回传。仅 COMPLETE 可推进归档，PARTIAL/FAILED 只保存诊断。"""

    worker_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=20, max_length=500)
    submission_key: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_version_id: UUID
    provider_name: str = Field(min_length=1, max_length=120)
    provider_version: str | None = Field(default=None, max_length=120)
    provider_request_id: str | None = Field(default=None, max_length=240)
    coverage: DocumentExtractionCoverage
    units: list[DocumentExtractionUnit] = Field(default_factory=list, max_length=MAX_DOCUMENT_EXTRACTION_UNITS)
    error: DocumentExtractionError | None = None

    @model_validator(mode="after")
    def validate_submission(self) -> "DocumentExtractionSubmitRequest":
        if self.coverage.unit_count != len(self.units):
            raise ValueError("coverage.unit_count 必须等于 units 数量")
        if self.coverage.status == "COMPLETE":
            if not self.units or self.error is not None:
                raise ValueError("COMPLETE 必须包含成功区段且不能带整体 error")
            if any(unit.status != "READ" for unit in self.units):
                raise ValueError("COMPLETE 不能包含失败、未支持或空白区段")
        elif self.coverage.status == "FAILED" and self.error is None:
            raise ValueError("FAILED 必须提供整体 error")
        unit_ids = [unit.unit_id for unit in self.units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("units 中的 unit_id 不能重复")
        block_ids = [block.block_id for unit in self.units for block in unit.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("文件内 block_id 不能重复")
        if [unit.order for unit in self.units] != list(range(len(self.units))):
            raise ValueError("units 的 order 必须从 0 连续递增")
        if sum(len(unit.text) for unit in self.units) > MAX_DOCUMENT_EXTRACTION_TEXT_CHARS:
            raise ValueError("文档正文超过 Demo 处理范围")
        if sum(len(unit.blocks) for unit in self.units) > MAX_DOCUMENT_EXTRACTION_BLOCKS:
            raise ValueError("blocks 总数超过 Demo 处理范围")
        if any(unit.status == "READ" and not unit.text.strip() for unit in self.units):
            raise ValueError("READ 区段必须包含非空正文")
        return self

    def payload_size_bytes(self) -> int:
        """路由在写入前使用同一规范 JSON 大小限制嵌套输入。"""

        return len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class DocumentExtractionTaskResponse(StrictDocumentExtractionInput):
    """安全任务投影，不返回正文、租约 token 或服务器路径。"""

    id: str
    ingest_item_id: str
    provider_contract_version: Literal[DOCUMENT_EXTRACTION_CONTRACT_VERSION]
    source_version_id: str
    source_sha256: str
    status: str
    coverage: dict = Field(default_factory=dict)
    retryable: bool = False
    error: dict = Field(default_factory=dict)
    extraction_run_id: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentExtractionSubmitResponse(StrictDocumentExtractionInput):
    """accepted 与 ok 分离，避免 PARTIAL 被误当成正式发布成功。"""

    task: DocumentExtractionTaskResponse
    accepted: bool
    ok: bool
    reused: bool
    next_stage: str
    filesystem_job_id: str | None = None


__all__ = [
    "DOCUMENT_EXTRACTION_CONTRACT_VERSION",
    "MAX_DOCUMENT_EXTRACTION_BLOCKS",
    "MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES",
    "MAX_DOCUMENT_EXTRACTION_TEXT_CHARS",
    "DocumentExtractionClaimResponse",
    "DocumentExtractionSubmitRequest",
    "DocumentExtractionSubmitResponse",
    "DocumentExtractionTaskResponse",
    "ExternalExtractionClaimRequest",
    "ExternalExtractionRenewRequest",
]
