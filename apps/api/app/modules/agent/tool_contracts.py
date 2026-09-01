"""Agent Tool 输出、结果绑定与统一错误契约。

本模块只定义可序列化数据结构，不读取文件、数据库或运行时服务。Planner 生成的绑定必须经过这里的
schema 和独立解析器校验，不能把自由表达式直接交给 Tool。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SAFE_BINDING_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


class ToolError(BaseModel):
    """Tool 失败时返回的统一、可审计错误。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
    user_action_required: bool = False


class ToolOutputValidationError(ValueError):
    """Tool handler 输出不符合注册 output schema 时抛出。"""


class GenericToolOutput(BaseModel):
    """旧 Tool 迁移期间使用的受控通用输出。

    ``extra=allow`` 只用于兼容当前已存在的业务字段；Registry 仍会先校验通用状态字段。核心 Adaptive
    Tool 应逐步替换为各自的严格 output model，不能长期把本模型当成完整业务契约。
    """

    model_config = ConfigDict(extra="allow")

    ok: bool = True
    status: str | None = None
    kind: str | None = None
    error: ToolError | dict[str, Any] | None = None
    changeset_id: str | None = None
    operation_plan_id: str | None = None
    async_job_id: str | None = None
    replan_required: bool = False


class DocumentInsightsToolOutput(GenericToolOutput):
    """文件洞察读取 Tool 的迁移期业务输出契约。"""

    documents: list[dict[str, Any]] = Field(default_factory=list)


class AgentCapabilitiesToolOutput(GenericToolOutput):
    """Agent 对外能力清单读取 Tool 的业务输出契约。"""

    version: str = Field(min_length=1)
    capabilities: list[dict[str, Any]] = Field(default_factory=list)


class IntentSummaryToolOutput(GenericToolOutput):
    """普通意图摘要 Tool 的业务输出契约。"""

    intent: str = Field(min_length=1)
    user_goal: str = Field(min_length=1)


class ClassificationTaxonomyToolOutput(GenericToolOutput):
    """固定分类目录读取 Tool 的业务输出契约。"""

    taxonomy: dict[str, Any]


class DocumentExtractionToolOutput(GenericToolOutput):
    """受控正文解析 Tool 的业务输出契约。"""

    document_id: str | None = None
    extraction_run_id: str | None = None
    extractor: str | None = None
    pages: list[dict[str, Any]] = Field(default_factory=list)


class OriginalFileMetadataToolOutput(GenericToolOutput):
    """已通过权限校验的原始文件元信息输出契约。"""

    # 失败分支只返回统一 error；成功分支由 handler 填充全部字段并在聚合前再次检查 kind/ok。
    kind: str | None = None
    document_id: str | None = None
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    storage_backend: str | None = None
    exists: bool | None = None


class DocumentClassificationsToolOutput(GenericToolOutput):
    """当前版本分类证据读取 Tool 的业务输出契约。"""

    version_scope: str | None = None
    documents: list[dict[str, Any]] = Field(default_factory=list)


