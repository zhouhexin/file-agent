"""自动诊断到建议器的闭环验收：使用真实规划节点与Fake模型，不发起真实服务请求。"""

from copy import deepcopy
import json
import os

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.improvement import adaptive_iteration as iteration
from app.modules.evidence_harness.improvement import iteration_comparator as comparator
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement import adaptive_validation as validation
from app.modules.evidence_harness.improvement import planning_diagnosis as diagnosis
from app.modules.evidence_harness.improvement.isolation import file_manifest
from app.tests.test_routing_harness import routing
from app.tests.test_adaptive_policy import fast_os, INSTRUCTION
from app.tests.test_adaptive_validation import c_data, validation_factory, options as c_options, isolated_provider_env


def provider(kind="iterate", requests=None):
    """建议器自主返回不同通用文本；规划Fake按真实提示内容变化，不直接改评分或Gold。"""
    requests = requests if requests is not None else []

    def build(role, number, arm=None, case=None, n=None):
        """所有角色使用真实Journal，总预算和请求绑定不能由Fake跳过。"""
        class Client:
            """可控响应仅替代外部模型，业务转换和诊断使用真实代码。"""

            def complete_json_with_usage(self, **request):
                """第一轮参数出错，第二轮读取自动诊断后修正，便于证明链路真实连通。"""
                requests.append({"role": role, "round_id": number, "arm": arm, "case_id": case.case_id if case else None, **deepcopy(request)})
                if kind == "interrupt" and role == "PLANNER":
                    raise KeyboardInterrupt
                if kind == "advisor_failure" and role == "ADVISOR":
                    raise RuntimeError("PRIVATE_EXCEPTION_SENTINEL")
                if role == "ADVISOR":
                    payload = request["user_payload"]
                    raw = {"schema_version": "adaptive-iteration-proposal-v2", "baseline_ref": "BASELINE",
                        "decision": "PROPOSE_POLICY", "feedback_refs": [payload["feedback"]["items"][0]["feedback_id"]],
                        "diagnosis": "BUSINESS_FAILURE", "evidence_summary": "独立开发反馈仍有任务错误，应保护正常清单任务。",
                        "hypothesis": "通用说明可能改善任务目标与工具参数一致性。", "instruction": INSTRUCTION,
                        "no_change_reason": None, "reason": "仅提出最小候选，效果由独立复测判断。", "no_change_attempt_ref": None}
                    usage = {"input_tokens": 10, "output_tokens": 5}
                    if kind == "no_change":
                        raw.update(decision="NO_CHANGE", diagnosis="INSUFFICIENT_EVIDENCE", hypothesis=None,
                            instruction=None, no_change_reason="INSUFFICIENT_EVIDENCE")
                    if kind == "bad_proposal":
                        raw["feedback_refs"] = [f"R{number}F999"]
                    raw["instruction"] = None if kind == "no_change" else INSTRUCTION + (" 请校验工具参数。" if number == 1 or kind == "repeat" else " 请按真实输入约束检查每个工具步骤。")
                    return raw, usage
                raw, usage = validation_factory()(arm, case, n).complete_json_with_usage(**request)
                if arm == "candidate" and (number == 1 and kind != "improve" or kind == "no_progress") and raw.get("tool_plan"):
                    raw["tool_plan"]["steps"][0]["literal_input"]["show_all_results"] = True
                return raw, usage
        return Client()
    return build


@pytest.fixture
def source(routing, c_data, monkeypatch):
    """来源由实际C流程生成并独立核验，避免手写分数或断开的反馈冒充真实来源。"""
    opts = c_options(routing, c_data, name="iteration-source")
    report = validation.run(**opts, backend_factory=validation_factory())
    assert report["decision"] == "VALIDATED", report
    monkeypatch.setattr(iteration, "probe_sandbox", lambda *a, **kw: {
        "compile": True, "read_blocked": True, "write_blocked": True, "network_blocked": True, "exec_blocked": True})
    monkeypatch.setattr(iteration, "run_checks", lambda *a: policy.expected_checks())
    return opts["output"]


def options(routing, source, **updates):
    """默认两轮共用显式额度，不给第二轮重新授权。"""
    return {"source_dir": source, "output": routing.root / "experiments/harness-v1/autonomous",
        "settings": routing.settings, "authorization": iteration.IterationAuthorization(max_total_requests=100), "source": "fake", **updates}


