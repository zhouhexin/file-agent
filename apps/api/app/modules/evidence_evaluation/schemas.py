"""证据旁观评估的严格协议；不包含文件写操作或可执行计划。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.llm.diagnostics import SafeErrorDiagnostic

ShortText = Annotated[str, Field(min_length=1, max_length=300)]
EvidenceID = Annotated[str, Field(min_length=1, max_length=100)]
IssueType = Literal["MISSING", "CONFLICT", "OUTDATED"]
CheckStatus = Literal["COMPLETED", "PARTIAL", "NOT_EVALUATED", "EVALUATION_FAILED"]
TemporalFocus = Literal["CURRENT", "HISTORICAL", "UNKNOWN"]


class StrictModel(BaseModel):
    """拒绝模型自造字段、类型转换及未知状态。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class Requirement(StrictModel):
    """问题所必需的一个原子要求；未支持不等于全库没有。"""

    requirement_id: str = Field(pattern=r"^r[1-8]$")
    text: ShortText
    status: Literal["SUPPORTED", "UNSUPPORTED", "UNCERTAIN"]
    evidence_ids: list[EvidenceID] = Field(max_length=12)
    reason: ShortText


class Fact(StrictModel):
    """冲突候选的一端；空维度表示无法确认可比性。"""

    evidence_id: EvidenceID
    entity: str = Field(max_length=120)
    attribute: str = Field(max_length=120)
    time: str = Field(max_length=120)
    basis: str = Field(max_length=120)
    fact_status: str = Field(max_length=120)
    value: ShortText
    # 原值必须是对应 quote 的连续子串；归一化值不能替代原文定位。
    value_quote: ShortText


class ConflictCandidate(StrictModel):
    """只比较同一事实的候选对，禁止不同口径仅凭数字不同就报冲突。"""

    left: Fact
    right: Fact
    incompatible: bool
    reason: ShortText


class SemanticResponse(StrictModel):
    """模型只做要求对照与有限候选对识别，不输出最终操作。"""

    requirements: list[Requirement] = Field(min_length=1, max_length=8)
    conflicts: list[ConflictCandidate] = Field(max_length=24)


class EvidenceIssue(StrictModel):
    """可解释问题；所有引用必须是输入证据白名单的子集。"""

    type: IssueType
    reason: ShortText
    reason_code: str = Field(pattern=r"^[A-Z_]{1,80}$")
    evidence_ids: list[EvidenceID] = Field(max_length=24)
    missing_requirements: list[ShortText] = Field(default_factory=list, max_length=8)


class CheckResult(StrictModel):
    """单个检查器的状态与安全诊断；失败不能折叠成检查通过。"""

    status: CheckStatus
    issues: list[EvidenceIssue] = Field(default_factory=list, max_length=1000)
    requirements: list[Requirement] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list)
    error_codes: list[str] = Field(default_factory=list)
    error_diagnostics: list[SafeErrorDiagnostic] = Field(default_factory=list, max_length=4)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    token_usage_complete: bool = True


class EvaluationResult(StrictModel):
    """完整评估结果仅供内部/离线使用，普通用户答案不消费此结果。"""

    evaluation_status: Literal["DISABLED", "COMPLETED", "PARTIAL", "EVALUATION_FAILED"]
    issues: list[EvidenceIssue] = Field(default_factory=list)
    version_check: CheckResult
    semantic_check: CheckResult
    evaluated_evidence_fingerprint: str
    evaluator_version: Literal["evidence-eval-v1"] = "evidence-eval-v1"
    limitations: list[str] = Field(default_factory=list)
    duration_ms: int = 0
