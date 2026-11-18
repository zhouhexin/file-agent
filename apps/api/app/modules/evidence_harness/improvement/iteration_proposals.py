"""建议器短引用协议：模型选择本轮编号，可信宿主精确关联原反馈及源码，不猜测修补。"""

from copy import deepcopy
import re
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_evaluation.schemas import StrictModel
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement.patches import source_hash, rejection_code


class ShortReferenceProposal(StrictModel):
    """只让模型引用短编号；业务解释的完整校验继续复用原PolicyProposal。"""

    schema_version: Literal["adaptive-iteration-proposal-v2"]
    baseline_ref: Literal["BASELINE"]
    decision: Literal["NO_CHANGE", "PROPOSE_POLICY"]
    feedback_refs: list[Annotated[str, Field(pattern=r"^R[1-3]F[0-9]{3}$")]] = Field(min_length=1, max_length=8)
    diagnosis: Literal["BUSINESS_FAILURE", "EXECUTION_FAILURE", "NO_ACTIONABLE_FAILURE", "INSUFFICIENT_EVIDENCE"]
    evidence_summary: str = Field(min_length=1, max_length=800)
    hypothesis: str | None = Field(max_length=800)
    instruction: str | None = Field(max_length=3000)
    no_change_reason: Literal["NO_ACTIONABLE_FAILURE", "INSUFFICIENT_EVIDENCE", "OUTSIDE_ALLOWED_SCOPE"] | None
    reason: str = Field(min_length=1, max_length=800)
    no_change_attempt_ref: None = None


class ExplorationProposal(ShortReferenceProposal):
    """新探索协议支持十轮，旧三轮反馈协议保持原样供历史重放。"""

    schema_version: Literal["adaptive-exploration-proposal-v1"]
    feedback_refs: list[Annotated[str, Field(pattern=r"^R(?:[1-9]|10)F[0-9]{3}$")]] = Field(min_length=1, max_length=8)


class ProposalRejected(ValueError):
    """安全拒绝只携带固定原因和字段位置，不携带模型原始输入或异常全文。"""

    def __init__(self, code, errors):
        """错误码用于回执，详情只保留可公开的协议字段和固定分类。"""
        super().__init__(code)
        self.errors = errors