@pytest.mark.parametrize("bad_input,code,path", [
    ({"query": "PRIVATE_QUERY_SENTINEL", "show_all_results": True}, "SHOW_ALL_REQUIRES_RESOLVED_CLARIFICATION", []),
    ({"query": "测试", "clarification_id": "PRIVATE_ID_SENTINEL"}, "CLARIFICATION_PAIR_REQUIRED", []),
    ({"query": "测试", "semantic_plan": {"core_topics": [{"phrase": "私" * 40}]}},
        "TOOL_PARAMETER_CONSTRAINT", ["semantic_plan", "core_topics", 0, "phrase"]),
    ({"query": "私" * 510}, "TOOL_PARAMETER_CONSTRAINT", ["query"]),
])
def test_automatic_real_schema_diagnostics_are_safe(bad_input, code, path):
    """诊断从真实输入模型自动得出字段和限制，绝不把用户输入或错误原文当成反馈。"""
    result = {"plan": {"tool_plan": {"steps": [{"tool_name": "hybrid-search", "input": bad_input}]}}}
    rows = diagnosis.parameter_diagnostics(result)
    assert rows[0]["code"] == code and rows[0]["field_path"] == path
    assert "PRIVATE_" not in json.dumps(rows) and "私" * 20 not in json.dumps(rows, ensure_ascii=False)
    if path and path[-1] == "phrase":
        assert rows[0]["limits"]["max_length"] == 30


def test_preflight_stays_in_denominator_and_outside_prompt_scope(routing):
    """固定预检题不能被排除或算作提示改善，自动反馈应如实标明优化说明覆盖不到它。"""
    from app.modules.evidence_harness.routing.dataset import load_dataset
    from app.modules.evidence_harness.routing.runtime import run_planning
    case = load_dataset(routing.dataset).inputs.cases[0].model_copy(update={
        "question": "总结一下培训通知.pdf", "adaptive_mode": "enabled"})
    catalog = load_dataset(routing.dataset).inputs.catalog_snapshot
    class Unused:
        """预检不应请求模型；若改变执行路径应使验收失败。"""

        def complete_json(self, **kw):
            """没有真实Provider，禁止把固定预检伪装成模型推理。"""
            raise AssertionError("PREFLIGHT_MUST_NOT_REQUEST_MODEL")
    result = run_planning(case, catalog=catalog, client=Unused(), settings=routing.settings, capture_audit=True, quiet=True)
    result["strategy_evaluable"] = False
    assert result["planning_audit"]["effective_mode"] == "PREFLIGHT"
    records = [{"arm": arm, "case_id": case.case_id, "repetition": 1, "case_input_ref": digest(case.model_dump(mode="json")),
        "duration_ms": 0, "result": deepcopy(result)} for arm in ("control", "candidate")]
    original = policy.policy_context((policy.API_ROOT / policy.TARGET).read_text())["original_prompt"]
    gold = [{"case_id": case.case_id, "expected_route": "OTHER", "expected_decision_type": "CLARIFY"}]
    score = comparator.score_round([case], gold, records, [], catalog, original, original + policy.MARKER + INSTRUCTION, 1, checks_ok=True)
    assert score["decision"] == "INCONCLUSIVE" and not score["strategy_evaluable"]
    assert score["arms"]["candidate"]["planned"] == 1 and score["non_adaptive_case_ids"] == [case.case_id]
    packet = diagnosis.feedback_packet([case], records, score["rows"], [])
    assert all(i["optimization_scope"] == "OUTSIDE_PROMPT_POLICY" for i in packet["items"])


