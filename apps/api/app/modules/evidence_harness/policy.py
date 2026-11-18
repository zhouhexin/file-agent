"""建议白名单校验；只给出是否可作为候选，不修改设置、不执行模型实验。"""

from app.modules.evidence_harness.schemas import FeedbackPacket, OptimizationProposal, ProposalValidation


ALLOWED_CHANGES = {"max_output_tokens": [3000, 6000, 8000], "timeout_seconds": [60, 120]}


def validate_proposal(proposal: OptimizationProposal, packet: FeedbackPacket) -> ProposalValidation:
    """核验真实反馈引用、当前基线、单变量和相同条件下已失败的历史Profile。"""
    def reject(reason):
        return ProposalValidation(status="REJECTED", reason_code=reason)

    baseline = next(r for r in packet.reports if r.report_ref == packet.baseline_ref)
    lookup = {t.feedback_id: r.condition_ref for r in packet.reports for t in r.trials}
    history = getattr(packet, "experiment_history", [])
    lookup.update({t.feedback_id: h.condition_ref for h in history for t in h.trials})
    if len(set(proposal.feedback_refs)) != len(proposal.feedback_refs):
        return reject("DUPLICATE_FEEDBACK_REFERENCE")
    if any(ref not in lookup for ref in proposal.feedback_refs):
        return reject("UNKNOWN_FEEDBACK_REFERENCE")
    if any(lookup[ref] != baseline.condition_ref for ref in proposal.feedback_refs):
        return reject("FOREIGN_FEEDBACK_CONTEXT")
    if proposal.decision == "NO_CHANGE":
        return ProposalValidation(status="NO_CHANGE", reason_code="NO_CHANGE_PROPOSED")
    change = proposal.change
    if change.field not in ALLOWED_CHANGES:
        return reject("FIELD_NOT_ALLOWED")
    if change.from_value != getattr(baseline.budget, change.field):
        return reject("BASELINE_VALUE_MISMATCH")
    if change.to == change.from_value:
        return reject("UNCHANGED_VALUE")
    if change.to not in ALLOWED_CHANGES[change.field]:
        return reject("VALUE_NOT_ALLOWED")
    candidate = baseline.budget.model_dump()
    candidate[change.field] = change.to
    if any(h.condition_ref == baseline.condition_ref and h.control_budget == baseline.budget
           and h.candidate_budget.model_dump() == candidate and h.execution_stage == "COMPLETED"
           and h.decision in {"NO_GAIN", "REGRESSION"} for h in history):
        return reject("PREVIOUSLY_UNSUCCESSFUL_EXPERIMENT")
    # 这里只能认定已有执行失败；不凭缺少Gold的反馈推断完整的NO_GAIN或准确率。
    if any(r.condition_ref == baseline.condition_ref and r.budget.model_dump() == candidate
           and (r.failed_count or r.partial_count) for r in packet.reports):
        return reject("PREVIOUSLY_UNSUCCESSFUL_PROFILE")
    return ProposalValidation(status="VALIDATED", reason_code="SINGLE_VARIABLE_ALLOWED")
