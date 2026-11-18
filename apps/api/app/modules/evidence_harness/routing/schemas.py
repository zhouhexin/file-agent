"""路由实验严格协议，原任务输入、人工标签与模型建议分别校验。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.modules.evidence_evaluation.schemas import StrictModel
from app.modules.evidence_harness.schemas import Hash

Route = Literal["CONTENT_ANSWER", "FILE_LIST", "OTHER"]
Strategy = Literal["baseline", "content_question_priority_v1"]


class RoutingDocument(StrictModel):
    """只保留原上下文元数据，不把参考文件或Gold转换成附件。"""

    document_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(default="", max_length=255)
    content_type: str = Field(default="", max_length=120)
    status: str = Field(default="", max_length=50)
    ingest_status: str = Field(default="", max_length=50)


class SourceSignal(StrictModel):
    """建议模型可见的历史执行信号，仅受控枚举和布尔值。"""

    observed_route: Route
    entry_tool: Literal["evidence-answer", "hybrid-search", "managed-file-list", "OTHER"]
    evidence_answer_called: bool
    run_failed: bool


class RoutingCase(StrictModel):
    """从真实运行冻结的原句和实际附件，源身份使用散列追溯。"""

    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    source_run_ref: Hash
    question: str = Field(min_length=1, max_length=4000)
    attachments: list[RoutingDocument] = Field(default_factory=list, max_length=50)
    context_documents: list[RoutingDocument] = Field(default_factory=list, max_length=50)
    adaptive_mode: Literal["legacy", "shadow", "enabled"]
    observation: SourceSignal


class RoutingInputs(StrictModel):
    """同版实验使用固定Catalog，记录当前导出策略版本与来源类型。"""

    schema_version: Literal["routing-inputs-v1"] = "routing-inputs-v1"
    context_version: Literal["routing-context-metadata-v1"] = "routing-context-metadata-v1"
    source: Literal["real", "fake"]
    catalog_snapshot: dict
    source_catalog_refs: list[str]
    cases: list[RoutingCase] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_cases(self):
        """每题代表一个独立源运行，防止重复记录被算成更多样本。"""
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("DUPLICATE_ROUTING_CASE")
        if len({c.source_run_ref for c in self.cases}) != len(self.cases):
            raise ValueError("DUPLICATE_SOURCE_RUN")
        return self


class RoutingGold(StrictModel):
    """只供独立裁判读取，确认必须由操作者显式填写。"""

    case_id: str
    expected_route: Route | None = None
    reason: str = Field(default="", max_length=2000)
    confirmed: bool = False


class RoutingDataset(StrictModel):
    """冻结任务输入与Gold散列，避免比较时改变预期。"""

    schema_version: Literal["routing-dataset-v1"] = "routing-dataset-v1"
    inputs: RoutingInputs
    gold: list[RoutingGold]
    inputs_sha256: Hash
    gold_sha256: Hash


class RoutingProposal(StrictModel):
    """LLM只选择受控策略或NO_CHANGE，不提供任意提示词或执行步骤。"""

    decision: Literal["NO_CHANGE", "PROPOSE_EXPERIMENT"]
    feedback_refs: list[Hash] = Field(min_length=1, max_length=8)
    strategy_id: Literal["content_question_priority_v1"] | None
    hypothesis: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_decision(self):
        """拒绝NO_CHANGE夹带策略或实验建议缺少候选。"""
        if (self.decision == "NO_CHANGE") != (self.strategy_id is None):
            raise ValueError("ROUTING_DECISION_MISMATCH")
        return self


class RoutingAuthorization(StrictModel):
    """操作者固定请求与整轮时间上限，预算无法由模型建议覆盖。"""

    max_total_requests: Annotated[int, Field(ge=1, le=1000)] = 19
    max_active_seconds: Annotated[int, Field(ge=1, le=7200)] = 1800
    repetitions: Annotated[int, Field(ge=1, le=3)] = 1
    # 建议器是独立调用阶段，额度由操作者授权，模型不能通过建议提高额度。
    advisor_max_output_tokens: Literal[3000, 6000, 8000] = 3000