def test_two_rounds_really_feed_diagnosis_to_advisor(routing, source):
    """第二轮自动看见第一轮约束错误并自主生成不同说明，独立评分能证实实际开发改善。"""
    requests = []
    opts = options(routing, source)
    before, active = file_manifest(source), (policy.API_ROOT / policy.TARGET).read_bytes()
    checked = iteration.check(**opts)
    assert not opts["output"].exists() and checked["planned_request_upper_bound"] == 98
    report = iteration.run(**opts, backend_factory=provider(requests=requests))
    if report["decision"] == "INVALID_EXPERIMENT":
        comparator._compare(opts["output"])  # 只在Fake验收展开可信错误位置，产品回执仍保持脱敏。
    assert report["decision"] == "DEVELOPMENT_IMPROVED", report
    assert [r["score"]["decision"] for r in report["rounds"]] == ["REGRESSION", "IMPROVED"]
    assert report["requests_used"] == len(requests) == 34 and report["advisor_calls"] == 2
    assert report["candidate_retained"] and report["retained_round"] == 2 and not report["automatically_adopted"]
    advisors = [r for r in requests if r["role"] == "ADVISOR"]
    for number, advisor in enumerate(advisors, 1):
        assert all(i["feedback_id"].startswith(f"R{number}F") for i in advisor["user_payload"]["feedback"]["items"])
        assert "baseline_sha256" not in advisor["user_payload"]["source_context"]
    assert any(d["code"] == "SHOW_ALL_REQUIRES_RESOLVED_CLARIFICATION"
        for i in advisors[1]["user_payload"]["feedback"]["items"] for d in i["parameter_diagnostics"])
    assert len(advisors[1]["user_payload"]["history"]) == 2
    serialized = json.dumps(requests, ensure_ascii=False)
    assert "请说明活动通知的要求并引用原文。" not in serialized and "查找会议安排，只列文件名。" not in serialized
    assert "expected_route" not in serialized and "expected_decision_type" not in serialized
    assert not any(r["case_id"] in {"H1", "H2"} for r in requests)
    for root in (opts["output"] / "rounds").iterdir():
        assert not (root / "candidate" / "development-gold.json").exists()
    artifacts = file_manifest(opts["output"])
    assert comparator.compare(opts["output"]) == report
    assert file_manifest(opts["output"]) == artifacts and file_manifest(source) == before
    assert (policy.API_ROOT / policy.TARGET).read_bytes() == active


def test_legacy_long_hash_rejection_keeps_original_verdict(routing, source):
    """重建旧v1漏一位Hash的Fake记录，升级后仍按旧协议拒绝，不补字或改写历史。"""
    opts = options(routing, source)
    iteration.run(**opts, backend_factory=provider("bad_proposal"))
    output, root = opts["output"], opts["output"] / "rounds/0001"
    meta = json.loads((output / "experiment.json").read_text())
    meta.update(schema_version="adaptive-iteration-experiment-v1", trusted_code_ref=comparator.KNOWN_ITERATION_V1_REF)
    (output / "experiment.json").write_text(json.dumps(meta))
    packet = json.loads((root / "feedback.json").read_text())
    raw = json.loads((root / "proposal.json").read_text())
    raw.pop("baseline_ref")
    source_text = (output / "baseline" / policy.TARGET).read_text()
    raw.update(schema_version="adaptive-policy-proposal-v1", baseline_sha256=policy.source_hash(source_text),
               feedback_refs=[packet["items"][0]["feedback_id"][:-1]])
    (root / "proposal.json").write_text(json.dumps(raw))
    (root / "validation.json").write_text(json.dumps({"stage": "REJECTED", "reason_code": "PATCH_SCHEMA_INVALID"}))
    (root / "reference-map.json").unlink()  # 仅删本测试生成的Fake产物，真实历史记录不可修改。
    context = policy.policy_context(source_text)
    context["tool_input_schemas"] = diagnosis.tool_contracts()
    history = json.loads((root / "history.json").read_text())["attempts"]
    request_ref = digest([iteration.LEGACY_ADVISOR_PROMPT, iteration.advisor_payload(packet, context, history, version="v1")])
    for path in (output / "calls").glob("*.json"):
        call = json.loads(path.read_text())
        call["request_ref"] = request_ref
        path.write_text(json.dumps(call))
    summary = json.loads((output / "run-summary.json").read_text())
    summary["stop_reason"] = "PATCH_SCHEMA_INVALID"
    (output / "run-summary.json").write_text(json.dumps(summary))
    manifest = file_manifest(output)
    for name in ("integrity.json", "comparison.json", "summary.md"):
        manifest.pop(name, None)
    (output / "integrity.json").write_text(json.dumps(manifest))
    before = file_manifest(output)
    report = comparator.compare(output)
    assert report["decision"] == "NOT_RUN" and report["stop_reason"] == "PATCH_SCHEMA_INVALID", report
    assert report["requests_used"] == 1 and file_manifest(output) == before


