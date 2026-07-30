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


class DocumentClassificationsToolOutput(GenericToolOutput):
    """当前版本分类证据读取 Tool 的业务输出契约。"""

    version_scope: str | None = None
    documents: list[dict[str, Any]] = Field(default_factory=list)


class WorkspaceFileSearchToolOutput(GenericToolOutput):
    """工作副本检索 Tool 的业务输出契约。"""

    kind: str = "workspace_file_search"
    query: str = ""
    results: list[dict[str, Any]] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list, max_length=100)


class EvidenceAnswerToolOutput(GenericToolOutput):
    """证据回答 Tool 的业务输出契约。"""

    kind: str = "evidence_answer"
    answer: str = ""
    references: list[dict[str, Any]] = Field(default_factory=list)


class ManagedFileCollectionToolOutput(GenericToolOutput):
    """受管文件列表和文件名检索 Tool 的业务输出契约。"""

    query: dict[str, Any] | None = None
    files: list[dict[str, Any]] = Field(default_factory=list)


class SpreadsheetToolOutput(GenericToolOutput):
    """表格分析、Profile 和校验 Tool 的业务输出契约。"""

    kind: str = Field(min_length=1)


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
