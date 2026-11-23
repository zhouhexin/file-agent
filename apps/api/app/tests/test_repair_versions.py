"""逐版自修复实验验收：不选优、不留用、受控契约交付及统一失败预算。"""

from copy import deepcopy
import json
import os

import pytest

from app.modules.evidence_harness import repair_versions as repair, reference_loop
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement.isolation import file_manifest
from app.modules.evidence_harness.executor import ExperimentStopped
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic
from app.tests.test_adaptive_iteration import routing, c_data, fast_os, source, provider, isolated_provider_env
from app.tests.test_adaptive_validation import validation_factory


def prepare(routing, source, monkeypatch, count=2):
    """模型替身仅替外部接口，规划、快照、来源和真实校验器不伪造。"""
    probe = lambda *a, **kw: {k: True for k in ("compile", "read_blocked", "write_blocked", "network_blocked", "exec_blocked")}
    monkeypatch.setattr(repair, "probe_sandbox", probe)
    monkeypatch.setattr(repair, "run_checks", lambda *a: policy.expected_checks())
    monkeypatch.setattr(reference_loop, "probe_sandbox", probe)
    monkeypatch.setattr(reference_loop, "run_checks", lambda *a: policy.expected_checks())
    diagnostic = routing.root.parent / "diagnostics.json"
    diagnostic.write_text(json.dumps({"calls": [{"status": "FAILED", "diagnostic": {"category": "PROVIDER_ERROR"}}],
        "trials": []}), encoding="utf-8")
    opts = dict(source_dir=source, reference_dir=source, diagnostics=diagnostic,
        output=routing.root / "experiments/harness-v1/repair-freeze", settings=routing.settings, source="fake",
        authorization=repair.Authorization(max_versions=count, max_total_requests=count))
    return opts


def advice(n):
    """候选由替身接口生成，宿主机械补上真实契约，模型不能自行改名或改阈值。"""
    client = provider("improve")("ADVISOR", n)

    class Client:
        """只返回受限文本，不改写评分、输入或源码。"""

        def complete_json_with_usage(self, **kwargs):
            """每份候选有不同说明，用于核对各版均执行而不是只测赢家。"""
            raw, usage = client.complete_json_with_usage(**kwargs)
            raw.update(schema_version="repair-version-proposal-v1", patch_kind="BOTH_CONTRACTS")
            raw["instruction"] += " 请核对所有必要输入。" if n == 1 else " 请保持完整来源条件。"
            return raw, usage
    return Client()


def planning(requests, kind="normal"):
    """按版本模拟质量差异，禁止用预设赢家替换真实规划和裁判。"""
    def factory(version, arm, case, n):
        """低分版本和同分版本都必须完整测量。"""
        client = validation_factory()(arm, case, n)

        class Client:
            """真实工具计划仍经过后端校验。"""

            def complete_json_with_usage(self, **kw):
                """保存真实请求身份，确认固定参考和候选分别送给了接口。"""
                requests.append({"version": version, "arm": arm, **deepcopy(kw)})
                if kind == "provider":
                    raise LLMResponseError("fake", diagnostic=make_diagnostic(category="PROVIDER_ERROR", stage="LLM_REQUEST"))
                raw, usage = client.complete_json_with_usage(**kw)
                if version == "0001" and arm == "candidate" and case.case_id == "C1":
                    raw["intent"] = "SEARCH_FILES"
                return raw, usage
        return Client()
    return factory


def checked_freeze(opts):
    """失败时展开测试专用固定异常，不把 INVALID_EXPERIMENT 当作通过。"""
    result = repair.freeze(**opts, backend_factory=advice)
    if result["decision"] == "INVALID_EXPERIMENT": repair._compare(opts["output"])
    assert result["stage"] == "COMPLETED", result
    return result


