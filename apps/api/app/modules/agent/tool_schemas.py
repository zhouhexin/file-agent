"""用于 Tool 输入校验的 Pydantic schema。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.retrieval.semantic_plan import FileSearchSemanticPlan


class ToolInputValidationError(ValueError):
    """Tool 输入参数未通过 schema 校验时抛出。"""


class StrictToolInput(BaseModel):
    """拒绝 Planner 输出中未声明字段的 Tool 输入基类。"""

    model_config = ConfigDict(extra="forbid")


class StructuredFieldSpec(BaseModel):
    """图片结构化抽取的动态字段声明，不接受任意代码或 Prompt。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    field_type: Literal[
        "string",
        "integer",
        "decimal",
        "money",
        "date",
        "datetime",
        "person_name",
        "phone",
        "id_number",
        "organization",
        "boolean",
        "enum",
    ]
    required: bool = False
    multiple: bool = False
    aliases: List[str] = Field(default_factory=list, max_length=10)
    enum_values: List[str] = Field(default_factory=list, max_length=30)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        """移除字段标签首尾空白并拒绝控制字符。"""

        normalized = " ".join(value.strip().split())
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("field label contains control characters")
        return normalized

    @field_validator("aliases", "enum_values")
    @classmethod
    def normalize_string_list(cls, values: List[str]) -> List[str]:
        """限制动态文本列表长度并按稳定顺序去重。"""

        normalized: List[str] = []
        for value in values:
            item = " ".join(str(value or "").strip().split())
            if not item or len(item) > 80:
                raise ValueError("field aliases and enum values must be 1-80 characters")
            if any(ord(char) < 32 for char in item):
                raise ValueError("field aliases and enum values contain control characters")
            if item not in normalized:
                normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def validate_enum_values(self) -> "StructuredFieldSpec":
        """枚举字段必须声明候选值，其他字段不得夹带枚举配置。"""

        if self.field_type == "enum" and not self.enum_values:
            raise ValueError("enum field requires enum_values")
        if self.field_type != "enum" and self.enum_values:
            raise ValueError("non-enum field cannot include enum_values")
        return self


class StructuredImageExtractionInput(StrictToolInput):
    """图片或扫描件动态结构化抽取白名单 Tool 的严格输入。"""

    document_id: str = Field(min_length=1, max_length=36)
    schema_mode: Literal["EXPLICIT_FIELDS", "AUTO_DISCOVER"]
    record_mode: Literal[
        "AUTO",
        "SINGLE_RECORD",
        "TABLE_ROWS",
        "KEY_VALUE_GROUPS",
    ] = "AUTO"
    fields: List[StructuredFieldSpec] = Field(default_factory=list, max_length=40)
    presentation: Literal[
        "AUTO",
        "TABLE",
        "JSON",
        "CSV",
        "XLSX",
        "TEXT",
    ] = "AUTO"
    retry_strategy: Literal["INITIAL", "REOCR", "VISION_CROP"] = "INITIAL"
    target_field_keys: List[str] = Field(default_factory=list, max_length=20)

    @field_validator("target_field_keys")
    @classmethod
    def normalize_target_field_keys(cls, values: List[str]) -> List[str]:
        """重试字段只能使用与动态字段相同的安全 key 格式。"""

        normalized: List[str] = []
        pattern = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
        for value in values:
            item = str(value or "").strip()
            if not pattern.fullmatch(item):
                raise ValueError("target field key has invalid format")
            if item not in normalized:
                normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def validate_schema_and_retry(self) -> "StructuredImageExtractionInput":
        """约束字段发现模式与最多一次定向增强所需参数。"""

        field_keys = [item.key for item in self.fields]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError("structured extraction field keys must be unique")
        if self.schema_mode == "EXPLICIT_FIELDS" and not self.fields:
            raise ValueError("EXPLICIT_FIELDS requires at least one field")
        if self.schema_mode == "AUTO_DISCOVER" and self.fields:
            raise ValueError("AUTO_DISCOVER cannot include planner-provided fields")
        if self.retry_strategy == "VISION_CROP" and not self.target_field_keys:
            raise ValueError("VISION_CROP requires target_field_keys")
        if self.retry_strategy != "VISION_CROP" and self.target_field_keys:
            raise ValueError("target_field_keys are only allowed for VISION_CROP")
        if self.target_field_keys and not set(self.target_field_keys).issubset(field_keys):
            raise ValueError("target_field_keys must exist in fields")
        return self


