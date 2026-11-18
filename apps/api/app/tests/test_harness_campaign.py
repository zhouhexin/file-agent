"""实验管理验收：继承历史、候选冻结、统一比较和锁定确认均使用Fake模型与真实规划节点。"""

from copy import deepcopy
import json
import os

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.improvement import campaign, campaign_confirmation as confirmation
from app.modules.evidence_harness.improvement import adaptive_iteration as iteration, iteration_comparator as comparator
from app.modules.evidence_harness.improvement import adaptive_policy as policy, adaptive_validation as validation
from app.modules.evidence_harness.improvement.isolation import file_manifest
from app.tests.test_adaptive_iteration import routing, c_data, fast_os, source, provider, isolated_provider_env
from app.tests.test_adaptive_validation import validation_factory


def factory(kind="improve", requests=None):
    """每份候选确由模型Fake返回不同文字；执行与评分仍由现有真实代码负责。"""
    def build(role, number, arm=None, case=None, n=None):
        """仅替换供应商，不接收Gold或候选自行评分。"""
        client = provider(kind if kind in {"no_change", "interrupt", "advisor_failure"} else "improve", requests)(role, number, arm, case, n)
        class Backend:
            """追加通用文本用以区分冻结版本，既有节点仍检查真实工具参数。"""

            def complete_json_with_usage(self, **kwargs):
                """只在建议阶段生成候选；试次中不会偷偷修改其内容。"""
                raw, usage = client.complete_json_with_usage(**kwargs)
                if role == "ADVISOR":
                    raw["schema_version"] = kwargs["user_payload"]["output_schema"]["properties"]["schema_version"]["const"]
                if role == "ADVISOR" and raw.get("instruction"):
                    suffix = " 请检查任务目标的一致性。" if number == 1 else " 请逐步核对当前的工具输入。" if number == 2 else f" 请审视第{number}类通用策略的计划边界。"
                    raw["instruction"] += suffix
                    if kind == "repeat":
                        raw["instruction"] = "请保持用户最终任务目标，并按工具契约检查通用输入，不猜测文件身份。"
                return raw, usage
        return Backend()
    return build


def options(routing, source, name="campaign", **updates):
    """新批使用独立目录和显式统一额度，不覆盖原实验或在轮次间重置费用。"""
    return {"source_dir": source, "output": routing.root / f"experiments/harness-v1/{name}", "settings": routing.settings,
        "authorization": campaign.CampaignAuthorization(max_total_requests=120, max_rounds=2, repetitions=2), "source": "fake", **updates}


@pytest.fixture
def confirmation_os(monkeypatch):
    """快速确认测试模拟OS，独立OS专项再验证真实权限边界。"""
    monkeypatch.setattr(confirmation, "probe_sandbox", lambda *a, **kw: {
        "compile": True, "read_blocked": True, "write_blocked": True, "network_blocked": True, "exec_blocked": True})
    monkeypatch.setattr(confirmation, "run_checks", lambda *a: policy.expected_checks())