@pytest.mark.parametrize("kind,reason,calls", [
    ("no_change", "MODEL_SELECTED_NO_CHANGE", 1), ("repeat", "REPEATED_CANDIDATE", 18),
    ("bad_proposal", "ITERATION_FEEDBACK_REFERENCE_UNKNOWN", 1),
    ("advisor_failure", "ITERATION_ADVISOR_OR_ISOLATION_FAILED", 1),
    ("interrupt", "USER_INTERRUPTED", 2), ("no_progress", "NO_PROGRESS_LIMIT", 34),
])
def test_authorized_stops_do_not_retry_or_deploy(routing, source, kind, reason, calls):
    """模型有不改的权利，重复、无进展、失败和中断都停止，不能借下一轮自动重试。"""
    opts = options(routing, source)
    report = iteration.run(**opts, backend_factory=provider(kind))
    assert report.get("stop_reason") == reason, report
    assert report["requests_used"] == calls and not report["candidate_retained"] and not report["automatically_adopted"]
    assert comparator.compare(opts["output"]) == report
    assert "PRIVATE_EXCEPTION_SENTINEL" not in json.dumps(report)


def test_global_budget_is_not_reset_at_round_boundary(routing, source):
    """首轮用了17次，第二轮只剩1次建议额度，随后必须停，不能再给整轮新额度。"""
    opts = options(routing, source, authorization=iteration.IterationAuthorization(max_total_requests=18))
    report = iteration.run(**opts, backend_factory=provider())
    assert report["stage"] == "STOPPED" and report["stop_reason"] == "REQUEST_BUDGET_EXHAUSTED", report
    assert report["requests_used"] == 18 and report["advisor_calls"] == 2 and not report["candidate_retained"]


@pytest.mark.parametrize("failure", ["time", "checks", "isolation"])
def test_time_and_protected_execution_failures_stop_before_another_candidate(routing, source, monkeypatch, failure):
    """总时间和保护检查优先于迭代，基础设施失败不能自动进入下一轮掩盖错误。"""
    from app.modules.evidence_harness.executor import ExperimentStopped
    from app.modules.evidence_harness.improvement.executor import Journal
    if failure == "time":
        class ShortJournal(Journal):
            """模拟建议调用后总时间耗尽，不使用真实等待或真实模型。"""

            def remaining(self):
                """所有阶段都应尊重同一个剩余时间。"""
                if self.used:
                    raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
                return super().remaining()
        monkeypatch.setattr(iteration, "Journal", ShortJournal)
    elif failure == "checks":
        monkeypatch.setattr(iteration, "run_checks", lambda *a: {"passed": False})
    else:
        def broken(*a, **kw):
            """隔离器拒绝执行时不得假造失败样本后继续消费模型额度。"""
            raise RuntimeError("PRIVATE_INFRASTRUCTURE_SENTINEL")
        monkeypatch.setattr(iteration, "run_trial", broken)
    opts = options(routing, source)
    report = iteration.run(**opts, backend_factory=provider())
    assert report["decision"] == "INCONCLUSIVE", report
    assert report["stage"] == ("STOPPED" if failure == "time" else "FAILED")
    assert report["requests_used"] == report["advisor_calls"] == 1 and not report["candidate_retained"]