def _normalize_path_prefix(value: Optional[str]) -> Optional[str]:
    """规范化相对路径前缀，用于限制受管目录子目录查询。"""

    if value is None:
        return None

    normalized = value.replace("\\", "/").strip().strip("/")
    if "\x00" in normalized:
        raise ValueError("path_prefix must not contain NUL characters")
    if normalized in {"", "."}:
        return None

    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise ValueError("path_prefix must be a relative path")

    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path_prefix must not contain '.', '..' or empty path segments")

    return path.as_posix()


class DocumentToolInput(StrictToolInput):
    """单文档 Tool 的输入。"""

    document_id: str = Field(min_length=1)
    force_reprocess: bool = False
    force_reconvert: bool = False


class SpreadsheetAnalysisInput(StrictToolInput):
    """只读电子表格分析输入；不允许传入路径、SQL 或表达式。"""

    document_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=2000)


class SpreadsheetDocumentInput(StrictToolInput):
    """表格工作台只读 Tool 输入；路径必须由后端仓库解析。"""

    document_id: str = Field(min_length=1)


class SearchConditionInput(StrictToolInput):
    """Planner 对自然语言检索条件的可审计描述。

    这些条件只用于语义查询说明和回执。后端只有在真实执行后才会把条件标记为已应用，
    不能让模型通过本结构生成 SQL、路径或任意数据库过滤器。
    """

    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=200)
    condition_type: Literal[
        "semantic",
        "scope",
        "time",
        "file_type",
        "entity",
        "relation",
        "other",
    ] = "semantic"


class SearchToolInput(StrictToolInput):
    """检索类 Tool 的输入。

    普通 Planner 提供 query、document_ids 和经过 schema 校验的 semantic_plan。
    match_mode、phrases 等续跑字段只允许后端在用户解决已持久化歧义选择后生成，
    不能由前端直接提交短语数组绕过选择校验。
    """

    query: str = Field(min_length=1, max_length=500)
    document_ids: List[str] = Field(default_factory=list)
    semantic_plan: FileSearchSemanticPlan | None = None
    match_mode: Literal["AUTO", "LITERAL", "RELATED", "BROAD"] = "AUTO"
    phrases: List[str] = Field(default_factory=list, max_length=8)
    require_body_evidence: bool | None = None
    interpreted_conditions: List[SearchConditionInput] = Field(
        default_factory=list,
        max_length=12,
    )
    clarification_id: str | None = Field(default=None, min_length=1, max_length=36)
    clarification_option_id: str | None = Field(
        default=None, min_length=1, max_length=80
    )
    show_all_results: bool = False

    @field_validator("phrases")
    @classmethod
    def validate_search_phrases(cls, values: List[str]) -> List[str]:
        """限制服务端续跑短语，拒绝空值、控制字符和超长内容。"""

        normalized: List[str] = []
        for value in values:
            phrase = " ".join(str(value or "").strip().split())
            if len(phrase) < 1 or len(phrase) > 30:
                raise ValueError("search phrase length must be between 1 and 30")
            if any(ord(char) < 32 for char in phrase):
                raise ValueError("search phrase contains control characters")
            if phrase not in normalized:
                normalized.append(phrase)
        return normalized

    @model_validator(mode="after")
    def validate_resolution_fields(self) -> "SearchToolInput":
        """续跑字段必须成组出现，AUTO 模式不能夹带短语。"""

        if self.match_mode == "AUTO" and self.phrases:
            raise ValueError("AUTO search cannot include explicit phrases")
        if bool(self.clarification_id) != bool(self.clarification_option_id):
            raise ValueError("clarification id and option id must be provided together")
        if self.show_all_results and (
            not self.clarification_id
            or self.clarification_option_id != "show-all-results"
        ):
            raise ValueError(
                "show_all_results requires a resolved show-all-results clarification"
            )
        return self