def test_history_is_inherited_and_gain_does_not_end_search(routing, source):
    """先生成真实协议历史，再验证新批自动承接、冻结两份改善候选并统一比较，而非首胜就停。"""
    old = routing.root / "experiments/harness-v1/old-iteration"
    iteration.run(source_dir=source, output=old, settings=routing.settings,
        authorization=iteration.IterationAuthorization(max_total_requests=100, repetitions=1), source="fake", backend_factory=provider())
    before = file_manifest(old)
    requests = []
    opts = options(routing, old)
    checked = campaign.check(**opts)
    assert len(checked["history_sources"]) == 1 and len(checked["historical_candidates"]) == 2
    assert not opts["output"].exists()
    report = campaign.run(**opts, backend_factory=factory(requests=requests))
    if report["decision"] == "INVALID_EXPERIMENT":
        comparator._compare(opts["output"])
    assert report["decision"] == "DEVELOPMENT_SELECTED", report
    assert len(report["rounds"]) == 2 and all(r["score"]["decision"] == "IMPROVED" for r in report["rounds"])
    assert report["requests_used"] == len(requests) == 34 and report["historical_requests_used"] == 18
    assert report["lifetime_requests_used"] == 52 and report["selection"]["selected"] in {"C001", "C002"}
    assert len(report["selection"]["ranking"]) == 4
    assert all(not r["comparable"] for r in report["selection"]["ranking"] if r["id"].startswith("H"))
    first = next(r for r in requests if r["role"] == "ADVISOR")
    assert len(first["user_payload"]["history"]) == 3
    serialized = json.dumps(requests, ensure_ascii=False)
    assert "expected_route" not in serialized and "请说明活动通知的要求并引用原文。" not in serialized
    assert file_manifest(old) == before
    for root in (opts["output"] / "rounds").iterdir():
        assert (root / "frozen-evaluation.json").exists() and (root / "round-sealed.json").exists()
    actual = file_manifest(opts["output"])
    assert comparator.compare(opts["output"]) == report and file_manifest(opts["output"]) == actual


def test_lock_development_confirmation_and_holdout_without_advisor(routing, source, confirmation_os):
    """锁定统一入选者后，同一版本可独立确认和保留验收，期间不再调用建议器。"""
    opts = options(routing, source)
    report = campaign.run(**opts, backend_factory=factory())
    assert report["decision"] == "DEVELOPMENT_SELECTED", report
    lock_dir = routing.root / "experiments/harness-v1/locked"
    locked = campaign.lock(campaign_dir=opts["output"], output=lock_dir)
    assert locked["stage"] == "LOCKED" and locked["model_calls"] == 0
    for split, expected in (("DEVELOPMENT", "DEVELOPMENT_CONFIRMED"), ("HOLDOUT", "HOLDOUT_PASSED")):
        output = routing.root / f"experiments/harness-v1/confirm-{split.lower()}"
        settings = dict(lock_dir=lock_dir, split=split, output=output, settings=routing.settings,
            authorization=validation.ValidationAuthorization(max_total_requests=72, repetitions=2), source="fake")
        checked = confirmation.check(**settings)
        assert not output.exists()
        requests = []
        result = confirmation.run(**settings, backend_factory=validation_factory(requests=requests))
        if result["decision"] == "INVALID_EXPERIMENT":
            confirmation._compare(output)
        assert result["decision"] == expected, result
        assert result["policy_ref"] == locked["policy_ref"] and result["advisor_calls"] == 0
        assert result["score"]["holdout_evaluated"] == (split == "HOLDOUT")
        assert all(r["case_id"].startswith("H") for r in requests) if split == "HOLDOUT" else all(not r["case_id"].startswith("H") for r in requests)
        assert confirmation.compare(output) == result
        with pytest.raises(ValueError, match="CAMPAIGN_HISTORY_TYPE_INVALID"):
            campaign.check(**options(routing, output, name="must-not-leak"))
    second = campaign.run(**options(routing, opts["output"], name="next-batch"), backend_factory=factory("no_change"))
    assert second["historical_requests_used"] == report["requests_used"] and second["requests_used"] == 1


@pytest.mark.parametrize("kind,cap,reason", [("no_change", 120, "MODEL_SELECTED_NO_CHANGE"),
    ("repeat", 120, "REPEATED_CANDIDATE"), ("improve", 18, "REQUEST_BUDGET_EXHAUSTED"),
    ("interrupt", 120, "USER_INTERRUPTED"), ("advisor_failure", 120, "ITERATION_ADVISOR_OR_ISOLATION_FAILED")])