class SearchEffectiveCondition(BaseModel):
    """后端确认后的单条检索条件，用于 Planner 观察和用户回执。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=300)
    condition_type: Literal[
        "semantic",
        "scope",
        "time",
        "file_type",
        "entity",
        "relation",
        "other",
    ] = "semantic"
    status: Literal[
        "APPLIED",
        "SEMANTIC_ONLY",
        "RELAXED",
        "UNSUPPORTED",
        "REJECTED",
    ]
    source: Literal["user_and_llm", "backend", "tool"] = "backend"


class WorkspaceFileSearchToolOutput(GenericToolOutput):
    """工作副本检索 Tool 的业务输出契约。"""

    kind: str = "workspace_file_search"
    query: str = ""
    total_returned: int = Field(default=0, ge=0)
    # 分级数量由受控检索 Tool 计算，前端仅用于分组展示，不能自行重算相关性。
    supported_count: int | None = Field(default=None, ge=0)
    possible_count: int | None = Field(default=None, ge=0)
    partial: bool = False
    results: list[dict[str, Any]] = Field(default_factory=list)
    semantic_plan: dict[str, Any] | None = None
    result_groups: list[dict[str, Any]] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list, max_length=100)
    effective_conditions: list[SearchEffectiveCondition] = Field(
        default_factory=list,
        max_length=30,
    )
    index_status: str = "READY"
    result_status: str = "ZERO_RESULTS"
    available_next_actions: list[str] = Field(default_factory=list, max_length=10)
    # 完整性由后端只读汇总，不允许 Planner 或 LLM 自行生成“已找全”结论。
    search_completeness: dict[str, Any] | None = None
    user_message: str = ""
    search_clarification: dict[str, Any] | None = None
    # 一字差异候选只用于请求用户确认，不能进入 document_ids 绑定。
    query_corrections: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    trash_restore_selection: dict[str, Any] | None = None
    relevant_file_set_id: str | None = None


class EvidenceAnswerToolOutput(GenericToolOutput):
    """证据回答 Tool 的业务输出契约。"""

    kind: str = "evidence_answer"
    answer: str = ""
    references: list[dict[str, Any]] = Field(default_factory=list)
    field_table: dict[str, Any] | None = None


class ManagedFileCollectionToolOutput(GenericToolOutput):
    """受管文件列表和文件名检索 Tool 的业务输出契约。"""

    query: dict[str, Any] | None = None
    files: list[dict[str, Any]] = Field(default_factory=list)


class ManagedFileReadToolOutput(GenericToolOutput):
    """受管文件读取和批量分类 Tool 的结构化输出契约。"""

    matched_count: int | None = Field(default=None, ge=0)
    completed_count: int | None = Field(default=None, ge=0)
    failed_count: int | None = Field(default=None, ge=0)
    extraction_results: list[dict[str, Any]] = Field(default_factory=list)
    classification_requested: bool = False
    document_id: str | None = None
    extraction_run_id: str | None = None
    extractor: str | None = None
    pages: list[dict[str, Any]] = Field(default_factory=list)


class OperationPlanToolOutput(GenericToolOutput):
    """工作副本计划或显式分类指令直接执行结果的兼容输出契约。"""

    kind: str | None = None
    message: str | None = None
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    item_count: int | None = Field(default=None, ge=0)


class ClassificationDecisionToolOutput(GenericToolOutput):
    """用户分类确认或更正 Tool 的结构化输出契约。"""

    kind: str | None = None
    message: str | None = None
    document_id: str | None = None


class SpreadsheetToolOutput(GenericToolOutput):
    """表格分析、Profile 和校验 Tool 的业务输出契约。"""

    kind: str = Field(min_length=1)


class StructuredExtractionToolOutput(GenericToolOutput):
    """图片动态结构化抽取 Tool 的严格业务输出。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["filesystem_job", "structured_image_extraction"]
    ok: bool = True
    status: Literal[
        "PENDING",
        "WAITING_FOR_ASYNC_JOB",
        "COMPLETED",
        "PARTIAL",
        "NEEDS_REVIEW",
        "FAILED",
    ]
    error: ToolError | dict[str, Any] | None = None
    changeset_id: str | None = None
    async_job_id: str | None = None
    replan_required: bool = False
    document_id: str
    structured_extraction_run_id: str | None = None
    schema_mode: Literal["EXPLICIT_FIELDS", "AUTO_DISCOVER"] | None = None
    record_mode: str | None = None
    presentation: str | None = None
    record_count: int = Field(default=0, ge=0)
    field_count: int = Field(default=0, ge=0)
    review_count: int = Field(default=0, ge=0)
    missing_required_field_count: int = Field(default=0, ge=0)
    quality_band: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    retryable: bool = False
    recommended_retry_strategy: Literal["NONE", "REOCR", "VISION_CROP"] = "NONE"
    low_confidence_field_keys: list[str] = Field(default_factory=list, max_length=20)
    field_schema: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    records: list[dict[str, Any]] = Field(default_factory=list)
    review_items: list[dict[str, Any]] = Field(default_factory=list)
    original_unchanged: bool = True
    export_artifact: dict[str, Any] | None = None
    reused: bool = False


