"""用于 Tool 输入校验的 Pydantic schema。

所有 Tool 调用在执行前都必须经过这些 schema，这是声明式 Planner 步骤和副作用函数之间的第一层运行时防线。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ToolInputValidationError(ValueError):
    """Tool 输入参数未通过 schema 校验时抛出。"""

    pass


class StrictToolInput(BaseModel):
    """拒绝 Planner 输出中未声明字段的 Tool 输入基类。"""

    model_config = ConfigDict(extra="forbid")


class DocumentToolInput(StrictToolInput):
    """单文档 Tool 的输入。"""

    document_id: str = Field(min_length=1)


class SearchToolInput(StrictToolInput):
    """检索类 Tool 的输入。"""

    query: str = Field(min_length=1)
    document_ids: List[str] = Field(default_factory=list)


class EvidenceAnswerInput(StrictToolInput):
    """基于证据生成回答的 Tool 输入。"""

    question: str = Field(min_length=1)
    document_ids: List[str] = Field(default_factory=list)


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


class DocumentLineageReadInput(StrictToolInput):
    """读取文档版本关系和派生件的输入。"""

    document_id: str = Field(min_length=1)