def test_stops_keep_all_records_and_never_increase_budget(routing, source, kind, cap, reason):
    """跨候选共用预算并保留拒绝/中断，无法完整封存的批次不产生可锁定推荐。"""
    opts = options(routing, source, authorization=campaign.CampaignAuthorization(max_total_requests=cap, max_rounds=2, repetitions=2))
    result = campaign.run(**opts, backend_factory=factory(kind))
    assert result.get("stop_reason") == reason, result
    assert result["requests_used"] <= cap and not result["automatically_adopted"]
    assert comparator.compare(opts["output"]) == result
    if result["stage"] != "COMPLETED" or kind == "no_change":
        assert not result["selection"]["selected"]


@pytest.mark.parametrize("target", ["freeze", "seal", "history", "candidate"])
def test_resealed_tampering_is_rejected(routing, source, target):
    """重写缓存完整性清单也不能掩盖冻结条件、封存记录、历史或实际候选源码篡改。"""
    opts = options(routing, source)
    campaign.run(**opts, backend_factory=factory())
    root = opts["output"] / "rounds/0001"
    if target == "candidate":
        path = root / "candidate" / policy.TARGET
        path.chmod(0o600)
        path.write_text(path.read_text() + "\nOUTSIDE_POLICY = True\n")
    else:
        path = root / ("frozen-evaluation.json" if target == "freeze" else "round-sealed.json" if target == "seal" else "history.json")
        value = json.loads(path.read_text())
        if target == "freeze": value["profile"]["planner_max_output_tokens"] = 8000
        if target == "seal": value["score_ref"] = "0" * 64
        if target == "history": value["attempts"] = []
        path.write_text(json.dumps(value))
    manifest = file_manifest(opts["output"])
    for name in ("integrity.json", "comparison.json", "summary.md"):
        manifest.pop(name, None)
    (opts["output"] / "integrity.json").write_text(json.dumps(manifest))
    assert comparator.compare(opts["output"])["decision"] == "INVALID_EXPERIMENT"


def test_selection_keeps_unqualified_observations_and_unknown_cost_is_not_zero():
    """只有合格可比方案能入选；未知费用、预检和失败不因看似低开销获得优势。"""
    base = {"id": "A", "profile": {"model": "fake"}, "repetitions": 2, "decision": "IMPROVED", "strategy_evaluable": True,
        "correct": 4, "planned": 4, "stable_correct_cases": 2, "unknown_token_calls": 0, "known_tokens": {"input_tokens": 30}, "median_latency_ms": 10}
    unknown = {**deepcopy(base), "id": "B", "unknown_token_calls": 2, "known_tokens": {}}
    failed = {**deepcopy(base), "id": "C", "decision": "INCONCLUSIVE", "strategy_evaluable": False, "known_tokens": {}}
    result = campaign.select([unknown, failed, base], base["profile"], 2)
    assert result["selected"] == "A" and len(result["ranking"]) == 3
    assert campaign.select([failed], base["profile"], 2)["selection_decision"] == "NO_ELIGIBLE_CANDIDATE"


def test_no_eligible_candidate_cannot_be_locked(routing, source):
    """没有合格候选是正常结果，不能为完成选优而把被拒方案冒充推荐。"""
    opts = options(routing, source)
    report = campaign.run(**opts, backend_factory=factory("no_change"))
    assert report["decision"] == "NO_ELIGIBLE_CANDIDATE"
    target = routing.root / "experiments/harness-v1/must-not-lock"
    with pytest.raises(ValueError, match="CAMPAIGN_NO_ELIGIBLE_CANDIDATE"):
        campaign.lock(campaign_dir=opts["output"], output=target)
    assert not target.exists()


