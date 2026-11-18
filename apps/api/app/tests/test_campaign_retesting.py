"""探索与开发复测验收：宽研究资格、严推荐门槛、更多轮次与冻结同一候选。"""

from copy import deepcopy
import json
import os

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.improvement import campaign, campaign_retesting as retesting
from app.modules.evidence_harness.improvement import campaign_confirmation as confirmation, iteration_comparator as comparator
from app.modules.evidence_harness.improvement import iteration_proposals as proposals
from app.modules.evidence_harness.improvement import advisor_memory
from app.modules.evidence_harness.improvement import adaptive_iteration as iteration, adaptive_validation as validation
from app.modules.evidence_harness.improvement.isolation import file_manifest
from app.tests.test_harness_campaign import routing, source, c_data, fast_os, isolated_provider_env, confirmation_os, factory, options
from app.tests.test_adaptive_validation import validation_factory


def quality_factory(unsafe=False):
    """初测模拟质量缺陷而不是更改裁判；危险工具必须在研究资格上被排除。"""
    def build(role, number, arm=None, case=None, n=None):
        """模型输出走实际规划转换与schema校验。"""
        client = factory()(role, number, arm, case, n)
        class Backend:
            """可控采样模拟同一候选在初测时的失败，复测仍原样使用该候选。"""

            def complete_json_with_usage(self, **kw):
                """只有开发初测的一题出错；没有把Gold交给模型。"""
                raw, usage = client.complete_json_with_usage(**kw)
                if role == "PLANNER" and arm == "candidate" and case.case_id == "C1" and raw.get("tool_plan"):
                    if unsafe == "scope":
                        raw["scope"] = {"source": "attachments", "document_ids": ["UNAUTHORIZED-DOCUMENT"]}
                        raw["tool_plan"]["steps"][0]["literal_input"]["document_ids"] = ["UNAUTHORIZED-DOCUMENT"]
                    elif unsafe:
                        raw["tool_plan"]["steps"][0]["tool_name"] = "not-a-whitelisted-tool"
                    else:
                        raw["tool_plan"]["steps"][0]["literal_input"]["show_all_results"] = True
                return raw, usage
        return Backend()
    return build


def search(routing, source, *, name="explore", unsafe=False):
    """一轮初测可有改善迹象但未达到原严格门槛，不能直接锁定为最终方案。"""
    opts = options(routing, source, name=name, authorization=campaign.CampaignAuthorization(max_rounds=2, repetitions=1, max_total_requests=100))
    report = campaign.run(**opts, backend_factory=quality_factory(unsafe))
    assert report["decision"] == "NO_ELIGIBLE_CANDIDATE", report
    return opts, report


def test_round_ten_short_references_preserve_legacy_protocol():
    """第10轮只能用新协议，旧三轮协议不能被升级后偷偷放宽。"""
    packet = {"items": [{"feedback_id": digest("actual-feedback")}]}
    mapping = proposals.reference_map(packet, 10, "source", extended=True)
    assert mapping["entries"] == {"R10F001": digest("actual-feedback")}
    _, _, schema = proposals.advisor_view(packet, {"baseline_sha256": digest("source")}, 10, extended=True)
    assert schema["properties"]["feedback_refs"]["items"]["enum"] == ["R10F001"]
    with pytest.raises(ValueError, match="ITERATION_REFERENCE_ROUND_INVALID"):
        proposals.reference_map(packet, 10, "source")


def test_ten_distinct_candidates_and_initial_scores_are_not_recommendations(routing, source):
    """新控制器完整记录十份候选；单次初测高分仍不能冒充已经验证的推荐。"""
    opts = options(routing, source, authorization=campaign.CampaignAuthorization(max_rounds=10, max_no_progress=10,
        repetitions=1, max_total_requests=100))
    report = campaign.run(**opts, backend_factory=factory())
    if report["decision"] == "INVALID_EXPERIMENT": comparator._compare(opts["output"])
    assert len(report["rounds"]) == 10 and report["requests_used"] == 90, report
    assert report["decision"] == "NO_ELIGIBLE_CANDIDATE" and len(report["research_candidates"]["shortlist"]) == 10
    for root in sorted((opts["output"] / "rounds").iterdir()):
        view = json.loads((root / "advisor-memory.json").read_text())
        assert view["request_characters"] <= advisor_memory.POLICY["request_characters"]
        full = json.loads((root / "history.json").read_text())["attempts"]
        assert view["history_projection"]["source_ref"] == digest(full)
        assert len(view["history"]) == len(full)
    assert comparator.compare(opts["output"]) == report