class StructuredExtractionObservation(BaseModel):
    """供 Planner 重规划的脱敏图片结构化抽取观察。"""

    model_config = ConfigDict(extra="forbid")

    record_count: int = Field(ge=0)
    field_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    missing_required_field_count: int = Field(ge=0)
    quality_band: Literal["HIGH", "MEDIUM", "LOW"]
    retryable: bool
    recommended_retry_strategy: Literal["NONE", "REOCR", "VISION_CROP"]
    low_confidence_field_keys: list[str] = Field(default_factory=list, max_length=20)


class ToolResultEnvelope(BaseModel):
    """LangGraph 步骤级执行保存的统一 Tool 结果外壳。"""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    tool_version: str = Field(default="1", min_length=1)
    invocation_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    status: Literal[
        "COMPLETED",
        "FAILED",
        "PENDING",
        "PARTIAL",
        "WAITING_FOR_ASYNC_JOB",
        "WAITING_FOR_CONFIRMATION",
        "NEEDS_REVIEW",
    ]
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    changeset_id: str | None = None
    operation_plan_id: str | None = None
    async_job_id: str | None = None
    replan_signal: str | None = None


class ExecutionObservation(BaseModel):
    """交给 Adaptive Planner 的统一脱敏执行观察。

    每个 Tool 都先投影为本模型允许的状态、数量和受控文件范围；正文、原始路径、数据库主键、密钥和
    任何 handler 私有字段不能进入该观察，避免模型把执行输出当作新的执行指令。
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=120)
    observation_kind: str = Field(default="generic", max_length=80)
    status: str = Field(default="", max_length=80)
    ok: bool | None = None
    error_code: str = Field(default="", max_length=120)
    replan_required: bool = False
    document_ids: list[str] = Field(default_factory=list, max_length=50)
    result_count: int | None = Field(default=None, ge=0)
    completed_count: int | None = Field(default=None, ge=0)
    failed_count: int | None = Field(default=None, ge=0)
    evidence_count: int | None = Field(default=None, ge=0)
    classification_count: int | None = Field(default=None, ge=0)
    # 检索专有字段同样是后端确认后的摘要；其他 Tool 保持默认空值。
    query: str = Field(default="", max_length=500)
    effective_conditions: list[SearchEffectiveCondition] = Field(
        default_factory=list,
        max_length=30,
    )
    result_status: str = Field(default="", max_length=80)
    index_status: str = Field(default="", max_length=80)
    partial: bool = False
    has_operation_plan: bool = False
    requires_user_confirmation: bool = False
    waiting_for_async_job: bool = False
    structured_extraction: StructuredExtractionObservation | None = None
    available_next_decisions: list[Literal["TOOL_PLAN", "CLARIFY", "FINISH"]] = Field(
        default_factory=list,
        max_length=3,
    )


class ToolResultBinding(BaseModel):
    """后续 ToolStep 从已完成步骤输出中取值的受控字段绑定。"""

    model_config = ConfigDict(extra="forbid")

    target_field: str = Field(min_length=1, max_length=200)
    source_step_id: str = Field(min_length=1, max_length=120)
    source_field: str = Field(min_length=1, max_length=200)

    @field_validator("target_field", "source_field")
    @classmethod
    def validate_field_path(cls, value: str) -> str:
        """只允许点分字段名，禁止模板、通配符和表达式执行。"""

        if not SAFE_BINDING_FIELD_PATTERN.fullmatch(value):
            raise ValueError("binding field must be a safe dotted field path")
        return value
