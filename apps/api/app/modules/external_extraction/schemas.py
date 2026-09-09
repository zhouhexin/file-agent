"""外部 OCR API 的严格输入输出 Schema。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExternalExtractionClaimRequest(BaseModel):
    """领取任务所需的稳定 Worker 身份。"""

    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=160)


class ExternalExtractionPageResource(BaseModel):
    """有效租约下可下载的一个待 OCR 页面。"""

    page_number: int
    content_type: str
    resource_url: str


class ExternalExtractionClaimResponse(BaseModel):
    """领取成功后一次性返回的租约和页面范围。"""

    task_id: str
    source_sha256: str
    source_version_id: str
    provider_contract_version: str
    lease_token: str
    lease_expires_at: datetime
    pages: list[ExternalExtractionPageResource]


class ExternalExtractionRenewRequest(BaseModel):
    """续租请求必须同时证明 Worker 和当前 token。"""

    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=20, max_length=500)


class ExternalExtractionPageResult(BaseModel):
    """一页真实 OCR 结果；缺失置信度或坐标时保持空值。"""

    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    text: str = Field(max_length=2_000_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    blocks: list[dict[str, Any]] = Field(default_factory=list, max_length=20_000)
    provider_name: str = Field(min_length=1, max_length=120)
    provider_version: str | None = Field(default=None, max_length=120)
    provider_request_id: str | None = Field(default=None, max_length=240)
    error: dict[str, Any] | None = None


class ExternalExtractionSubmitRequest(BaseModel):
    """一次覆盖固定页集合的幂等提交。"""

    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=160)
    lease_token: str = Field(min_length=20, max_length=500)
    submission_key: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_version_id: str = Field(min_length=1, max_length=36)
    pages: list[ExternalExtractionPageResult] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def reject_duplicate_pages(self) -> "ExternalExtractionSubmitRequest":
        """限制页集合和总体积，防止嵌套 blocks 绕过请求资源上限。"""

        numbers = [page.page_number for page in self.pages]
        if len(numbers) != len(set(numbers)):
            raise ValueError("pages 中的 page_number 不能重复")
        total_text_chars = sum(len(page.text) for page in self.pages)
        if total_text_chars > 50_000_000:
            raise ValueError("一次 OCR 提交的文本总量不能超过 5000 万字符")
        structured_bytes = sum(
            len(
                json.dumps(
                    {"blocks": page.blocks, "error": page.error},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for page in self.pages
        )
        if structured_bytes > 20 * 1024 * 1024:
            raise ValueError("一次 OCR 提交的结构化坐标和错误信息不能超过 20 MB")
        return self


class ExternalExtractionTaskResponse(BaseModel):
    """外部 OCR 任务的安全业务状态。"""

    id: str
    ingest_item_id: str
    source_version_id: str
    source_sha256: str
    phase: str
    provider_contract_version: str
    status: str
    page_numbers: list[int]
    extraction_run_id: str | None = None
    error: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ExternalExtractionSubmitResponse(BaseModel):
    """结果验收与后续工作流状态。"""

    task: ExternalExtractionTaskResponse
    accepted: bool
    reused: bool
    next_stage: str
    filesystem_job_id: str | None = None