@pytest.mark.parametrize("unsafe", [True, "scope"])
def test_quality_failure_can_be_retested_but_unsafe_plan_cannot(routing, source, unsafe):
    """质量退步可继续研究，未知工具和未授权文件不能藏在拒绝后的回退成绩中。"""
    _, report = search(routing, source)
    assert report["research_candidates"]["shortlist"] == ["C001", "C002"]
    assert not report["selection"]["selected"]
    _, rejected = search(routing, source, name="unsafe", unsafe=unsafe)
    assert not rejected["research_candidates"]["shortlist"]
    assert any("UNSAFE_ADAPTIVE_PLAN" in r["retest_exclusion_reasons"] for r in rejected["research_candidates"]["ranking"])


@pytest.mark.parametrize("repetitions", [5, 10])
def test_freeze_retest_five_times_recommend_only_after_strict_validation(routing, source, confirmation_os, repetitions):
    """研究锁固定两份原候选各测五或十次，零建议调用，通过严格复测后才允许生成最终锁。"""
    opts, initial = search(routing, source)
    before = file_manifest(opts["output"])
    output = routing.root / "experiments/harness-v1/retested"
    args = dict(campaign_dir=opts["output"], output=output, settings=routing.settings,
        authorization=retesting.RetestAuthorization(top_k=2, repetitions=repetitions, max_total_requests=48*repetitions), source="fake")
    checked = retesting.check(**args)
    assert len(checked["chosen"]) == 2 and checked["planned_request_upper_bound"] == 48*repetitions and not output.exists()
    requests = []
    report = retesting.run(**args, backend_factory=lambda number, arm, case, n: validation_factory(requests=requests)(arm, case, n))
    if report["decision"] == "INVALID_EXPERIMENT": retesting._compare(output)
    assert report["decision"] == "DEVELOPMENT_SELECTED" and report["requests_used"] == 16*repetitions, report
    assert report["advisor_calls"] == 0 and len(requests) == 16*repetitions
    assert all(r["score"]["arms"]["candidate"]["planned"] == 4*repetitions for r in report["rounds"])
    assert {r["policy_ref"] for r in report["rounds"]} <= {r["policy_ref"] for r in initial["research_candidates"]["ranking"]}
    assert file_manifest(opts["output"]) == before and retesting.compare(output) == report
    research_lock = output / "locks/0001"
    with pytest.raises(ValueError, match="RESEARCH_LOCK_HOLDOUT_FORBIDDEN"):
        confirmation.check(lock_dir=research_lock, split="HOLDOUT", output=routing.root / "experiments/harness-v1/forbidden",
            settings=routing.settings, authorization=confirmation.ConfirmationAuthorization(max_total_requests=60), source="fake")
    final = routing.root / "experiments/harness-v1/final-after-retest"
    assert campaign.lock(campaign_dir=output, output=final)["stage"] == "LOCKED"
    assert not campaign.load_lock(final).get("research_only", False)
    next_args = options(routing, output, name="next-search", authorization=campaign.CampaignAuthorization(max_total_requests=100, max_rounds=1))
    inherited = campaign.check(**next_args)
    assert len(inherited["history_sources"]) == 2 and len(inherited["historical_candidates"]) == 4


def test_retest_budget_is_shared_across_candidates(routing, source, confirmation_os):
    """第一份用满剩余预算后，不能重置额度去测第二份；已发出的请求均保留。"""
    opts, _ = search(routing, source)
    output = routing.root / "experiments/harness-v1/retest-budget"
    report = retesting.run(campaign_dir=opts["output"], output=output, settings=routing.settings,
        authorization=retesting.RetestAuthorization(max_total_requests=41, repetitions=5), source="fake",
        backend_factory=lambda number, arm, case, n: validation_factory()(arm, case, n))
    if report["decision"] == "INVALID_EXPERIMENT": retesting._compare(output)
    assert report["requests_used"] == 41 and report["stage"] == "STOPPED" and report["selection"]["selected"] is None, report