@pytest.mark.parametrize("api_override,live", [(False, False), (True, False), (False, True)])
def test_cli_provider_and_zero_call_check(routing, source, monkeypatch, capsys, api_override, live):
    """新入口复用env选择Codex并支持原API覆盖；建议和规划共享配置但使用独立输出额度。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    from app.modules.evidence_harness.improvement import codex_backend
    from app.tests.test_codex_harness_backend import fake_spec
    monkeypatch.setenv("EVIDENCE_HARNESS_PROVIDER", "codex-login")
    monkeypatch.setenv("EVIDENCE_HARNESS_CODEX_MODEL", "fake-model")
    monkeypatch.setenv("EVIDENCE_HARNESS_CODEX_TRANSPORT", "https")
    monkeypatch.setenv("EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE", "true")
    inspections = []
    def inspect(model, *, transport):
        """只检查明确传入的Provider选项，禁止测试访问本机账号或网络。"""
        inspections.append((model, transport))
        return {**fake_spec(), "model": model, "transport": transport}
    monkeypatch.setattr(codex_backend, "inspect", inspect)
    original_check, original_run = iteration.check, iteration.run
    requests = []
    monkeypatch.setattr(iteration, "check", lambda **kw: original_check(**{**kw, "source": "fake"}))
    monkeypatch.setattr(iteration, "run", lambda **kw: original_run(**{**kw, "source": "fake"}, backend_factory=provider("improve", requests)))
    opts = options(routing, source)
    monkeypatch.setattr(sys, "argv", ["harness", "adaptive-iterate", "--source", str(source), "--output", str(opts["output"]),
        "--max-requests", "100", *( ["--provider", "openai-compatible"] if api_override else []), *( ["--live"] if live else [])])
    loader = config.get_settings
    try:
        config.get_settings = lambda: routing.settings
        evidence_harness.main()
    finally:
        config.get_settings = loader
    result = json.loads(capsys.readouterr().out)
    assert result["provider"] == ("openai-compatible" if api_override else "codex-login")
    assert inspections == ([] if api_override else [("fake-model", "https")])
    if live:
        assert result["decision"] == "DEVELOPMENT_IMPROVED" and result["requests_used"] == 17
        assert requests[0]["max_output_tokens"] == 8000 and all(r["max_output_tokens"] == 3000 for r in requests[1:])
        assert all("output_schema" in r["user_payload"] for r in requests)
    else:
        assert result["stage"] == "CHECK_PASSED" and result["model_calls"] == 0 and not opts["output"].exists()
        assert result["development_case_count"] == 4 and result["configuration_sources"]["provider"] == ("CLI" if api_override else "ENV")


@pytest.mark.parametrize("target", ["feedback", "request", "candidate", "score", "gold", "profile", "mapping", "resolved"])
def test_independent_reconstruction_rejects_or_ignores_forged_artifacts(routing, source, target):
    """即使重封完整性清单，伪造反馈/请求/代码/Gold仍拒绝；缓存分数不能左右结论。"""
    opts = options(routing, source)
    original = iteration.run(**opts, backend_factory=provider("improve"))
    output, root = opts["output"], opts["output"] / "rounds/0001"
    if target == "candidate":
        path = root / "candidate" / policy.TARGET
        path.chmod(0o600)
        path.write_text(path.read_text() + "\nPRIVATE_MODIFICATION = True\n")
    else:
        path = {"feedback": root / "feedback.json", "request": output / "calls/0001.started.json",
                "score": root / "score.json", "gold": output / "development-gold.json", "profile": output / "experiment.json",
                "mapping": root / "reference-map.json", "resolved": root / "resolved-proposal.json"}[target]
        value = json.loads(path.read_text())
        if target == "feedback":
            item = value["items"][0]
            item["task_outcome"] = "INCORRECT" if item["task_outcome"] == "CORRECT" else "CORRECT"
        elif target == "request":
            value["request_ref"] = "0" * 64
        elif target == "score":
            value["decision"] = "NO_GAIN"
        elif target == "gold":
            value["labels"][0]["expected_route"] = "OTHER"
        elif target == "mapping":
            value["entries"]["R1F001"] = "0" * 64
        elif target == "resolved":
            value["feedback_refs"] = ["0" * 64]
        else:
            value["profile"]["planner_max_output_tokens"] = 8000
        path.write_text(json.dumps(value))
    manifest = file_manifest(output)
    for name in ("integrity.json", "comparison.json", "summary.md"):
        manifest.pop(name, None)
    (output / "integrity.json").write_text(json.dumps(manifest))
    report = comparator.compare(output)
    assert report == original if target == "score" else report["decision"] == "INVALID_EXPERIMENT"


@pytest.mark.skipif(os.environ.get("HARNESS_OS_ACCEPTANCE") != "1", reason="真实macOS权限需单独验收")
def test_os_autonomous_candidate_runs_with_fake_provider(routing, c_data, monkeypatch):
    """真实OS隔离执行新候选与固定回归；候选没有Gold、密钥、网络或文件写权限。"""
    from app.modules.evidence_harness.improvement import executor, isolation
    source_opts = c_options(routing, c_data, name="os-source")
    source_report = validation.run(**source_opts, backend_factory=validation_factory())
    assert source_report["decision"] == "VALIDATED"
    monkeypatch.setattr(executor, "isolated_run", isolation.isolated_run)
    monkeypatch.setattr(iteration, "probe_sandbox", isolation.probe_sandbox)
    monkeypatch.setattr(iteration, "run_checks", executor.run_checks)
    opts = options(routing, source_opts["output"], authorization=iteration.IterationAuthorization(max_total_requests=49, max_rounds=1, repetitions=1))
    report = iteration.run(**opts, backend_factory=provider("improve"))
    assert report["decision"] == "DEVELOPMENT_IMPROVED", report
    assert report["requests_used"] == 9 and not report["automatically_adopted"]
    assert comparator.compare(opts["output"]) == report
