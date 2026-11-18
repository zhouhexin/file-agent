"""建议器历史长度验收：保留完整计数和来源身份，摘要不能改写Gold、成绩或停止事实。"""

from copy import deepcopy
import json

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.executor import ExperimentStopped
from app.modules.evidence_harness.improvement import advisor_memory as memory, campaign, adaptive_iteration as iteration
from app.modules.evidence_harness.improvement import iteration_comparator as comparator, campaign_retesting as retesting
from app.modules.evidence_harness.improvement.isolation import file_manifest
from app.tests.test_harness_campaign import routing, source, c_data, fast_os, isolated_provider_env, factory, options


def item(arm="candidate", correct=False, number=0):
    """模拟已受控生成的机器诊断；重复错误仍须全部进入统计，不引入人工预期答案。"""
    return {"feedback_id": f"H1R1F{number:03d}", "case_ref": digest(number), "arm": arm,
        "task_outcome": "CORRECT" if correct else "INCORRECT", "execution_state": "MODEL_FAILED",
        "effective_mode": "LEGACY", "fallback_kind": "ADAPTIVE_REJECTED", "optimization_scope": "PROMPT_POLICY_UNVERIFIED",
        "task_diagnostic": {"final_task_goal": {"original_request": "原始任务文字" * 600},
            "intent_contract": {"mapping_status": "UNRECOGNIZED", "observed_initial_route": "OTHER"},
            "mismatch_observations": [{"code": "UNRECOGNIZED_TASK_INTENT_FOR_DISCOVERY", "certainty": "OBSERVED"}]},
        "parameter_diagnostics": [{"code": "TOOL_PARAMETER_CONSTRAINT", "field_path": ["query"], "limits": {"max_length": 500}}],
        "validation_events": [{"phase": "ADAPTIVE", "status": "REJECTED"}], "violation_codes": ["TOOL_SCHEMA_INVALID"],
        "call_diagnostics": [{"category": "PROVIDER_ERROR", "stage": "LLM_REQUEST", "exception_type": "LLMResponseError",
            "http_status": 429, "output_truncated": False}]}


def historical(count=2, rows=4):
    """大量原始逐题文字用于触发旧版上限，压缩只改变模型视图，完整对象不修改。"""
    return [{"origin": "VERIFIED_DEVELOPMENT_HISTORY", "round_id": n+1, "instruction": "通用规划说明。" * 300,
        "policy_ref": digest(n), "decision": "REGRESSION", "strategy_evaluable": False,
        "profile": {"provider": "FAKE", "model_ref": digest(n % 2)}, "candidate_parameter_failures": rows,
        "arms": {"control": {"correct": 0, "planned": rows}, "candidate": {"correct": 0, "planned": rows}},
        "observations": {"items": [item(number=i) for i in range(rows)]}}
        for n in range(count)]


def test_all_errors_counted_with_arm_boundaries():
    """统计全部试次和错误，调用事件次数不能与示例条数混淆。"""
    packet = {"items": [item(number=0), item(number=1), item("control", True, 2)]}
    summary = memory.observation_summary(packet)
    assert summary["item_count"] == 3
    assert summary["arms"]["candidate"]["items"] == 2 and summary["arms"]["control"]["items"] == 1
    counts = summary["arms"]["candidate"]["counts"]
    assert counts["task_outcome"] == [{"value": "INCORRECT", "count": 2}]
    assert counts["call_events"][0]["value"]["http_status"] == 429 and counts["call_events"][0]["count"] == 2
    assert counts["parameter_events"][0]["count"] == 2 and counts["mismatch_events"][0]["count"] == 2
    assert summary["examples"] == [] and summary["causal_explanation"] == "NOT_VERIFIED"


def test_large_history_is_bounded_deterministic_and_keeps_every_attempt():
    """80份候选的原报告超过40万字符，摘要保留全部尝试、计数和明确的省略标记。"""
    history = historical(80, rows=10)
    before = digest(history)
    base = {"feedback": {"full_current_feedback": "CURRENT_SENTINEL"}, "source_context": {}, "output_schema": {}}
    assert memory.request_characters("prompt", {**base, "history": history}) > 400000
    view = memory.project(base, history, "prompt")
    assert view == memory.project(base, history, "prompt") and digest(history) == before
    assert len(view["history"]) == 80 and view["history_projection"]["attempts"] == 80
    assert memory.request_characters("prompt", view) <= memory.POLICY["request_characters"]
    assert len(str({k: view[k] for k in ("history", "history_projection")})) <= memory.POLICY["history_characters"]
    assert len(view["history_projection"]["profiles"]) == 2 and view["history_projection"]["source_ref"] == before
    assert view["history_projection"]["omitted_instruction_characters"] > 0
    examples = []
    for original, row in zip(history, view["history"]):
        assert row["policy_ref"] == original["policy_ref"] and row["arms"] == original["arms"]
        assert row["observations"]["arms"]["candidate"]["counts"]["task_outcome"][0]["count"] == 10
        assert row["instruction_projection"]["source_ref"] == digest(original["instruction"])
        examples.extend(row["observations"]["examples"])
    assert sum(len(str(e)) for e in examples) <= memory.POLICY["example_characters"]
    assert view["feedback"] == base["feedback"]