def reference_map(packet, round_id, source, *, extended=False):
    """每轮独立建立短编号映射，绑定完整反馈和原版源码；编号不具备权限。"""
    if type(round_id) is not int or not 1 <= round_id <= (10 if extended else 3):
        raise ValueError("ITERATION_REFERENCE_ROUND_INVALID")
    items = packet["items"]
    refs = [i["feedback_id"] for i in items]
    if (not 1 <= len(refs) <= 999 or len(set(refs)) != len(refs)
        or any(not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{64}", ref) for ref in refs)):
        raise ValueError("ITERATION_REFERENCE_MAP_INVALID")
    return {"schema_version": "adaptive-iteration-reference-map-v1", "round_id": round_id,
        "feedback_ref": digest(packet), "baseline_sha256": source_hash(source),
        "entries": {f"R{round_id}F{n:03d}": ref for n, ref in enumerate(refs, 1)}}


def advisor_view(packet, context, round_id, *, extended=False):
    """替换模型需引用的长ID；完整映射留在宿主，原反馈和上下文不被原地修改。"""
    visible, source_context = deepcopy(packet), deepcopy(context)
    source_context.pop("baseline_sha256", None)
    source_context["baseline_ref"] = "BASELINE"
    for n, item in enumerate(visible["items"], 1):
        item["feedback_id"] = f"R{round_id}F{n:03d}"
    visible.update(schema_version="adaptive-iteration-feedback-v2", round_id=round_id)
    schema = (ExplorationProposal if extended else ShortReferenceProposal).model_json_schema()
    # 枚举来自当前真实反馈，不能只靠一句“请原样复制”约束自由字符串。
    schema["properties"]["feedback_refs"]["items"] = {"type": "string", "enum": [i["feedback_id"] for i in visible["items"]]}
    schema["properties"]["feedback_refs"]["uniqueItems"] = True
    return visible, source_context, schema


def _schema_errors(exc, fields):
    """额外键也可能包含私有内容，只允许固定字段名及反馈引用的整数下标。"""
    rows = []
    for error in exc.errors(include_input=False, include_url=False):
        loc = error["loc"]
        path = [loc[0] if loc and loc[0] in fields else "<unrecognized-field>"] if loc else []
        if len(loc) == 2 and path == ["feedback_refs"] and type(loc[1]) is int:
            path.append(loc[1])
        row = {"field_path": path, "error_type": error["type"]}
        constraint = str((error.get("ctx") or {}).get("error", ""))
        if constraint in {"POLICY_EXPLANATION_EMPTY", "POLICY_HYPOTHESIS_REQUIRED", "POLICY_NO_CHANGE_INVALID", "POLICY_DIAGNOSIS_MISMATCH"}:
            row["constraint_code"] = constraint
        rows.append(row)
    return rows


def resolve_proposal(raw, mapping, *, extended=False):
    """只接收本轮确切编号，拒绝旧轮、截断、未知及重复引用；不补字、不模糊匹配。"""
    try:
        proposal = (ExplorationProposal if extended else ShortReferenceProposal).model_validate(raw)
    except ValidationError as exc:
        raise ProposalRejected("ITERATION_PROPOSAL_SCHEMA_INVALID", _schema_errors(exc, ShortReferenceProposal.model_fields)) from None
    resolved, seen = [], set()
    for index, ref in enumerate(proposal.feedback_refs):
        if ref not in mapping["entries"] or ref in seen:
            code = "ITERATION_FEEDBACK_REFERENCE_UNKNOWN" if ref not in mapping["entries"] else "ITERATION_FEEDBACK_REFERENCE_DUPLICATE"
            raise ProposalRejected(code, [{"field_path": ["feedback_refs", index], "error_type": code}])
        seen.add(ref)
        resolved.append(mapping["entries"][ref])
    normalized = proposal.model_dump()
    normalized.pop("baseline_ref")
    normalized.update(schema_version="adaptive-policy-proposal-v1", baseline_sha256=mapping["baseline_sha256"], feedback_refs=resolved)
    try:
        policy.PolicyProposal.model_validate(normalized)
    except ValidationError as exc:
        raise ProposalRejected("ITERATION_PROPOSAL_SCHEMA_INVALID", _schema_errors(exc, policy.PolicyProposal.model_fields)) from None
    return normalized


def materialize(source, raw, packet, cases, round_id, *, extended=False):
    """精确恢复后的协议仍须通过既有解释、来源、样本与AST限制，不开放额外修改范围。"""
    resolved = resolve_proposal(raw, reference_map(packet, round_id, source, extended=extended), extended=extended)
    pattern = r"(?<![A-Za-z0-9_])R(?:[1-9]|10)F[0-9]{3}(?![A-Za-z0-9_])" if extended else r"(?<![A-Za-z0-9_])R[1-3]F[0-9]{3}(?![A-Za-z0-9_])"
    if resolved.get("instruction") and re.search(pattern, resolved["instruction"]):
        raise ProposalRejected("ITERATION_REFERENCE_IN_INSTRUCTION", [
            {"field_path": ["instruction"], "error_type": "FEEDBACK_REFERENCE_IS_NOT_GENERAL_POLICY"}])
    candidate = policy.materialize(source, resolved, packet, cases)
    candidate["resolved_proposal"] = resolved
    return candidate


def rejection(exc):
    """将新拒绝写成明确字段诊断；旧业务拒绝继续保持固定错误码且不暴露输入。"""
    code = rejection_code(exc)
    return {"stage": "REJECTED", "reason_code": code,
        "diagnostic": {"stage": "ADVISOR_PROPOSAL_VALIDATION", "errors": exc.errors if isinstance(exc, ProposalRejected) else []}}