def test_retest_interrupt_keeps_calls_and_does_not_select(routing, source, confirmation_os):
    """固定候选复测中断后保留已开始调用，不选择赢家或继续第二份候选。"""
    opts, _ = search(routing, source)
    output = routing.root / "experiments/harness-v1/retest-interrupt"
    report = retesting.run(campaign_dir=opts["output"], output=output, settings=routing.settings,
        authorization=retesting.RetestAuthorization(max_total_requests=240, repetitions=5), source="fake",
        backend_factory=lambda number, arm, case, n: validation_factory("interrupt")(arm, case, n))
    if report["decision"] == "INVALID_EXPERIMENT": retesting._compare(output)
    assert report["stage"] == "INTERRUPTED" and report["selection"]["selected"] is None, report
    assert report["requests_used"] == len(list((output / "rounds/0001/calls").glob("*.started.json"))) > 0
    assert not (output / "rounds/0002").exists() and retesting.compare(output) == report


def test_research_lock_and_batch_tampering_are_rejected(routing, source, confirmation_os):
    """研究用途或候选清单被改写，即使重封完整性清单也不能进入保留或升级推荐。"""
    opts, _ = search(routing, source)
    output = routing.root / "experiments/harness-v1/retest-tamper"
    retesting.run(campaign_dir=opts["output"], output=output, settings=routing.settings,
        authorization=retesting.RetestAuthorization(max_total_requests=240, top_k=1), source="fake",
        backend_factory=lambda number, arm, case, n: validation_factory()(arm, case, n))
    p=output/"locks/0001"
    raw=json.loads((p/"lock.json").read_text());raw["purpose"]="FINAL_CONFIRMATION"
    (p/"lock.json").write_text(json.dumps(raw))
    manifest=file_manifest(p);manifest.pop("integrity.json",None)
    (p/"integrity.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match="RETEST_LOCK_BINDING_INVALID"):
        campaign.load_lock(p)


def test_cli_exploration_defaults_and_retest_dispatch_are_zero_call(routing, source, monkeypatch, capsys):
    """默认六份各一次初测、前两份各五次复测，两个入口检查均不启动模型。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    def cli(argv):
        """进入命令时临时提供Fake配置，返回或异常均恢复带cache_clear的正式loader。"""
        monkeypatch.setattr(sys, "argv", argv)
        original_loader = config.get_settings
        try:
            config.get_settings = lambda: routing.settings
            evidence_harness.main()
        finally:
            config.get_settings = original_loader
    original = campaign.check
    monkeypatch.setattr(campaign, "check", lambda **kw: original(**{**kw, "source": "fake"}))
    output = routing.root / "experiments/harness-v1/cli-explore"
    cli(["harness", "campaign-run", "--source", str(source), "--output", str(output), "--max-requests", "150"])
    raw = json.loads(capsys.readouterr().out)
    assert raw["authorization"]["max_rounds"] == 6 and raw["authorization"]["repetitions"] == 1
    assert raw["model_calls"] == 0 and not output.exists()
    opts, _ = search(routing, source)
    original_retest = retesting.check
    monkeypatch.setattr(retesting, "check", lambda **kw: original_retest(**{**kw, "source": "fake"}))
    cli(["harness", "campaign-retest", "--campaign", str(opts["output"]), "--output", str(output), "--max-requests", "240"])
    raw = json.loads(capsys.readouterr().out)
    assert raw["authorization"]["top_k"] == 2 and raw["authorization"]["repetitions"] == 5
    assert raw["retest_candidates"] == ["C001", "C002"] and raw["model_calls"] == 0 and not output.exists()


@pytest.mark.skipif(os.environ.get("HARNESS_OS_ACCEPTANCE")!="1",reason="真实macOS权限需单独验收")
def test_os_research_retest_keeps_candidate_frozen(routing, source, confirmation_os, monkeypatch):
    """研究复测仍走真实OS隔离，允许质量观察不等于授予文件、网络或工具执行权限。"""
    from app.modules.evidence_harness.improvement import executor,isolation
    opts,_=search(routing,source)
    monkeypatch.setattr(executor,"isolated_run",isolation.isolated_run)
    monkeypatch.setattr(confirmation,"probe_sandbox",isolation.probe_sandbox)
    monkeypatch.setattr(confirmation,"run_checks",executor.run_checks)
    result=retesting.run(campaign_dir=opts["output"],output=routing.root/"experiments/harness-v1/os-retest",settings=routing.settings,
        authorization=retesting.RetestAuthorization(top_k=1,repetitions=2,max_total_requests=48),source="fake",
        backend_factory=lambda number,arm,case,n:validation_factory()(arm,case,n))
    assert result["decision"]=="DEVELOPMENT_SELECTED",result
