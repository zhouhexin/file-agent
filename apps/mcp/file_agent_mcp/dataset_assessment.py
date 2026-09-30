"""MCP 字段判定的有界传输契约；正式证据与判断由服务端校验。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class SourceQuote(BaseModel):
    """可引用正式 unit UUID 或本轮宿主 unit_id，不接受路径。"""
    model_config = ConfigDict(extra="forbid")
    source_unit_id: str = Field(min_length=1, max_length=120)
    quote: str = Field(min_length=1, max_length=8000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)


class ApprovalScope(BaseModel):
    """完整栏位也能绑定审批，不需要显式项号或签字。"""
    model_config = ConfigDict(extra="forbid")
    binding_method: Literal["EXPLICIT_RANGE", "WHOLE_ITEM", "WHOLE_SECTION", "CHECKED", "ROW_ORDER", "LAST_ITEM_FALLBACK"]
    declaration: SourceQuote
    seal: SourceQuote
    scope: list[SourceQuote] = Field(min_length=1, max_length=20)
    explanation: str = Field(min_length=1, max_length=1000)
    approval_kind: Literal["STANDARD", "HR_ANNUAL_EXCELLENCE"] = "STANDARD"


class RequirementCheck(BaseModel):
    """以标题原文、观测原文和引文表达条件，不能直接提交通过布尔值。"""
    model_config = ConfigDict(extra="forbid")
    criterion: str = Field(min_length=1, max_length=4000)
    operator: Literal["CONTAINS", "EQUALS", "GTE", "LTE"]
    expected: str = Field(min_length=1, max_length=200)
    observed: str = Field(min_length=1, max_length=4000)
    evidence: list[SourceQuote] = Field(min_length=1, max_length=20)


class RequirementGroup(BaseModel):
    """一个备选分支内的全部要求。"""
    model_config = ConfigDict(extra="forbid")
    checks: list[RequirementCheck] = Field(min_length=1, max_length=20)


class FieldAssessment(BaseModel):
    """对应目标列的完整条件树，数字比较和组合由服务端执行。"""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=4000)
    mode: Literal["ALL", "ANY", "AT_LEAST"] = "ALL"
    minimum: int = Field(default=1, ge=1, le=20)
    required_groups: list[int] = Field(default_factory=list, max_length=20)
    groups: list[RequirementGroup] = Field(min_length=1, max_length=20)
    unresolved_reasons: list[str] = Field(default_factory=list, max_length=20)