class EvidenceAnswerInput(StrictToolInput):
    """基于证据生成回答的 Tool 输入。

    ``document_selection_clarification_id`` 只用于后端续跑已解决的文件选择。
    EvidenceAnswerService 仍会回查持久化记录并校验用户、会话和文件集合，因此即使
    LLM 生成该字段，也不能自行把普通候选范围提升为用户已确认范围。
    """

    question: str = Field(min_length=1, max_length=4000)
    document_ids: List[str] = Field(default_factory=list, max_length=50)
    answer_mode: Literal["AUTO", "FOCUSED", "FULL_SUMMARY"] = "AUTO"
    document_selection_clarification_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
    )


class DocumentInsightsReadInput(StrictToolInput):
    """读取上传阶段 deterministic ingest 洞察的 Tool 输入。"""

    document_ids: List[str] = Field(default_factory=list)


class DocumentClassificationsReadInput(StrictToolInput):
    """读取当前会话文件当前版本的最新成功分类建议。"""

    document_ids: List[str] = Field(default_factory=list)
    version_scope: Literal["CURRENT_WORKING_COPY"] = "CURRENT_WORKING_COPY"
    include_evidence: bool = True
    include_explanation_context: bool = True


class IntentSummaryInput(StrictToolInput):
    """仅记录 LLM 已理解用户需求的低风险 Tool 输入。"""

    intent: str = Field(min_length=1)
    user_goal: str = Field(min_length=1)


class AgentCapabilitiesReadInput(StrictToolInput):
    """读取 Agent 固定能力清单的 Tool 输入。"""

    detail_level: str = "brief"


class ClassificationTaxonomyReadInput(StrictToolInput):
    """读取系统固定分类目录的 Tool 输入。"""

    detail_level: str = "brief"
    max_depth: int = 2


class ChangeReportInput(StrictToolInput):
    """生成 ChangeSet 回执的 Tool 输入。"""

    document_id: Optional[str] = None
    changeset_id: Optional[str] = None


class OperationPlanCreateInput(StrictToolInput):
    """创建高风险 OperationPlan 的输入，只生成计划，不执行动作。"""

    operation_type: str = Field(min_length=1)
    target_document_ids: List[str] = Field(min_length=1)
    proposed_changes: Dict[str, Any] = Field(default_factory=dict)


class ConfirmedFileActionInput(StrictToolInput):
    """执行已确认 OperationPlan 的输入。"""

    operation_plan_id: str = Field(min_length=1)
    confirmation_text: str = Field(min_length=1)


class FeedbackRecordInput(StrictToolInput):
    """记录用户显式反馈的输入。"""

    target_type: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    feedback_type: str = Field(min_length=1)
    comment: str = ""
    context_json: Dict[str, Any] = Field(default_factory=dict)


class ManagedRootListInput(StrictToolInput):
    """列出受管逻辑目录的输入。"""

    enabled_only: bool = True


class ManagedFileListInput(StrictToolInput):
    """列出受管文件元数据的输入。"""

    root_key: Optional[str] = None
    path_prefix: Optional[str] = Field(
        default=None,
        description=(
            "受管根目录下的相对目录或文件路径前缀；"
            "用户要求查看某个子目录下文件时使用，例如：合同/2024。"
        ),
    )
    extension: Optional[str] = None
    filename_contains: Optional[str] = None
    category_path: Optional[str] = None
    classification_mode: Optional[str] = None
    status: str = "ACTIVE"
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @field_validator("path_prefix")
    @classmethod
    def validate_path_prefix(cls, value: Optional[str]) -> Optional[str]:
        """校验并规范化受管目录内的相对路径前缀。"""

        return _normalize_path_prefix(value)