def test_confirmation_rejects_profile_change_and_tampering(routing, source, confirmation_os):
    """锁定后不能悄悄更换模型或靠重封文件改测试对象、分组及成绩。"""
    opts = options(routing, source, authorization=campaign.CampaignAuthorization(max_total_requests=49, max_rounds=1, repetitions=2))
    campaign.run(**opts, backend_factory=factory())
    lock_dir = routing.root / "experiments/harness-v1/lock-profile"
    campaign.lock(campaign_dir=opts["output"], output=lock_dir)
    output = routing.root / "experiments/harness-v1/profile-confirm"
    args = dict(lock_dir=lock_dir, split="DEVELOPMENT", output=output, settings=routing.settings,
        authorization=validation.ValidationAuthorization(max_total_requests=48, repetitions=2), source="fake")
    with pytest.raises(ValueError, match="CAMPAIGN_CONFIRM_PROFILE_CHANGED"):
        confirmation.check(**{**args, "settings": routing.settings.model_copy(update={"llm_chat_model": "changed-model"})})
    assert not output.exists()
    report = confirmation.run(**args, backend_factory=validation_factory())
    assert report["decision"] == "DEVELOPMENT_CONFIRMED"
    path = output / "frozen-evaluation.json"
    frozen = json.loads(path.read_text())
    frozen["schedule"].reverse()
    path.write_text(json.dumps(frozen))
    manifest = file_manifest(output)
    for name in ("integrity.json", "comparison.json"):
        manifest.pop(name, None)
    (output / "integrity.json").write_text(json.dumps(manifest))
    assert confirmation.compare(output)["decision"] == "INVALID_EXPERIMENT"


def test_mutating_frozen_code_stops_before_next_model_call(routing, source, monkeypatch):
    """测试期间发现实际代码变化就终止，不边改边测或继续支付下一试次费用。"""
    original = iteration.run_trial
    def changed(**kw):
        """只修改本测试临时副本以模拟越过冻结窗口；真实候选没有此权限。"""
        result = original(**kw)
        path = kw["code"] / policy.TARGET
        path.chmod(0o600)
        path.write_text(path.read_text() + "\nCHANGED_DURING_MEASUREMENT = True\n")
        return result
    monkeypatch.setattr(iteration, "run_trial", changed)
    opts = options(routing, source)
    report = campaign.run(**opts, backend_factory=factory())
    summary = json.loads((opts["output"] / "run-summary.json").read_text())
    assert summary["stage"] == "FAILED" and summary["requests_used"] == 2
    assert report["decision"] == "INVALID_EXPERIMENT"


def test_cli_check_is_zero_call_and_does_not_create_output(routing, source, monkeypatch, capsys):
    """新命令能实际抵达实验管理检查，默认不请求模型、不创建实验目录。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    original = campaign.check
    monkeypatch.setattr(campaign, "check", lambda **kw: original(**{**kw, "source": "fake"}))
    output = routing.root / "experiments/harness-v1/cli-campaign"
    monkeypatch.setattr(sys, "argv", ["harness", "campaign-run", "--source", str(source), "--output", str(output),
        "--max-rounds", "2", "--max-requests", "98", "--repetitions", "2"])
    loader = config.get_settings
    try:
        config.get_settings = lambda: routing.settings
        evidence_harness.main()
    finally:
        config.get_settings = loader
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "CHECK_PASSED" and result["model_calls"] == 0
    assert result["planned_request_upper_bound"] == 98 and result["case_count"] == 4 and not output.exists()


@pytest.mark.skipif(os.environ.get("HARNESS_OS_ACCEPTANCE") != "1", reason="真实macOS权限需独立验收")
def test_os_campaign_frozen_candidate(routing, source, monkeypatch):
    """真实OS隔离运行重复测量，候选不获得Gold、凭证、项目写入或网络权限。"""
    from app.modules.evidence_harness.improvement import executor, isolation
    monkeypatch.setattr(executor, "isolated_run", isolation.isolated_run)
    monkeypatch.setattr(iteration, "probe_sandbox", isolation.probe_sandbox)
    monkeypatch.setattr(iteration, "run_checks", executor.run_checks)
    opts = options(routing, source, authorization=campaign.CampaignAuthorization(max_total_requests=49, max_rounds=1, repetitions=2))
    result = campaign.run(**opts, backend_factory=factory())
    assert result["decision"] == "DEVELOPMENT_SELECTED", result
    assert result["requests_used"] == 17 and not result["automatically_adopted"]
