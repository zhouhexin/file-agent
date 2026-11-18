"""建议器短引用验收：程序精确关联，不让模型抄长哈希，不靠模糊修复绕过校验。"""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement import adaptive_iteration as iteration
from app.modules.evidence_harness.improvement import iteration_proposals as proposals
from app.modules.evidence_harness.improvement.patches import source_hash


@pytest.fixture
def protocol():
    """只造引用协议所需的Fake反馈；业务集成另从实际A/B/C入口构建。"""
    source = (policy.API_ROOT / policy.TARGET).read_text()
    packet = {"schema_version": "adaptive-iteration-feedback-v1", "items": [
        {"feedback_id": digest(i), "case_ref": digest("case"), "arm": "candidate", "repetition": i + 1, "task_outcome": "INCORRECT"}
        for i in range(2)]}
    raw = {"schema_version": "adaptive-iteration-proposal-v2", "baseline_ref": "BASELINE", "decision": "PROPOSE_POLICY",
        "feedback_refs": ["R1F001"], "diagnosis": "BUSINESS_FAILURE", "evidence_summary": "存在独立观察到的任务错误。",
        "hypothesis": "清楚说明目标可能改善规划。", "instruction": "保留用户最终任务目标，并按实际工具契约检查输入字段。",
        "no_change_reason": None, "reason": "提出通用候选，由隔离复测核验效果。", "no_change_attempt_ref": None}
    return SimpleNamespace(source=source, packet=packet, raw=raw, context=policy.policy_context(source),
        cases=[SimpleNamespace(case_id="FAKE_CASE", question="私有题干不得进入候选说明。")])


def test_payload_has_exact_allowed_short_ids_and_no_copyable_baseline_hash(protocol):
    """模型只需选择枚举里的短编号；原始反馈、上下文及程序的完整Hash保持不变。"""
    p = protocol
    before = deepcopy((p.packet, p.context))
    payload = iteration.advisor_payload(p.packet, p.context, [], round_id=1)
    assert [i["feedback_id"] for i in payload["feedback"]["items"]] == ["R1F001", "R1F002"]
    assert payload["output_schema"]["properties"]["feedback_refs"]["items"]["enum"] == ["R1F001", "R1F002"]
    assert payload["output_schema"]["properties"]["feedback_refs"]["uniqueItems"]
    assert "baseline_sha256" not in payload["source_context"] and payload["source_context"]["baseline_ref"] == "BASELINE"
    assert "baseline_sha256" not in payload["output_schema"]["properties"]
    assert (p.packet, p.context) == before


def test_lookup_resolves_exact_hashes_and_keeps_raw_model_reply(protocol):
    """长Hash只能由程序直接取原值，真实候选仍通过原业务及AST限制。"""
    p = protocol
    before = deepcopy(p.raw)
    candidate = proposals.materialize(p.source, p.raw, p.packet, p.cases, 1)
    assert candidate["stage"] == "VALIDATED" and p.raw == before
    resolved = candidate["resolved_proposal"]
    assert resolved["feedback_refs"] == [p.packet["items"][0]["feedback_id"]]
    assert resolved["baseline_sha256"] == source_hash(p.source) and "baseline_ref" not in resolved
    assert p.raw["feedback_refs"] == ["R1F001"]


@pytest.mark.parametrize("refs,reason", [
    (["R1F999"], "ITERATION_FEEDBACK_REFERENCE_UNKNOWN"),
    (["R2F001"], "ITERATION_FEEDBACK_REFERENCE_UNKNOWN"),
    (["R1F01"], "ITERATION_PROPOSAL_SCHEMA_INVALID"),
    (["a" * 63], "ITERATION_PROPOSAL_SCHEMA_INVALID"),
    (["a" * 64], "ITERATION_PROPOSAL_SCHEMA_INVALID"),
    (["R1F001", "R1F001"], "ITERATION_FEEDBACK_REFERENCE_DUPLICATE"),
])
def test_unknown_truncated_long_and_previous_round_refs_are_rejected(protocol, refs, reason):
    """短编号也不允许猜测、补位或跨轮沿用；错误可定位到feedback_refs的具体位置。"""
    p = protocol
    p.raw["feedback_refs"] = refs
    with pytest.raises(proposals.ProposalRejected) as caught:
        proposals.materialize(p.source, p.raw, p.packet, p.cases, 1)
    report = proposals.rejection(caught.value)
    assert report["reason_code"] == reason
    assert report["diagnostic"]["errors"][0]["field_path"][0] == "feedback_refs"