class ManagedFileSearchInput(StrictToolInput):
    """按文件名关键词搜索受管文件的输入。"""

    query: str = Field(min_length=1)
    root_key: Optional[str] = None
    path_prefix: Optional[str] = Field(
        default=None,
        description=(
            "受管根目录下的可选相对路径前缀；"
            "用户要求在某个子目录内搜索时使用。"
        ),
    )
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("path_prefix")
    @classmethod
    def validate_path_prefix(cls, value: Optional[str]) -> Optional[str]:
        """校验并规范化受管目录内的相对路径前缀。"""

        return _normalize_path_prefix(value)


class ManagedFileReadDocumentInput(StrictToolInput):
    """读取并解析唯一受管文件的输入。"""

    root_key: Optional[str] = None
    relative_path: Optional[str] = None
    path_prefix: Optional[str] = Field(
        default=None,
        description="受管根目录下的相对目录或文件路径前缀。",
    )
    extension: Optional[str] = None
    filename_contains: Optional[str] = None
    force_reprocess: bool = False
    scan_before_read: bool = True

    @field_validator("relative_path", "path_prefix")
    @classmethod
    def validate_managed_path(cls, value: Optional[str]) -> Optional[str]:
        """校验并规范化受管目录内的相对路径。"""

        return _normalize_path_prefix(value)


class ManagedFileClassificationInput(StrictToolInput):
    """按受管目录逻辑范围批量创建快照、解析正文并分类。"""

    root_key: Optional[str] = Field(default=None, max_length=100)
    path_prefix: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="受管根目录下的相对目录前缀。",
    )
    extension: Optional[str] = Field(default=None, max_length=20)
    filename_contains: Optional[str] = Field(default=None, max_length=255)
    recursive: bool = True
    force_reprocess: bool = False
    conversation_id: Optional[str] = Field(default=None, max_length=255)
    agent_run_id: Optional[str] = Field(default=None, max_length=255)

    @field_validator("path_prefix")
    @classmethod
    def validate_path_prefix(cls, value: Optional[str]) -> Optional[str]:
        """校验并规范化受管目录内的相对路径。"""

        return _normalize_path_prefix(value)

    @model_validator(mode="after")
    def validate_scope(self) -> "ManagedFileClassificationInput":
        """禁止无范围扫描全部受管目录，避免误触发大批量解析。"""

        if not any([self.root_key, self.path_prefix, self.extension, self.filename_contains]):
            raise ValueError("managed file classification requires at least one scope filter")
        return self


class GenerateRenameSuggestionsInput(StrictToolInput):
    """把附件或受管范围解析为工作副本并持久化重命名 OperationPlan。"""

    document_ids: List[str] = Field(default_factory=list, max_length=50)
    root_key: Optional[str] = None
    path_prefix: Optional[str] = None
    relative_path: Optional[str] = None
    path_candidates: List[str] = Field(default_factory=list, max_length=10)
    scope_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    extension: Optional[str] = None
    filename_contains: Optional[str] = None
    limit: int = Field(default=500, ge=1, le=500)
    conversation_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)

    @field_validator("path_prefix", "relative_path")
    @classmethod
    def validate_path_prefix(cls, value: Optional[str]) -> Optional[str]:
        """校验受管目录内的相对路径。"""

        return _normalize_path_prefix(value)

    @field_validator("path_candidates")
    @classmethod
    def validate_path_candidates(cls, value: List[str]) -> List[str]:
        """规范化 LLM 目录候选并保持顺序去重。"""

        normalized = [_normalize_path_prefix(item) for item in value]
        return list(dict.fromkeys(item for item in normalized if item))

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, value: List[str]) -> List[str]:
        """拒绝空标识并保持附件顺序去重，避免扩大重命名范围。"""

        normalized = [str(item).strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("document_ids must not contain empty values")
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_single_scope(self) -> "GenerateRenameSuggestionsInput":
        """附件范围与受管目录过滤条件不能混用，防止跨边界扫描。"""

        if self.document_ids and any(
            [
                self.root_key,
                self.path_prefix,
                self.relative_path,
                self.path_candidates,
                self.extension,
                self.filename_contains,
            ]
        ):
            raise ValueError("document_ids cannot be combined with managed-file filters")
        return self


class ResolveRenameReviewsInput(StrictToolInput):
    """处理重命名待复核项的用户更正或放弃消息。"""

    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)


