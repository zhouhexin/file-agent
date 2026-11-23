"""探索—复测—反馈自动闭环验收：Fake只替外部模型，预算、冻结及独立评分走真实代码。"""

import json
import os
from copy import deepcopy

import pytest

from app.modules.evidence_harness import campaign_loop as loop
from app.modules.evidence_harness.improvement import campaign
from app.modules.evidence_harness.improvement.isolation import file_manifest
from app.tests.test_harness_campaign import routing, source, c_data, fast_os, isolated_provider_env, confirmation_os, factory
from app.tests.test_campaign_retesting import quality_factory


@pytest.fixture(autouse=True)
def fast_confirmation(confirmation_os):
    """快速测试沿用可信OS替身，真实隔离由单独专项验证。"""


def backend(requests, *, quality=True, interrupt=False, no_change=False, unsafe=False):
    """让建议Fake每循环返回不同通用候选；不给模型Gold、不替换真实裁判。"""
    def build(cycle, role, number, arm=None, case=None, repetition=None):
        """模型收到的是实际组装后的反馈及同一冻结候选请求。"""
        base = (quality_factory(unsafe) if quality else factory("no_change" if no_change else "improve"))(
            role, (cycle - 1) * 10 + number, arm, case, repetition)
        class Client:
            """只用于观察反馈回流和受控模拟中断。"""

            def complete_json_with_usage(self, **kw):
                """复测中断不应自动启动第二个循环或丢弃已经开始的请求。"""
                requests.append({"cycle": cycle, "role": role, "arm": arm, "repetition": repetition, **deepcopy(kw)})
                if interrupt and role == "PLANNER" and arm == "candidate" and repetition == 2:
                    raise KeyboardInterrupt
                return base.complete_json_with_usage(**kw)
        return Client()
    return build


def options(routing, source, **changes):
    """新任务有独立目录和单份明确总预算。"""
    return dict(source_dir=source, output=routing.root / "experiments/harness-v1/loop", settings=routing.settings,
        source="fake", authorization=loop.Authorization(max_total_requests=100, max_rounds=1, top_k=1,
            repetitions=1, retest_repetitions=2), **changes)


def test_two_real_cycles_consume_retest_feedback_and_never_reset_budget(routing, source):
    """真实两次闭环：第二次建议看见16条复测反馈，旧策略排除且所有调用只计一次。"""
    opts = options(routing, source)
    before = file_manifest(source)
    requests = []
    report = loop.run(**opts, backend_factory=backend(requests))
    if report["decision"] == "INVALID_EXPERIMENT": loop._compare(opts["output"])
    assert report["stage"] == "COMPLETED" and report["recommendation"] is None, report
    assert [p["phase"] for p in report["phases"]] == ["EXPLORATION", "RETEST"] * 2
    assert report["requests_used"] == len(requests) == 50 and report["advisor_calls"] == 2
    advisors = [r for r in requests if r["role"] == "ADVISOR"]
    assert len(advisors[1]["user_payload"]["feedback"]["items"]) == 16
    assert advisors[1]["user_payload"]["history"][-1]["observations"]["item_count"] == 16
    first = json.loads((opts["output"] / "cycles/0001/retest/comparison.json").read_text())["rounds"][0]["policy_ref"]
    second = json.loads((opts["output"] / "cycles/0002/retest/experiment.json").read_text())
    assert second["authorization"]["excluded_policy_refs"] == [first]
    assert second["candidates"][0]["policy_ref"] != first
    assert second["authorization"]["max_total_requests"] == 66
    assert file_manifest(source) == before and loop.compare(opts["output"]) == report


