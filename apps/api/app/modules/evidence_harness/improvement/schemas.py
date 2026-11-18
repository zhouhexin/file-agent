"""自主补丁严格协议，预算由操作者提供，模型只能生成受限判断条件。"""

from typing import Literal

from pydantic import Field, model_validator

from app.modules.evidence_evaluation.schemas import StrictModel
from app.modules.evidence_harness.schemas import Hash


class LegacyPatchProposal(StrictModel):
    """只供旧反馈/历史格式核验，不能作为新版建议器的宽松后备协议。"""

    decision: Literal["NO_CHANGE", "PROPOSE_PATCH"]
    feedback_refs: list[Hash] = Field(min_length=1, max_length=8)
    baseline_sha256: Hash
    guard: str | None = Field(default=None, max_length=1200)
    reason: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def decision_matches_guard(self):
        """拒绝不改却夹带补丁，以及声称修改但没有条件。"""
        if (self.decision == "NO_CHANGE") != (self.guard is None):
            raise ValueError("PATCH_DECISION_MISMATCH")
        return self


class PatchProposal(LegacyPatchProposal):
    """新版建议区分任务判断、修改假设与不修改原因，不要求模型必须改代码。"""

    schema_version: Literal["patch-proposal-v2"] = "patch-proposal-v2"
    diagnosis: Literal["BUSINESS_FAILURE", "EXECUTION_FAILURE", "NO_ACTIONABLE_FAILURE", "INSUFFICIENT_EVIDENCE"]
    evidence_summary: str = Field(min_length=1, max_length=800)
    hypothesis: str | None = Field(max_length=800)
    no_change_reason: Literal["NO_ACTIONABLE_FAILURE", "INSUFFICIENT_EVIDENCE", "OUTSIDE_ALLOWED_SCOPE", "VERIFIED_NO_GAIN"] | None
    no_change_attempt_ref: Hash | None = None

    @model_validator(mode="after")
    def validate_explanation(self):
        """修改需有可检验假设；不修改需明确原因，不能通过空说明蒙混过关。"""
        if not self.reason.strip() or not self.evidence_summary.strip():
            raise ValueError("PATCH_EXPLANATION_EMPTY")
        if self.decision == "PROPOSE_PATCH":
            if not self.hypothesis or not self.hypothesis.strip() or self.no_change_reason is not None:
                raise ValueError("PATCH_HYPOTHESIS_REQUIRED")
        elif self.no_change_reason is None or self.hypothesis is not None:
            raise ValueError("NO_CHANGE_REASON_REQUIRED")
        if (self.no_change_reason == "NO_ACTIONABLE_FAILURE") != (self.diagnosis == "NO_ACTIONABLE_FAILURE"):
            raise ValueError("PATCH_DIAGNOSIS_REASON_MISMATCH")
        if (self.no_change_reason == "VERIFIED_NO_GAIN") != (self.no_change_attempt_ref is not None):
            raise ValueError("NO_CHANGE_ATTEMPT_REFERENCE_REQUIRED")
        return self


class ImprovementAuthorization(StrictModel):
    """整个多轮实验共用额度，不能每轮重新计算授权。"""

    max_rounds: int = Field(default=1, ge=1, le=5)
    max_total_requests: int = Field(default=19, ge=1, le=1000)
    max_active_seconds: int = Field(default=1800, ge=1, le=7200)
    repetitions: int = Field(default=1, ge=1, le=3)
    advisor_max_output_tokens: Literal[3000, 6000, 8000] = 8000
    planner_max_output_tokens: Literal[3000, 6000, 8000] = 3000
    max_no_progress: int = Field(default=2, ge=1, le=5)
    max_infrastructure_failures: int = Field(default=2, ge=1, le=5)


class PlanningEvaluationAuthorization(StrictModel):
    """独立规划基线没有建议调用，每题最多三次模型请求，共用全局授权。"""

    max_total_requests: int = Field(default=6, ge=1, le=1000)
    max_active_seconds: int = Field(default=1800, ge=1, le=7200)
    repetitions: int = Field(default=1, ge=1, le=3)
    planner_max_output_tokens: Literal[3000, 6000, 8000] = 3000