def test_every_frozen_version_runs_even_when_first_regresses_and_no_one_is_retained(routing, source, monkeypatch):
    """完整串联两版冻结、模拟、同一参考的真实规划对照，并重验篡改及零调用 CLI。"""
    opts = prepare(routing, source, monkeypatch)
    before = file_manifest(source)
    frozen = checked_freeze(opts)
    assert len(frozen["versions"]) == 2 and all(v["status"] == "FROZEN" for v in frozen["versions"])
    assert frozen["requests_used"] == 2 and frozen["selection"] is None
    batch = repair.load_batch(opts["output"])
    for v in batch["versions"]:
        assert '"content_intents"' in v["candidate"]["prompt"]
        assert '"core_topic_phrase_max_length": 30' in v["candidate"]["prompt"]
    requests = []
    test_opts = dict(batch_dir=opts["output"], versions=None, output=routing.root / "experiments/harness-v1/repair-test-all",
        settings=routing.settings, source="fake", authorization=repair.Authorization(max_total_requests=100))
    report = repair.test(**test_opts, backend_factory=planning(requests))
    if report["decision"] == "INVALID_EXPERIMENT": repair._compare(test_opts["output"])
    assert report["stage"] == "COMPLETED" and [v["status"] for v in report["versions"]] == ["COMPLETED"] * 2, report
    assert report["versions"][0]["score"]["decision"] == "REGRESSION"
    assert {r["version"] for r in requests} == {"0001", "0002"}
    assert report["requests_used"] == len(requests) and report["selection"] is None
    assert report["reference_updated"] is False and report["automatically_adopted"] is False
    assert file_manifest(source) == before and repair.compare(test_opts["output"]) == report

    # 重封清单也不能把某个低分版本删掉或改成另一份候选。
    root = opts["output"]
    path = root / "versions/0001/version.json"
    original = path.read_bytes()
    value = json.loads(original)
    value["candidate"]["prompt_ref"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")
    manifest = file_manifest(root)
    for name in ("integrity.json", "comparison.json"): manifest.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert repair.compare(root)["decision"] == "INVALID_EXPERIMENT"


def test_failure_budget_leaves_remaining_versions_not_run(routing, source, monkeypatch):
    """第三次连接故障就停止，保留所有失败调用，其余版本明确 NOT_RUN。"""
    opts = prepare(routing, source, monkeypatch)
    checked_freeze(opts)
    output = routing.root / "experiments/harness-v1/repair-provider-failure"
    requests = []
    result = repair.test(batch_dir=opts["output"], versions=None, output=output, settings=routing.settings,
        source="fake", authorization=repair.Authorization(max_total_requests=100), backend_factory=planning(requests, "provider"))
    if result["decision"] == "INVALID_EXPERIMENT": repair._compare(output)
    assert result["stage"] == "STOPPED" and result["stop_reason"] == "INFRASTRUCTURE_FAILURE_LIMIT", result
    assert result["requests_used"] == len(requests) == 3
    assert result["versions"][1]["status"] == "NOT_RUN" and result["unknown_token_calls"] == 3


def test_advisor_failure_is_recorded_and_next_version_uses_same_budget(routing, source, monkeypatch):
    """复现第二次连接失败：第一版不丢，第三版仍生成，第二版不自动重试。"""
    opts = prepare(routing, source, monkeypatch, count=3)
    requested = []

    def build(n):
        """不同版本各只领一次建议请求，失败不能额外增发请求。"""
        client = advice(n)

        class Client:
            """只在第二份模拟 CLI 的 PROVIDER_ERROR。"""

            def complete_json_with_usage(self, **kw):
                """失败事件仍由真实 Journal 记账。"""
                requested.append(n)
                if n == 2:
                    raise LLMResponseError("fake", diagnostic=make_diagnostic(category="PROVIDER_ERROR", stage="LLM_REQUEST"))
                return client.complete_json_with_usage(**kw)
        return Client()

    result = repair.freeze(**opts, backend_factory=build)
    if result["decision"] == "INVALID_EXPERIMENT": repair._compare(opts["output"])
    assert result["stage"] == "COMPLETED" and result["decision"] == "PARTIAL_VERSIONS_FROZEN", result
    assert requested == [1, 2, 3] and result["requests_used"] == 3 and result["failed_model_calls"] == 1
    assert [v["status"] for v in result["versions"]] == ["FROZEN", "GENERATION_FAILED", "FROZEN"]
    assert result["versions"][1]["failure"]["diagnostic"]["category"] == "PROVIDER_ERROR"
    assert repair.compare(opts["output"]) == result

    # 重封清单也不能把真实失败改成另一种原因。
    root = opts["output"]
    failure_path = root / "versions/0002/generation-failure.json"
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    failure["diagnostic"]["category"] = "TIMEOUT"
    failure_path.write_text(json.dumps(failure), encoding="utf-8")
    manifest = file_manifest(root)
    for name in ("integrity.json", "comparison.json"): manifest.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert repair.compare(root)["decision"] == "INVALID_EXPERIMENT"


def test_generation_stops_at_threshold_or_blocked_account(routing, source, monkeypatch):
    """连接故障连续三次才停，认证或额度受限立即停，全部调用均保留。"""
    for category in ("PROVIDER_ERROR", "AUTHENTICATION_FAILED"):
        opts = prepare(routing, source, monkeypatch, count=5)
        opts["output"] = routing.root / ("experiments/harness-v1/repair-failure-" + category.lower())

        def build(n):
            """类别取固定模拟值，不读取真实账号或网络配置。"""
            class Client:
                """供应商失败不应触发补丁编译或模拟。"""

                def complete_json_with_usage(self, **kw):
                    """所有失败都由真实 Journal 计入统一预算。"""
                    raise LLMResponseError("fake", diagnostic=make_diagnostic(category=category, stage="LLM_REQUEST"))
            return Client()

        result = repair.freeze(**opts, backend_factory=build)
        if result["decision"] == "INVALID_EXPERIMENT": repair._compare(opts["output"])
        expected = 3 if category == "PROVIDER_ERROR" else 1
        assert result["stage"] == "STOPPED" and result["requests_used"] == expected, result
        assert result["stop_reason"] == ("INFRASTRUCTURE_FAILURE_LIMIT" if expected == 3 else "MODEL_ACCESS_BLOCKED")
        assert [v["status"] for v in result["versions"]][:expected] == ["GENERATION_FAILED"] * expected
        assert [v["status"] for v in result["versions"]][expected:] == ["NOT_GENERATED"] * (5-expected)


def test_legacy_failed_batch_keeps_first_frozen_version_readable_and_testable(routing, source, monkeypatch):
    """模拟用户已有 v1：第二次失败终止且没有新失败文件，第一版仍可独立测试。"""
    opts = prepare(routing, source, monkeypatch, count=3)
    # 模拟旧版生成阶段一次失败即终止；只在测试夹具中调整策略，不改用户产物。
    monkeypatch.setattr(repair, "RECOVERABLE_ADVISOR", set())

    def build(n):
        """第一份正常冻结，第二份模拟调用异常。"""
        if n == 1:
            return advice(n)

        class Client:
            """旧版失败记录只在 calls 中持久化。"""

            def complete_json_with_usage(self, **kw):
                """不会生成或执行任意真实补丁。"""
                raise LLMResponseError("fake", diagnostic=make_diagnostic(category="PROVIDER_ERROR", stage="LLM_REQUEST"))
        return Client()

    result = repair.freeze(**opts, backend_factory=build)
    assert result["stage"] == "FAILED", result
    root = opts["output"]
    meta = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    meta.update(schema_version=repair.LEGACY_VERSION, controller_ref=repair.LEGACY_CONTROLLER_REF)
    (root / "experiment.json").write_text(json.dumps(meta), encoding="utf-8")
    (root / "versions/0002/generation-failure.json").unlink()
    manifest = file_manifest(root)
    for name in ("integrity.json", "comparison.json"): manifest.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = file_manifest(root)
    restored = repair.compare(root)
    assert restored["decision"] == "PARTIAL_VERSIONS_FROZEN", restored
    assert restored["versions"][0]["testable"] and restored["versions"][1]["status"] == "GENERATION_FAILED"
    output = routing.root / "experiments/harness-v1/legacy-first-version-test"
    tested = repair.test(batch_dir=root, versions=["0001"], output=output, settings=routing.settings,
        source="fake", authorization=repair.Authorization(max_total_requests=100), backend_factory=planning([]))
    if tested["decision"] == "INVALID_EXPERIMENT": repair._compare(output)
    assert tested["stage"] == "COMPLETED" and tested["versions"][0]["status"] == "COMPLETED", tested
    assert file_manifest(root) == before


def test_simulation_failure_is_listed_and_cannot_be_reported_as_tested(routing, source, monkeypatch):
    """第二版模拟失败也保留版本，测试列表标 BLOCKED，不冒充全部已测。"""
    opts = prepare(routing, source, monkeypatch)
    attempts = []

    def simulate(*args):
        """只控制固定模拟结果；原始版本及真实对照评分保持独立。"""
        attempts.append(True)
        checks = policy.expected_checks()
        if len(attempts) == 2:
            checks["passed"] = False
        return checks

    monkeypatch.setattr(repair, "run_checks", simulate)
    result = repair.freeze(**opts, backend_factory=advice)
    if result["decision"] == "INVALID_EXPERIMENT": repair._compare(opts["output"])
    assert [v["status"] for v in result["versions"]] == ["FROZEN", "SIMULATION_FAILED"]
    output = routing.root / "experiments/harness-v1/repair-partly-blocked"
    report = repair.test(batch_dir=opts["output"], versions=None, output=output, settings=routing.settings,
        source="fake", authorization=repair.Authorization(max_total_requests=100), backend_factory=planning([]))
    if report["decision"] == "INVALID_EXPERIMENT": repair._compare(output)
    assert report["decision"] == "BLOCKED_VERSIONS_PRESENT"
    assert [v["status"] for v in report["versions"]] == ["COMPLETED", "BLOCKED"]


def test_repair_cli_default_check_does_not_create_or_call(routing, source, monkeypatch, capsys):
    """冻结命令也必须显式 live 才调用建议器，测试授权与生成授权分开。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    opts = prepare(routing, source, monkeypatch, count=1)
    original = repair.freeze_check
    monkeypatch.setattr(repair, "freeze_check", lambda **kw: original(**{**kw, "source": "fake"}))
    monkeypatch.setattr(sys, "argv", ["harness", "repair-freeze", "--source", str(source), "--reference", str(source),
        "--diagnostics", str(opts["diagnostics"]), "--output", str(opts["output"]), "--max-versions", "1", "--max-requests", "1"])
    with monkeypatch.context() as local:
        local.setattr(config, "get_settings", lambda: routing.settings)
        evidence_harness.main()
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "CHECK_PASSED" and report["model_calls"] == 0 and not opts["output"].exists()


def test_unknown_recipe_and_rejected_contract_do_not_expand_permissions():
    """改函数、改阈值或改代理都不属于受限提示配方。"""
    with pytest.raises(ValueError, match="REPAIR_PATCH_KIND_INVALID"):
        repair.contract_text("CHANGE_PROXY")
    assert repair._contract_checks() == {"unresolved_show_all_rejected": True, "long_topic_rejected": True,
        "normal_query_accepted": True, "actual_intents_recognized": True}


@pytest.mark.parametrize("selection,expected", [(["--all"], None), (["--version", "0001"], ["0001"])])
def test_real_test_cli_requires_explicit_live_for_each_selection(tmp_path, monkeypatch, capsys, selection, expected):
    """逐版及整批入口默认均只读检查，不会隐式调用真实测试器。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    captured = []

    def check_only(**kw):
        """这里只验证 CLI 授权边界，实际来源及评分由完整串联测试覆盖。"""
        captured.append(kw)
        return {"cases": [], "profile": {}, "planned_request_upper_bound": 120,
            "selected": [{"version": "0001", "status": "FROZEN", "testable": True}]}

    def must_not_run(**kw):
        """缺少 live 时任何测试执行都算错误。"""
        raise AssertionError("REAL_TEST_WITHOUT_LIVE")

    monkeypatch.setattr(repair, "test_check", check_only)
    monkeypatch.setattr(repair, "test", must_not_run)
    monkeypatch.setattr(sys, "argv", ["harness", "repair-test", "--batch", str(tmp_path / "batch"),
        "--output", str(tmp_path / "output"), "--max-requests", "120", *selection])
    with monkeypatch.context() as local:
        local.setattr(config, "get_settings", lambda: None)
        evidence_harness.main()
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "CHECK_PASSED" and result["model_calls"] == 0
    assert captured[0]["versions"] == expected and not (tmp_path / "output").exists()


@pytest.mark.skipif(os.environ.get("HARNESS_OS_ACCEPTANCE") != "1", reason="真实本机模拟专项单独执行")
def test_frozen_repair_real_local_simulation(routing, source, monkeypatch):
    """真实子进程执行冻结补丁的编译和回归，建议模型仍用替身。"""
    from app.modules.evidence_harness.improvement import executor, isolation
    opts = prepare(routing, source, monkeypatch, count=1)
    monkeypatch.setattr(executor, "isolated_run", isolation.isolated_run)
    monkeypatch.setattr(repair, "run_checks", executor.run_checks)
    monkeypatch.setattr(repair, "probe_sandbox", isolation.probe_sandbox)
    report = checked_freeze(opts)
    assert report["versions"][0]["testable"] and report["model_calls"] == 0