def test_current_feedback_and_short_reference_enum_stay_full():
    """只缩历史，当前完整反馈与本轮精确短编号仍和旧v3投影完全相同。"""
    packet = {"items": [{**item(number=1), "feedback_id": digest("current")}]}
    original = deepcopy(packet)
    old = iteration.advisor_payload(packet, {}, historical(), version="v3", round_id=10)
    new = iteration.advisor_payload(packet, {}, historical(), version="v4", round_id=10, prompt="prompt")
    assert old["feedback"] == new["feedback"] and packet == original
    assert new["output_schema"] == old["output_schema"] and new["source_context"] == old["source_context"]
    assert new["output_schema"]["properties"]["feedback_refs"]["items"]["enum"] == ["R10F001"]
    assert memory.audit("prompt", new)["request_ref"] == digest(["prompt", new])


def test_required_context_and_summary_overflow_stop_without_silent_drops(monkeypatch):
    """放不下必需内容就明确拒绝，不提额度、不删错误、不截断JSON。"""
    with pytest.raises(ExperimentStopped, match="ADVISOR_REQUIRED_CONTEXT_TOO_LARGE"):
        memory.project({"feedback": "x" * 360001}, [], "prompt")
    with pytest.raises(ExperimentStopped, match="ADVISOR_HISTORY_ATTEMPT_LIMIT"):
        memory.project({}, [{}] * 101, "prompt")
    monkeypatch.setitem(memory.POLICY, "history_characters", 100)
    with pytest.raises(ExperimentStopped, match="ADVISOR_HISTORY_SUMMARY_TOO_LARGE"):
        memory.project({}, historical(), "prompt")


@pytest.mark.parametrize("tamper", ["alter", "remove"])
def test_memory_audit_tampering_is_rejected_even_if_resealed(routing, source, tamper):
    """摘要不是可信事实来源：重封清单也不能让篡改的错误统计通过独立重算。"""
    opts = options(routing, source, authorization=campaign.CampaignAuthorization(max_rounds=1, repetitions=1, max_total_requests=25))
    report = campaign.run(**opts, backend_factory=factory())
    assert report["stage"] == "COMPLETED", report
    root = opts["output"]
    path = root / "rounds/0001/advisor-memory.json"
    raw = json.loads(path.read_text())
    assert raw["request_characters"] <= 360000
    if tamper == "alter":
        raw["history_projection"]["example_count"] = 999
        path.write_text(json.dumps(raw))
    else:
        path.unlink()  # 只删除本测试新建的临时摘要，保护实际调用发生后摘要不得缺失的约束。
    manifest = file_manifest(root)
    for name in ("integrity.json", "comparison.json", "summary.md"):
        manifest.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="ADVISOR_MEMORY_CHANGED" if tamper == "alter" else "ADVISOR_MEMORY_MISSING"):
        comparator._compare(root)
    assert comparator.compare(root)["decision"] == "INVALID_EXPERIMENT"


def test_later_memory_stop_is_recorded_and_retest_still_refuses_partial_source(routing, source, monkeypatch):
    """后续轮放不下时保留已完成轮和停止事实，不能偷偷将STOPPED放行成完整搜索。"""
    monkeypatch.setitem(memory.POLICY, "maximum_attempts", 1)
    opts = options(routing, source, authorization=campaign.CampaignAuthorization(max_rounds=2, repetitions=1, max_total_requests=50))
    result = campaign.run(**opts, backend_factory=factory())
    if result["decision"] == "INVALID_EXPERIMENT": comparator._compare(opts["output"])
    assert result["stage"] == "STOPPED" and result["stop_reason"] == "ADVISOR_HISTORY_ATTEMPT_LIMIT", result
    assert result["advisor_calls"] == 1 and result["rounds"][1]["proposal_stage"] == "NOT_RUN"
    with pytest.raises(ValueError, match="RETEST_SOURCE_NOT_COMPLETED"):
        retesting.check(campaign_dir=opts["output"], output=routing.root / "experiments/harness-v1/forbidden",
            settings=routing.settings, authorization=retesting.RetestAuthorization(max_total_requests=240), source="fake")