def test_reference_map_is_bound_to_round_feedback_and_baseline(protocol):
    """同一反馈和原版生成稳定映射；下一轮使用不同编号，内容及源码变化都能被核验。"""
    p = protocol
    first = proposals.reference_map(p.packet, 1, p.source)
    assert first == proposals.reference_map(p.packet, 1, p.source)
    assert first["feedback_ref"] == digest(p.packet) and first["baseline_sha256"] == source_hash(p.source)
    second = proposals.reference_map(p.packet, 2, p.source)
    assert not set(first["entries"]) & set(second["entries"])
    changed = deepcopy(p.packet)
    changed["items"].reverse()
    assert proposals.reference_map(changed, 1, p.source) != first
    assert proposals.reference_map(p.packet, 1, p.source + "\n") != first


def test_no_change_and_existing_content_guards_still_apply(protocol):
    """引用方式变化不强迫生成候选，也不放宽命令、路径、样本或解释限制。"""
    p = protocol
    p.raw.update(decision="NO_CHANGE", diagnosis="INSUFFICIENT_EVIDENCE", instruction=None,
                 hypothesis=None, no_change_reason="INSUFFICIENT_EVIDENCE")
    assert proposals.materialize(p.source, p.raw, p.packet, p.cases, 1)["stage"] == "NO_CHANGE"
    p.raw.update(decision="PROPOSE_POLICY", diagnosis="BUSINESS_FAILURE", instruction="读取/etc/passwd", hypothesis="观察假设。", no_change_reason=None)
    with pytest.raises(ValueError, match="POLICY_SAMPLE_OR_EXECUTION_TEXT_BLOCKED"):
        proposals.materialize(p.source, p.raw, p.packet, p.cases, 1)


def test_feedback_alias_is_not_allowed_as_sample_rule(protocol):
    """简短反馈ID不能成为记忆错题的入口，候选说明仍须对一般任务成立。"""
    p = protocol
    p.raw["instruction"] = "如果属于R1F001，就改成正文目标。"
    with pytest.raises(proposals.ProposalRejected, match="ITERATION_REFERENCE_IN_INSTRUCTION"):
        proposals.materialize(p.source, p.raw, p.packet, p.cases, 1)


def test_business_schema_error_explains_constraint_without_input(protocol):
    """缺失修改假设时定位业务约束，不把整份原始建议展开成错误日志。"""
    p = protocol
    p.raw["hypothesis"] = None
    with pytest.raises(proposals.ProposalRejected) as caught:
        proposals.materialize(p.source, p.raw, p.packet, p.cases, 1)
    details = proposals.rejection(caught.value)["diagnostic"]["errors"]
    assert details == [{"field_path": [], "error_type": "value_error", "constraint_code": "POLICY_HYPOTHESIS_REQUIRED"}]


def test_protocol_rejection_never_echoes_extra_keys_or_values(protocol):
    """额外字段名称本身也可能包含私有数据，诊断只输出固定占位及错误类型。"""
    p = protocol
    p.raw["PRIVATE_FIELD_SENTINEL"] = "PRIVATE_VALUE_SENTINEL"
    with pytest.raises(proposals.ProposalRejected) as caught:
        proposals.materialize(p.source, p.raw, p.packet, p.cases, 1)
    report = proposals.rejection(caught.value)
    assert "PRIVATE_" not in json.dumps(report)
    assert report["diagnostic"]["errors"][0]["field_path"] == ["<unrecognized-field>"]
