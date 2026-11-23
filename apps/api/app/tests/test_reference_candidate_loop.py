"""参考方案模式验收：旧参考不覆盖、同条件测量、预算与独立裁判均走真实代码。"""

from copy import deepcopy
import json
import os

import pytest

from app.modules.evidence_harness import reference_loop as loop
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement.isolation import file_manifest
from app.tests.test_adaptive_iteration import routing, c_data, fast_os, source, provider, isolated_provider_env
from app.tests.test_adaptive_validation import validation_factory


@pytest.fixture(autouse=True)
def reference_os(monkeypatch, source):
    """快速测试只替系统执行，规划、请求代理、来源重放和评分仍用真实函数。"""
    monkeypatch.setattr(loop, "probe_sandbox", lambda *a, **kw: {
        "compile": True, "read_blocked": True, "write_blocked": True, "network_blocked": True, "exec_blocked": True})
    monkeypatch.setattr(loop, "run_checks", lambda *a: policy.expected_checks())


def options(routing, source, **updates):
    """首次对照和候选共用一份额度，不复用历史分数代替当前测量。"""
    return {"source_dir": source, "reference_dir": source, "output": routing.root / "experiments/harness-v1/reference-loop",
        "settings": routing.settings, "source": "fake", "authorization": loop.Authorization(
            max_cycles=1, max_rounds=2, max_no_progress=2, max_total_requests=120, retest_repetitions=2, top_k=1), **updates}


def backend(requests, kind="same"):
    """模拟供应商按真实提示返回计划，不伪造执行结果；建议器不读取 Gold。"""
    def factory(cycle, role, number, arm=None, case=None, repetition=None):
        """初次对照使用同一供应商，后续仅根据实验意图模拟新规则的不同结果。"""
        client = provider("no_change" if kind == "no_change" else "improve")(role, number, arm, case, repetition)

        class Client:
            """记录实际输入，验证参考全文与当前父版本真正交给模型。"""

            def complete_json_with_usage(self, **kw):
                """失败和中断仍由 Journal 记账，不能从分母删除。"""
                requests.append({"cycle": cycle, "role": role, "number": number, "arm": arm, **deepcopy(kw)})
                if kind == "interrupt" and cycle > 0 and role == "PLANNER":
                    raise KeyboardInterrupt
                raw, usage = client.complete_json_with_usage(**kw)
                if role == "ADVISOR":
                    raw["schema_version"] = "adaptive-exploration-proposal-v1"
                    if raw.get("instruction"):
                        raw["instruction"] += " 请逐步核对计划。" if number == 1 else " 请保护已有正确目标。"
                if kind == "worse" and cycle > 0 and role == "PLANNER" and arm == "candidate" and raw.get("tool_plan"):
                    raw["tool_plan"]["steps"][0]["literal_input"]["show_all_results"] = True
                if kind == "better" and cycle > 0 and role == "PLANNER" and arm == "control" and case.case_id == "D1":
                    # 有效但目标错误的规划才能检验真实提升；回退路径必须保持不合格。
                    raw["intent"] = "SEARCH_FILES"
                return raw, usage

        return Client()
    return factory


def run_checked(opts, requests, kind="same"):
    """独立比较失败时展开固定测试异常，避免测试只看到模糊的失败回执。"""
    report = loop.run(**opts, backend_factory=backend(requests, kind))
    if report["decision"] == "INVALID_EXPERIMENT":
        loop._compare(opts["output"])
    return report


def test_reference_is_remeasured_first_and_same_or_worse_never_replaces(routing, source):
    """旧方案先在新环境测量；同分、违规候选不能覆盖旧参考，说明完整进入建议输入。"""
    opts, requests = options(routing, source), []
    before = file_manifest(source)
    report = run_checked(opts, requests, "worse")
    assert report["stage"] == "COMPLETED" and report["decision"] == "NO_ELIGIBLE_CANDIDATE", report
    assert not report["promotions"] and report["retained"] == report["reference"]
    first_advisor = next(i for i, r in enumerate(requests) if r["role"] == "ADVISOR")
    assert first_advisor > 0 and all(r["cycle"] == 0 for r in requests[:first_advisor])
    for r in requests:
        if r["role"] == "ADVISOR":
            ctx = r["user_payload"]["source_context"]
            assert ctx["reference_instruction"] == report["reference"]["instruction"] == ctx["retained_instruction"]
    assert report["requests_used"] == len(requests) and report["model_calls"] == 0
    assert report["automatically_adopted"] is False and file_manifest(source) == before
    assert loop.compare(opts["output"]) == report


def test_strict_repeated_gain_updates_parent_but_preserves_original_reference(routing, source):
    """更新的是研究起点；第二轮看到新父说明，旧参考全文及文件保持不变。"""
    opts, requests = options(routing, source), []
    report = run_checked(opts, requests, "better")
    assert report["decision"] == "DEVELOPMENT_SELECTED" and report["promotions"], report
    assert report["retained"]["prompt_ref"] != report["reference"]["prompt_ref"]
    advisors = [r for r in requests if r["role"] == "ADVISOR"]
    assert advisors[0]["user_payload"]["source_context"]["retained_instruction"] == report["reference"]["instruction"]
    assert advisors[1]["user_payload"]["source_context"]["retained_instruction"] == report["retained"]["instruction"]
    assert advisors[1]["user_payload"]["source_context"]["reference_instruction"] == report["reference"]["instruction"]
    assert len(report["retained"]["instruction"]) <= 3000 and not report["holdout_evaluated"]


