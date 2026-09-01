"""阶段五内部证据包和模型输出 schema。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidenceItem(BaseModel):
    """一条已完成工作副本或源侧修订范围校验的原文证据。"""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    document_id: str
    document_version_id: str
    # 源侧只读证据没有工作副本；空值明确表示不能据此直接执行文件操作。
    working_copy_id: str | None = None
    managed_file_revision_id: str | None = None
    filename: str
    quote: str
    page_number: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None


class EvidencePackage(BaseModel):
    """一次模型生成只允许消费的已授权证据包。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    question_type: str
    answer_mode: Literal["FOCUSED", "FULL_SUMMARY"]
    response_format: Literal["TEXT", "FIELD_TABLE"] = "TEXT"
    fields: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    scope: dict[str, Any] = Field(default_factory=dict)
    evidence_items: list[EvidenceItem] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)
    evidence_fingerprint: str


class AnswerClaim(BaseModel):
    """模型生成的一条结论及其证据 ID。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


class AnswerFieldValue(BaseModel):
    """模型针对后端锁定字段返回的候选值。"""

    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value: str = Field(default="", max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    status: Literal["EXTRACTED", "NOT_FOUND"] = "EXTRACTED"


class StructuredAnswer(BaseModel):
    """模型必须返回的受控回答结构。"""

    model_config = ConfigDict(extra="forbid")

    claims: list[AnswerClaim] = Field(default_factory=list, max_length=80)
    field_values: list[AnswerFieldValue] = Field(default_factory=list, max_length=40)
    limitations: list[
        Annotated[str, Field(min_length=1, max_length=500)]
    ] = Field(default_factory=list, max_length=12)
    status: Literal["COMPLETED", "PARTIAL", "NO_EVIDENCE"] = "COMPLETED"
