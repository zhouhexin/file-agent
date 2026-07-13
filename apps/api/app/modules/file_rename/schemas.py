"""文件重命名领域 schema。"""

from __future__ import annotations

from enum import Enum
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

    year: RenameFieldResult
    document_number: RenameFieldResult
    title: RenameFieldResult

    @property
    def can_build_filename(self) -> bool:
        """年份和标题可靠时允许生成正式或降级文件名。"""

        return (
            self.year.status == RenameFieldStatus.RESOLVED
            and self.title.status == RenameFieldStatus.RESOLVED
        )


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
    document_id: str = ""
    root_key: str
    relative_path: str
    filename: str
    proposed_relative_path: str | None = None
    proposed_filename: str | None = None
    source_sha256: str = ""
    year: RenameFieldResult
    document_number: RenameFieldResult
    title: RenameFieldResult
    policy_key: str
    policy_version: str
    template_key: str | None = None
    status: str
    warnings: list[str] = Field(default_factory=list)
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


class RenameBatchResult(BaseModel):
    """批量 Native 重命名结果。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    matched_count: int
    completed_count: int
    failed_count: int
    items: list[RenameExecutionItem]