@pytest.mark.parametrize("kind", ["budget", "interrupt", "no_change", "initial_budget"])
def test_stop_keeps_reference_and_all_started_calls(routing, source, kind):
    """预算不足时停止在对应阶段，不跳过首次对照、清空失败调用或自动续费。"""
    opts, requests = options(routing, source), []
    if kind in {"budget", "initial_budget"}:
        opts["authorization"] = opts["authorization"].model_copy(update={"max_total_requests": 18 if kind == "budget" else 1})
    report = run_checked(opts, requests, "interrupt" if kind == "interrupt" else "no_change" if kind == "no_change" else "same")
    assert report["requests_used"] == len(requests) and report["retained"] == report["reference"], report
    assert not report["automatically_adopted"]
    if kind == "no_change":
        assert report["stop_reason"] == "MODEL_SELECTED_NO_CHANGE"
    elif kind == "interrupt":
        assert report["stage"] == "INTERRUPTED" and report["unknown_token_calls"] >= 1
    else:
        assert report["stage"] == "STOPPED" and report["stop_reason"] == "REQUEST_BUDGET_EXHAUSTED"
    if kind == "initial_budget":
        assert all(r["role"] == "PLANNER" and r["cycle"] == 0 for r in requests)


@pytest.mark.parametrize("target", ["parent", "score", "retained", "freeze", "request", "replacement"])
def test_resealed_tampering_is_rejected(routing, source, target):
    """重封清单不能掩盖父版本、缓存分数、冻结条件、请求或替换决定的篡改。"""
    opts, requests = options(routing, source), []
    run_checked(opts, requests, "better")
    root = opts["output"]
    locations = {"parent": "attempts/0001/context.json", "score": "reference-evaluation/score.json",
        "retained": "retained-policy.json", "freeze": "reference-evaluation/frozen-evaluation.json",
        "request": "calls/0001.started.json", "replacement": "attempts/0001/replacement.json"}
    path = root / locations[target]
    data = json.loads(path.read_text(encoding="utf-8"))
    if target == "parent": data["parent"]["prompt_ref"] = "0" * 64
    elif target == "score": data["arms"]["candidate"]["correct"] = 999
    elif target == "retained": data["instruction"] = "不可信缓存。"
    elif target == "freeze": data["repetitions"] += 1
    elif target == "request": data["profile_ref"] = "0" * 64
    else: data["replace"] = False
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    actual = file_manifest(root)
    for name in ("integrity.json", "comparison.json"):
        actual.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(actual), encoding="utf-8")
    assert loop.compare(root)["decision"] == "INVALID_EXPERIMENT"


def test_strategy_schedule_and_replacement_require_each_repeat_gain():
    """混合方式事先固定；总分涨但某次重复不涨，仍不能替换。"""
    auth = loop.Authorization(max_total_requests=100)
    assert [loop.generation_mode(auth, n) for n in range(1, 6)] == ["inherit", "inherit", "inherit", "fresh", "inherit"]
    score = {"decision": "IMPROVED", "rows": [
        {"arm": arm, "repetition": n, "correct": arm == "candidate" and n == 1}
        for n in (1, 2) for arm in ("control", "candidate")]}
    assert not loop.replacement(score, 2)["replace"]


def test_cli_reference_check_is_zero_call(routing, source, monkeypatch, capsys):
    """新模式仍需显式 live，默认检查不会创建目录或请求模型。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    original = loop.check
    monkeypatch.setattr(loop, "check", lambda **kw: original(**{**kw, "source": "fake"}))
    output = options(routing, source)["output"]
    monkeypatch.setattr(sys, "argv", ["harness", "campaign-loop", "--source", str(source), "--reference", str(source),
        "--output", str(output), "--max-requests", "100"])
    # 在自动清理夹具调用 cache_clear 之前恢复真实的缓存函数。
    with monkeypatch.context() as local_patch:
        local_patch.setattr(config, "get_settings", lambda: routing.settings)
        evidence_harness.main()
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "CHECK_PASSED" and result["model_calls"] == 0 and not output.exists()


@pytest.mark.skipif(os.environ.get("HARNESS_OS_ACCEPTANCE") != "1", reason="真实本机执行器专项需显式开启")
def test_reference_pair_uses_real_local_processes(routing, source, monkeypatch):
    """两组含受限说明时也必须通过真实子进程门禁，模型仍用替身。"""
    from app.modules.evidence_harness.improvement import executor, isolation
    monkeypatch.setattr(executor, "isolated_run", isolation.isolated_run)
    monkeypatch.setattr(loop, "run_checks", executor.run_checks)
    monkeypatch.setattr(loop, "probe_sandbox", isolation.probe_sandbox)
    opts = options(routing, source)
    requests = []
    report = run_checked(opts, requests, "no_change")
    assert report["stage"] == "COMPLETED" and report["stop_reason"] == "MODEL_SELECTED_NO_CHANGE", report
    assert not report["automatically_adopted"]