class WorkingCopyActionPlanInput(StrictToolInput):
    """把对话文件动作转换为待确认工作副本计划的受控输入。"""

    action: Literal[
        "TRASH",
        "RESTORE",
        "CONFLICT_KEEP_BOTH",
        "CONFLICT_KEEP_EXISTING",
        "CONFLICT_REPLACE_EXISTING",
        "CONFLICT_DELETE_EXISTING",
        "CONFLICT_CANCEL",
        "MOVE_BY_CONFIRMED_CATEGORY",
        "MOVE_AFTER_AUTO_RECLASSIFICATION",
    ]
    message: str = Field(min_length=1, max_length=4000)
    document_ids: List[str] = Field(default_factory=list, max_length=50)
    conversation_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, value: List[str]) -> List[str]:
        """拒绝空 ID 并保持后端附件顺序，不能扩大用户选择范围。"""

        normalized = [str(item).strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("document_ids must not contain empty values")
        return list(dict.fromkeys(normalized))


class ClassificationDecisionInput(StrictToolInput):
    """自然语言分类决定只能携带原话和后端解析的附件范围。"""

    action: Literal["ACCEPT", "REJECT", "CORRECT"]
    message: str = Field(min_length=1, max_length=4000)
    document_ids: List[str] = Field(default_factory=list, max_length=50)
    conversation_id: str = Field(min_length=1, max_length=36)
    agent_run_id: str = Field(min_length=1, max_length=36)

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, value: List[str]) -> List[str]:
        """保持后端附件顺序并拒绝空 ID。"""

        normalized = [str(item).strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("document_ids must not contain empty values")
        return list(dict.fromkeys(normalized))


class ManagedRootScanInput(StrictToolInput):
    """创建受管目录扫描任务的输入。"""

    root_key: str = Field(min_length=1)


class MCPFilesystemListInput(StrictToolInput):
    """实时列出 Filesystem MCP 受管目录的只读输入。"""

    path_prefix: Optional[str] = None
    sort_by: Literal["name", "size"] = "name"

    @field_validator("path_prefix")
    @classmethod
    def validate_path_prefix(cls, value: Optional[str]) -> Optional[str]:
        """校验并规范化 MCP 受管目录内的相对路径。"""

        return _normalize_path_prefix(value)


class MCPFilesystemSearchInput(StrictToolInput):
    """实时搜索 Filesystem MCP 受管目录的只读输入。"""

    query: str = Field(min_length=1, max_length=200)
    path_prefix: Optional[str] = None
    exclude_patterns: List[str] = Field(default_factory=list, max_length=20)

    @field_validator("path_prefix")
    @classmethod
    def validate_path_prefix(cls, value: Optional[str]) -> Optional[str]:
        """校验并规范化 MCP 受管目录内的相对路径。"""

        return _normalize_path_prefix(value)


class MCPFilesystemInfoInput(StrictToolInput):
    """读取 Filesystem MCP 受管路径元数据的只读输入。"""

    path: str = Field(min_length=1, max_length=1000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """校验并规范化 MCP 受管目录内的相对路径。"""

        normalized = _normalize_path_prefix(value)
        if normalized is None:
            raise ValueError("path must identify a file or directory")
        return normalized