@pytest.mark.parametrize("kind", ["budget", "interrupt", "no_change", "qualified", "unsafe"])
def test_stop_rules_preserve_records_and_never_adopt(routing, source, kind):
    """全局额度、中断、不修改、严格成功及没有新研究对象都有确定的停止事实。"""
    opts = options(routing, source)
    if kind == "budget":
        opts["authorization"] = opts["authorization"].model_copy(update={"max_total_requests": 10})
    requests = []
    report = loop.run(**opts, backend_factory=backend(requests, quality=kind not in {"no_change", "qualified"},
        no_change=kind == "no_change", interrupt=kind == "interrupt", unsafe=kind == "unsafe"))
    if report["decision"] == "INVALID_EXPERIMENT": loop._compare(opts["output"])
    assert report["requests_used"] == len(requests) and not report["automatically_adopted"], report
    if kind == "budget":
        assert report["stage"] == "STOPPED" and report["stop_reason"] == "REQUEST_BUDGET_EXHAUSTED" and len(requests) == 10
    elif kind == "interrupt":
        assert report["stage"] == "INTERRUPTED" and report["unknown_token_calls"] >= 1 and report["recommendation"] is None
    elif kind == "no_change":
        assert report["stop_reason"] == "MODEL_SELECTED_NO_CHANGE" and len(requests) == 1
    elif kind == "qualified":
        assert report["decision"] == "DEVELOPMENT_SELECTED" and report["stop_reason"] == "QUALIFIED_CANDIDATE_FOUND"
        assert report["recommendation"]["experiment"].endswith("cycles/0001/retest")
    else:
        assert sum(p["stage"] == "SKIPPED" for p in report["phases"]) == 2 and report["recommendation"] is None


def test_resealed_phase_authorization_is_not_trusted(routing, source):
    """根清单即使重封，阶段也不能领到超出全局剩余额度的授权。"""
    opts = options(routing, source)
    loop.run(**opts, backend_factory=backend([], quality=False, no_change=True))
    root = opts["output"]
    path = root / "events/0001.json"
    event = json.loads(path.read_text())
    event["authorization"]["max_total_requests"] += 1
    path.write_text(json.dumps(event))
    manifest = file_manifest(root)
    for name in ("integrity.json", "comparison.json"):
        manifest.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="LOOP_PHASE_AUTHORIZATION_CHANGED"):
        loop._compare(root)
    assert loop.compare(root)["decision"] == "INVALID_EXPERIMENT"


def test_cli_check_is_zero_call_and_does_not_create_directory(routing, source, monkeypatch, capsys):
    """新入口默认只核对，不能因接成循环而隐式调用模型。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    original = loop.check
    monkeypatch.setattr(loop, "check", lambda **kw: original(**{**kw, "source": "fake"}))
    opts = options(routing, source)
    monkeypatch.setattr(sys, "argv", ["harness", "campaign-loop", "--source", str(source), "--output", str(opts["output"]), "--max-requests", "100"])
    loader = config.get_settings
    try:
        config.get_settings = lambda: routing.settings
        evidence_harness.main()
    finally:
        config.get_settings = loader
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "CHECK_PASSED" and report["model_calls"] == 0 and not opts["output"].exists()


def test_total_time_reserve_stops_before_first_model_call(routing, source):
    """总时限不够预留一次调用时，不能因进入新阶段重新领一份时间。"""
    opts = options(routing, source)
    opts["authorization"] = opts["authorization"].model_copy(update={"max_active_seconds": 1})
    requests = []
    report = loop.run(**opts, backend_factory=backend(requests))
    if report["decision"] == "INVALID_EXPERIMENT": loop._compare(opts["output"])
    assert report["stage"] == "STOPPED" and report["stop_reason"] == "ACTIVE_TIME_BUDGET_EXHAUSTED"
    assert report["requests_used"] == 0 and not requests


@pytest.mark.skipif(os.environ.get("HARNESS_OS_ACCEPTANCE") != "1", reason="真实系统隔离专项单独执行；Windows需要Docker")
def test_os_auto_loop_keeps_candidates_isolated(routing, source, monkeypatch):
    """自动跨阶段仍实际使用OS隔离，候选不能访问项目、Gold、网络或执行其他程序。"""
    from app.modules.evidence_harness.improvement import isolation, executor, adaptive_iteration, campaign_confirmation
    monkeypatch.setattr(executor, "isolated_run", isolation.isolated_run)
    for module in (adaptive_iteration, campaign_confirmation):
        monkeypatch.setattr(module, "probe_sandbox", isolation.probe_sandbox)
        monkeypatch.setattr(module, "run_checks", executor.run_checks)
    opts = options(routing, source)
    opts["authorization"] = opts["authorization"].model_copy(update={"max_cycles": 1})
    report = loop.run(**opts, backend_factory=backend([], quality=False))
    if report["decision"] == "INVALID_EXPERIMENT": loop._compare(opts["output"])
    assert report["decision"] == "DEVELOPMENT_SELECTED" and not report["automatically_adopted"], report
