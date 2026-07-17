"""文件重命名领域 schema。"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RenameFieldStatus(str, Enum):
    """重命名字段解析状态。"""

    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING = "MISSING"
    INVALID = "INVALID"


class RenameEvidenceItem(BaseModel):
    """年份、文号或标题的可定位证据。"""

    model_config = ConfigDict(extra="forbid")

    type: str = "text_quote"
    page_number: int | None = None
    sheet_name: str | None = None
    quote: str
    source: str
    element_index: int | None = None
    element_label: str | None = None
    bbox: dict[str, Any] | None = None
    parser_name: str | None = None
    candidate_score: float | None = Field(default=None, ge=0, le=1)
    selection_reason: str | None = None


class RenameFieldResult(BaseModel):
    """单个命名字段的解析结果。"""

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    status: RenameFieldStatus
    source: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_items: list[RenameEvidenceItem] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)


class FilenameMetadataResult(BaseModel):
    """一个文件的命名字段集合。"""

    model_config = ConfigDict(extra="forbid")

    document_date: RenameFieldResult = Field(
        default_factory=lambda: RenameFieldResult(status=RenameFieldStatus.MISSING)
    )
    year: RenameFieldResult
    document_number: RenameFieldResult
    title: RenameFieldResult

    @property
    def can_build_filename(self) -> bool:
        """正文标题可靠时允许生成正式、日期降级或纯标题文件名。"""

        if self.title.status != RenameFieldStatus.RESOLVED:
            return False
        if self.year.status != RenameFieldStatus.RESOLVED and self.title.source == "filename":
            # 纯标题兜底必须来自正文或结构化文档，不能把原文件名原样当成新名称。
            return False
        return True


class RenameTemplate(BaseModel):
    """一条受控文件名模板。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    template: str
    required_fields: list[str]
    when: str | None = None


class RenamePolicy(BaseModel):
    """项目允许使用的安全重命名规则。"""

    model_config = ConfigDict(extra="forbid")

    policy_key: str
    version: str
    separator: str = "_"
    templates: list[RenameTemplate]
    missing_field_strategy: str = "NEEDS_REVIEW"
    conflict_strategy: str = "ERROR"
    duplicate_title_strategy: str = "VERSION_SUFFIX"
    include_hidden: bool = False
    rename_directories: bool = False
    preserve_extension: bool = True
    lowercase_extension: bool = True
    max_filename_bytes: int = Field(default=240, ge=64, le=255)
    noise_terms: list[str] = Field(default_factory=list)


class RenameSuggestion(BaseModel):
    """单个受管文件的重命名建议。"""

    model_config = ConfigDict(extra="forbid")

    managed_file_id: str
    review_id: str | None = None
    document_id: str = ""
    root_key: str
    relative_path: str
    filename: str
    extension: str = ""
    size_bytes: int = 0
    managed_status: str = "ACTIVE"
    proposed_relative_path: str | None = None
    proposed_filename: str | None = None
    source_sha256: str = ""
    document_date: RenameFieldResult = Field(
        default_factory=lambda: RenameFieldResult(status=RenameFieldStatus.MISSING)
    )
    year: RenameFieldResult
    document_number: RenameFieldResult
    title: RenameFieldResult
    policy_key: str
    policy_version: str
    template_key: str | None = None
    status: str
    warnings: list[str] = Field(default_factory=list)
    rename_parse_mode: str = ""
    rename_candidate_parsers: list[str] = Field(default_factory=list)
    arbitration_warnings: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class RenameExecutionItem(BaseModel):
    """一个已确认重命名项目的执行结果。"""

    model_config = ConfigDict(extra="forbid")

    managed_file_id: str
    before_relative_path: str
    after_relative_path: str
    status: str
    error_code: str | None = None
    error_message: str | None = None


class RenameBatchItem(BaseModel):
    """一个已经由 OperationPlan 固化的重命名映射。"""

    model_config = ConfigDict(extra="forbid")

    managed_file_id: str
    before_relative_path: str
    after_relative_path: str
    source_sha256: str = ""


class RenameBatchRequest(BaseModel):
    """Native 和 F2 共用的批量执行请求。"""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    root_path: Path
    operation_plan_id: str
    items: list[RenameBatchItem] = Field(min_length=1)
    timeout_seconds: int = Field(default=60, ge=1, le=600)


class RenameBatchResult(BaseModel):
    """Native 和 F2 共用的批量重命名结果。"""

    model_config = ConfigDict(extra="forbid")

    executor: str = "native"
    executor_version: str = "builtin"
    preview_digest: str = ""
    status: str
    matched_count: int
    completed_count: int
    failed_count: int
    duration_ms: int = 0
    items: list[RenameExecutionItem]


class UploadedRenameExecutionItem(BaseModel):
    """一个上传附件在临时存储中的重命名执行结果。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    before_filename: str
    after_filename: str
    status: str
    error_code: str | None = None
    error_message: str | None = None


class UploadedRenameBatchResult(BaseModel):
    """上传附件临时存储重命名的逐文件批次结果。"""

    model_config = ConfigDict(extra="forbid")

    executor: str = "temporary-storage"
    status: str
    matched_count: int
    completed_count: int
    failed_count: int
    duration_ms: int = 0
    items: list[UploadedRenameExecutionItem]
