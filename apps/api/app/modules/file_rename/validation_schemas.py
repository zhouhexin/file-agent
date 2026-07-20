"""文件重命名质量校验使用的受控数据契约。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class RenameRiskLevel(str, Enum):
    """确定性分析得到的风险等级。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RenameValidationVerdict(str, Enum):
    """模型只能返回的校验结论。"""

    PASS = "PASS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECT = "REJECT"


class RenameRiskAssessment(BaseModel):
    """确定性差异分析结果。"""

    model_config = ConfigDict(extra="forbid")

    risk_level: RenameRiskLevel
    risk_score: float = Field(ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list)
    hard_blockers: list[str] = Field(default_factory=list)


class RenameTitleCandidate(BaseModel):
    """允许模型选择的后端标题候选。"""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    value: str
    page_number: int | None = None
    quote: str = ""
    source: str = ""
    parser_name: str | None = None


class RenameLLMValidationResult(BaseModel):
    """模型校验输出，禁止附加自由字段。"""

    model_config = ConfigDict(extra="forbid")

    verdict: RenameValidationVerdict
    title_supported: bool
    year_supported: bool
    document_number_supported: bool
    selected_title_candidate_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    explanation: str = Field(default="", max_length=500)


class RenameValidationAudit(BaseModel):
    """一条建议最终固化到批次中的质量审计。"""

    model_config = ConfigDict(extra="forbid")

    risk_level: RenameRiskLevel
    risk_score: float = Field(ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list)
    hard_blockers: list[str] = Field(default_factory=list)
    validation_mode: str
    llm_verdict: RenameValidationVerdict | None = None
    validator_model: str | None = None
    prompt_version: str | None = None
    validated_at: str


class RenameValidationDecision(BaseModel):
    """质量校验层交给建议服务的最终决定。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    warning_codes: list[str] = Field(default_factory=list)
    audit: RenameValidationAudit
