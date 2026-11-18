"""离线反馈的严格协议；内部追溯信息与未来建议模型的输入分别定义。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.modules.evidence_evaluation.schemas import StrictModel, IssueType
from app.modules.llm.diagnostics import SafeErrorDiagnostic

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(ge=0)]


class RuntimeBudget(StrictModel):
    """从真实报告提取已使用预算，只记录数值，不授权执行调整。"""

    max_input_chars: Annotated[int, Field(gt=0)]
    max_output_tokens: Annotated[int, Field(gt=0)]
    max_calls: Annotated[int, Field(gt=0)]
    timeout_seconds: Annotated[int, Field(gt=0)]
    max_retries: Count


class CaseStatistics(StrictModel):
    """只保留散列、计数和长度，题干、文件名、引用原文不进入反馈。"""

    case_ref: Hash
    input_sha256: Hash
    evidence_count: Count
    question_chars: Count
    quote_chars: Count


class TrialSignal(StrictModel):
    """建议模型允许读取的执行信号，不包含人工标签或语义结论文本。"""

    feedback_id: Hash
    case_ref: Hash
    repetition: Annotated[int, Field(gt=0)]
    evaluation_status: Literal["DISABLED", "COMPLETED", "PARTIAL", "EVALUATION_FAILED"]
    version_status: Literal["COMPLETED", "PARTIAL", "NOT_EVALUATED", "EVALUATION_FAILED"]
    semantic_status: Literal["COMPLETED", "PARTIAL", "NOT_EVALUATED", "EVALUATION_FAILED"]
    semantic_judgment_available: bool
    duration_ms: Count
    llm_calls: Count
    input_tokens: Count | None
    output_tokens: Count | None
    token_usage_complete: bool
    failure_kinds: list[Literal[
        "OUTPUT_TRUNCATED", "UNCLASSIFIED_FAILURE", "NETWORK_ERROR", "TIMEOUT",
        "QUOTA_EXHAUSTED", "AUTHENTICATION_FAILED", "PERMISSION_DENIED", "RATE_LIMITED",
        "PROVIDER_ERROR", "REQUEST_REJECTED", "RESPONSE_FORMAT_INVALID", "SCHEMA_INVALID",
        "EVIDENCE_VALIDATION_FAILED", "CONFIGURATION_ERROR", "INTERNAL_ERROR", "UNKNOWN_LLM_ERROR",
    ]]
    diagnostics: list[SafeErrorDiagnostic]


class RoundFeedback(TrialSignal):
    """内部记录可观察到的有效语义标签；失败永不解释成NORMAL或MISSING。"""

    semantic_outcome: Literal["NO_ISSUES_DETECTED", "ISSUES_DETECTED", "UNAVAILABLE"]
    issue_types: list[IssueType]


class AttemptSignal(StrictModel):
    """已发生的尝试，仅描述执行是否完整，不凭空宣称策略有效。"""

    report_ref: Hash
    mode: Literal["live", "rules", "replay"]
    config_sha256: Hash
    condition_ref: Hash
    attempt_ref: Hash
    model_ref: Hash
    endpoint_ref: Hash
    prompt_sha256: Hash
    evaluator_version: Literal["evidence-eval-v1"]
    code_ref: Hash
    budget: RuntimeBudget
    run_count: Count
    completed_count: Count
    failed_count: Count
    partial_count: Count
    observation: Literal["ALL_COMPLETED", "HAS_FAILURES", "INCOMPLETE"]
    trials: list[TrialSignal]


class ReportFeedback(AttemptSignal):
    """内部报告保留语义观察；必须显式投影成AttemptSignal才可交给建议模型。"""

    trials: list[RoundFeedback]


class OptimizerInput(StrictModel):
    """A阶段模型载荷：仅失败信号、结构统计、预算与历史尝试，无Gold和原文。"""

    schema_version: Literal["harness-optimizer-input-v1"] = "harness-optimizer-input-v1"
    stage: Literal["FEEDBACK_READY"] = "FEEDBACK_READY"
    baseline_ref: Hash
    cases: list[CaseStatistics]
    attempts: list[AttemptSignal]


class FeedbackPacket(StrictModel):
    """本机可追溯反馈，人工标签只保留散列，不混入未来模型输入。"""

    schema_version: Literal["harness-feedback-v1"] = "harness-feedback-v1"
    dataset_sha256: Hash
    annotation_sha256: Hash
    baseline_ref: Hash
    cases: list[CaseStatistics]
    reports: list[ReportFeedback]

    def optimizer_input(self) -> OptimizerInput:
        """逐字段投影，禁止把内部Gold散列、问题标签或未来扩展字段默认透传。"""
        attempts = []
        for report in self.reports:
            data = {name: getattr(report, name) for name in AttemptSignal.model_fields if name != "trials"}
            data["trials"] = [
                TrialSignal(**{name: getattr(trial, name) for name in TrialSignal.model_fields})
                for trial in report.trials
            ]
            attempts.append(AttemptSignal(**data))
        return OptimizerInput(baseline_ref=self.baseline_ref, cases=self.cases, attempts=attempts)


class ProposalChange(StrictModel):
    """单变量建议；字段名先按结构解析，再由服务端白名单决定是否允许。"""

    field: Annotated[str, Field(pattern=r"^[a-z_]{1,48}$")]
    from_value: Annotated[int, Field(ge=0)] = Field(alias="from")
    to: Annotated[int, Field(ge=0)]


class OptimizationProposal(StrictModel):
    """模型只能提出一个调整或NO_CHANGE，不能输出路径、预算授权或执行步骤。"""

    decision: Literal["NO_CHANGE", "PROPOSE_EXPERIMENT"]
    target: Literal["evaluation_runtime"]
    feedback_refs: list[Hash] = Field(min_length=1, max_length=8)
    change: ProposalChange | None
    hypothesis: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def check_decision(self):
        """拒绝NO_CHANGE夹带动作或提出实验却没有变更。"""
        if (self.decision == "NO_CHANGE") != (self.change is None):
            raise ValueError("DECISION_CHANGE_MISMATCH")
        return self


class ProposalValidation(StrictModel):
    """策略校验只产生固定状态/原因，不代表已有执行器或可直接上线。"""

    status: Literal["VALIDATED", "NO_CHANGE", "REJECTED"]
    reason_code: Literal[
        "SINGLE_VARIABLE_ALLOWED", "NO_CHANGE_PROPOSED", "PROPOSAL_SCHEMA_INVALID",
        "UNSAFE_PROPOSAL_TEXT", "UNKNOWN_FEEDBACK_REFERENCE", "DUPLICATE_FEEDBACK_REFERENCE",
        "FOREIGN_FEEDBACK_CONTEXT", "FIELD_NOT_ALLOWED", "VALUE_NOT_ALLOWED",
        "BASELINE_VALUE_MISMATCH", "UNCHANGED_VALUE", "PREVIOUSLY_UNSUCCESSFUL_PROFILE",
        "PREVIOUSLY_UNSUCCESSFUL_EXPERIMENT",
    ]


class ExperimentProfile(StrictModel):
    """只供离线执行器使用的实际预算，120秒不经过普通env的60秒裁剪。"""

    max_input_chars: Annotated[int, Field(ge=16000, le=120000)]
    max_output_tokens: Literal[3000, 6000, 8000]
    max_calls: Annotated[int, Field(ge=1, le=5)]
    timeout_seconds: Literal[60, 120]
    max_retries: Annotated[int, Field(ge=0, le=0)]


class ExecutionAuthorization(StrictModel):
    """操作者冻结的总上限，模型建议不能修改；已消费的建议请求也计入19次。"""

    max_total_requests: Annotated[int, Field(ge=1, le=19)] = 19
    max_active_seconds: Annotated[int, Field(ge=1, le=1800)] = 1800


class HistoryTrial(TrialSignal):
    """历史实验的安全执行信号，标记组别但不携带Gold、业务文本或语义标签。"""

    arm: Literal["control", "candidate"]


class ExperimentHistory(StrictModel):
    """由独立比较器重算的历史摘要，不允许模型或手填摘要宣布实验效果。"""

    experiment_ref: Hash
    condition_ref: Hash
    source: Literal["live", "fake"]
    decision: Literal["IMPROVED", "NO_GAIN", "REGRESSION", "INCONCLUSIVE"]
    execution_stage: Literal["COMPLETED", "STOPPED", "INTERRUPTED", "FAILED", "UNFINALIZED"]
    control_budget: RuntimeBudget
    candidate_budget: RuntimeBudget
    execution_code_ref: Hash
    trials: list[HistoryTrial]


class OptimizerInputV2(OptimizerInput):
    """新增历史反馈的载荷版本；不改变旧版本序列化或旧建议的输入绑定。"""

    schema_version: Literal["harness-optimizer-input-v2"] = "harness-optimizer-input-v2"
    experiment_history: list[ExperimentHistory]


class FeedbackPacketV2(FeedbackPacket):
    """新版本反馈追加实验历史，旧反馈及原始实验始终保持不可变。"""

    schema_version: Literal["harness-feedback-v2"] = "harness-feedback-v2"
    experiment_history: list[ExperimentHistory]

    def optimizer_input(self) -> OptimizerInputV2:
        """沿用原安全投影，只追加严格的历史信号，不默认透传内部追溯数据。"""
        base = super().optimizer_input().model_dump(mode="json")
        base.pop("schema_version")
        return OptimizerInputV2(**base, experiment_history=self.experiment_history)
