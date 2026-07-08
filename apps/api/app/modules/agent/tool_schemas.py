"""用于 Tool 输入校验的 Pydantic schema。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolInputValidationError(ValueError):
    """Tool 输入参数未通过 schema 校验时抛出。"""


class StrictToolInput(BaseModel):
    """拒绝 Planner 输出中未声明字段的 Tool 输入基类。"""

    model_config = ConfigDict(extra="forbid")


def _normalize_path_prefix(value: Optional[str]) -> Optional[str]:
    """规范化相对路径前缀，用于限制受管目录子目录查询。"""

    if value is None:
        return None

    normalized = value.replace("\\", "/").strip().strip("/")
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


class SpreadsheetAnalysisInput(StrictToolInput):
    """只读电子表格分析输入；不允许传入路径、SQL 或表达式。"""

    document_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=2000)


class SpreadsheetDocumentInput(StrictToolInput):
    """表格工作台只读 Tool 输入；路径必须由后端仓库解析。"""

    document_id: str = Field(min_length=1)


class SearchToolInput(StrictToolInput):
    """检索类 Tool 的输入。"""

    query: str = Field(min_length=1)
    document_ids: List[str] = Field(default_factory=list)


class EvidenceAnswerInput(StrictToolInput):
    """基于证据生成回答的 Tool 输入。"""

    question: str = Field(min_length=1)
    document_ids: List[str] = Field(default_factory=list)


class DocumentInsightsReadInput(StrictToolInput):
    """读取上传阶段 deterministic ingest 洞察的 Tool 输入。"""

    document_ids: List[str] = Field(default_factory=list)


class DocumentClassificationsReadInput(StrictToolInput):
    """读取当前会话文件的历史分类建议。"""

    document_ids: List[str] = Field(default_factory=list)


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


class JobStatusReadInput(StrictToolInput):
    """读取异步任务状态的输入。"""

    job_id: str = Field(min_length=1)


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


class ManagedRootScanInput(StrictToolInput):
    """创建受管目录扫描任务的输入。"""

    root_key: str = Field(min_length=1)


class DocumentLineageReadInput(StrictToolInput):
    """读取文档版本关系和派生件的输入。"""

    document_id: str = Field(min_length=1)
