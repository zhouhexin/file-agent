"""B阶段自主追加规划说明验收，Fake只替代模型与快速测试的OS边界。"""

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement import executor, planning_evaluation as evaluation
from app.modules.evidence_harness.routing.runtime import run_planning
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic
from app.tests.test_routing_harness import routing
from app.tests.test_planning_evaluation import model_factory

INSTRUCTION = "请保持用户的最终任务目标，并用应用支持的稳定意图表示它。正文问题即使需要先发现来源，也应保留正文任务；单纯文件清单应保留文件搜索目标。"


@pytest.fixture
def fast_os(monkeypatch, routing):
    """快速测试只跑可信原版代码，实际系统权限另由OS专项验证。"""
    probe = lambda *a, **k: {"compile": True, "read_blocked": True, "write_blocked": True, "network_blocked": True, "exec_blocked": True}
    monkeypatch.setattr(evaluation, "probe_sandbox", probe)
    monkeypatch.setattr(policy, "probe_sandbox", probe)
    monkeypatch.setattr(policy, "run_checks", lambda *a: policy.expected_checks())

    def isolated(code, scratch, request, *, broker, timeout, planner_mode):
        """模拟唯一提示常量差异，模型请求仍经过真实宿主代理和全局预算。"""
        from app.modules.agent.adaptive_planner import ADAPTIVE_PLANNER_SYSTEM_PROMPT
        from app.modules.evidence_harness.routing.schemas import RoutingCase
        prompt = policy.prompt_node((code / policy.TARGET).read_text())[1].value.value
        refs = []

        class Client:
            """模拟worker IPC，不获得凭证或人工标签。"""

            def complete_json(self, *, system_prompt, user_payload):
                """仅候选常量不同；其余Prompt、原始输入和schema仍由实际服务构造。"""
                actual = prompt if system_prompt == ADAPTIVE_PLANNER_SYSTEM_PROMPT else system_prompt
                refs.append(digest([actual, user_payload]))
                result = broker({"type": "MODEL_REQUEST", "system_prompt": actual, "user_payload": user_payload})
                if result["status"] != "COMPLETED":
                    raise LLMResponseError("BROKER_MODEL_FAILED", diagnostic=make_diagnostic(category="INTERNAL_ERROR", stage="LLM_REQUEST"))
                return result["raw"]

        case = RoutingCase.model_validate(request["case"])
        assert case.adaptive_mode == planner_mode
        result = run_planning(case, catalog=request["catalog"], client=Client(), settings=routing.settings,
                              capture_audit=request["capture_audit"], quiet=True)
        for response, ref in zip(result["planning_audit"]["responses"], refs):
            response["request_ref"] = ref
        return result

    monkeypatch.setattr(executor, "isolated_run", isolated)


def factory(kind="improve", requests=None):
    """Fake根据实际收到的候选说明作出不同计划，不能只按实验组假称策略生效。"""
    requests = requests if requests is not None else []

    def build(role, round_id, arm, case, repetition):
        """每题独立Provider，同一次请求仍由可信Journal统一记账。"""
        class Client:
            """固定供应商返回，schema与范围校验均使用真实服务。"""

            def complete_json_with_usage(self, **request):
                """记录实际载荷以检查Gold、密钥及两组提示身份。"""
                requests.append({"role": role, "arm": arm, "case_id": case.case_id if case else None, **deepcopy(request)})
                if kind == "interrupt" and role == "PLANNER" and arm == "candidate":
                    raise KeyboardInterrupt
                if kind == "advisor_timeout" and role == "ADVISOR" or kind == "planner_timeout" and role == "PLANNER" and arm == "candidate":
                    raise LLMResponseError("private", diagnostic=make_diagnostic(category="TIMEOUT", stage="LLM_REQUEST"))
                if role == "ADVISOR":
                    payload = request["user_payload"]
                    raw = {"schema_version": "adaptive-policy-proposal-v1", "decision": "PROPOSE_POLICY",
                        "feedback_refs": [payload["feedback"]["items"][0]["feedback_id"]],
                        "baseline_sha256": payload["source_context"]["baseline_sha256"], "diagnosis": "BUSINESS_FAILURE",
                        "evidence_summary": "模型调用成功，但独立开发反馈仍有任务错误；需保留正常文件清单行为。",
                        "hypothesis": "通用补充说明可能保留正文目标并使用受支持的应用意图。",
                        "instruction": INSTRUCTION, "no_change_reason": None, "reason": "只提出最小通用说明，效果待独立复测。"}
                    if kind in {"no_change", "false_no_error"}:
                        raw.update(decision="NO_CHANGE", instruction=None, hypothesis=None,
                            no_change_reason="NO_ACTIONABLE_FAILURE" if kind == "false_no_error" else "INSUFFICIENT_EVIDENCE",
                            diagnosis="NO_ACTIONABLE_FAILURE" if kind == "false_no_error" else "INSUFFICIENT_EVIDENCE")
                    if kind == "foreign_ref":
                        raw["feedback_refs"] = ["0" * 64]
                    if kind == "sample":
                        raw["instruction"] = "对于C1请输出正文目标。"
                    return raw, {"input_tokens": 10, "output_tokens": 5}
                candidate = INSTRUCTION in request["system_prompt"]
                content = "引用原文" in request["user_payload"]["message"]
                result, usage = model_factory("http_fallback" if kind == "fallback" and candidate else "success")(case, repetition).complete_json_with_usage(**request)
                if candidate and kind == "unknown_id" and "scope" in result:
                    result["scope"]["document_ids"] = ["FOREIGN_DOCUMENT"]
                elif candidate and kind == "safety":
                    result["tool_plan"]["steps"][0]["requires_confirmation"] = True
                elif not candidate or kind == "no_gain" or kind == "regress" and not content:
                    result["intent"] = "ANSWER_QUESTION_FROM_FILES" if candidate and kind == "regress" and not content else "定位来源文件"
                return result, {} if kind == "unknown_usage" and candidate else usage
        return Client()
    return build


def a_factory(case, repetition):
    """A基线真实出现自由文本intent，模拟已观察到的问题，不改Gold。"""
    return factory()("PLANNER", 1, "control", case, repetition)


def make_baseline(routing):
    """通过实际A入口生成冻结产物，禁止手写反馈冒充来源。"""
    output = routing.root / "experiments/harness-v1/a-baseline"
    report = evaluation.run_evaluation(dataset_path=routing.dataset, output=output, planner_mode="enabled", settings=routing.settings,
        authorization=evaluation.PlanningEvaluationAuthorization(), source="fake", backend_factory=a_factory)
    assert report["decision"] == "EVALUATED" and report["correct"] == 1, report
    return output


def options(routing, baseline_dir, name="b-policy", **updates):
    """每个实验使用全新目录和显式总授权。"""
    return {"baseline_dir": baseline_dir, "output": routing.root / f"experiments/harness-v1/{name}",
        "settings": routing.settings, "authorization": policy.PolicyAuthorization(), "source": "fake", **updates}


@pytest.mark.parametrize("kind,decision", [("improve", "IMPROVED"), ("no_gain", "NO_GAIN"), ("regress", "REGRESSION"),
    ("fallback", "INCONCLUSIVE"), ("planner_timeout", "INCONCLUSIVE"), ("unknown_id", "INCONCLUSIVE"),
    ("safety", "REGRESSION"), ("unknown_usage", "IMPROVED")])
def test_real_policy_difference_and_independent_scores(routing, fast_os, kind, decision):
    """真正追加模型内容并运行规划，两组计划重放评分；回退、退步和费用未知如实记录。"""
    requests = []
    source = make_baseline(routing)
    opts = options(routing, source)
    original = (policy.API_ROOT / policy.TARGET).read_bytes()
    source_before = {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    report = policy.run_cycle(**opts, backend_factory=factory(kind, requests))
    assert report["decision"] == decision, report
    assert report["arms"]["control"]["correct"] == 1 and not report["automatically_adopted"]
    if kind == "improve":
        assert report["arms"]["candidate"]["correct"] == 2 and report["candidate_retained"]
    before = {str(p): p.read_bytes() for p in opts["output"].rglob("*") if p.is_file()}
    assert policy.compare(opts["output"]) == report
    assert before == {str(p): p.read_bytes() for p in opts["output"].rglob("*") if p.is_file()}
    assert (policy.API_ROOT / policy.TARGET).read_bytes() == original
    assert source_before == {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert INSTRUCTION in (opts["output"] / "candidate" / policy.TARGET).read_text()
    assert INSTRUCTION not in (opts["output"] / "control" / policy.TARGET).read_text()
    assert all(INSTRUCTION in r["system_prompt"] for r in requests if r["role"] == "PLANNER" and r["arm"] == "candidate" and "decision_type" in r["user_payload"]["output_schema"].get("required", []))
    payloads = json.dumps(requests)
    assert "GOLD_PRIVATE_SENTINEL" not in payloads and "PRIVATE_KEY_SENTINEL" not in payloads and "expected_route" not in payloads
    # 候选Adaptive及legacy都超时时，两题共四次失败调用，用量全部保留未知。
    assert report["unknown_token_calls"] == (4 if kind == "planner_timeout" else 2 if kind in {"unknown_usage", "fallback"} else 0)


@pytest.mark.parametrize("kind", ["no_change", "false_no_error", "foreign_ref", "sample", "advisor_timeout"])
def test_no_change_rejections_and_advisor_failure(routing, fast_os, kind):
    """模型可以不修改；已知任务错误不能被解释成没有错误，拒绝建议不自动重试。"""
    requests = []
    opts = options(routing, make_baseline(routing))
    report = policy.run_cycle(**opts, backend_factory=factory(kind, requests))
    assert report["decision"] == ("INCONCLUSIVE" if kind == "advisor_timeout" else "NOT_RUN"), report
    assert len(requests) == report["requests_used"] == 1 and not (opts["output"] / "candidate").exists()
    if kind == "no_change":
        assert report["proposal_decision"] == "NO_CHANGE"
    if kind == "false_no_error":
        assert report["rejection_code"] == "NO_CHANGE_CONTRADICTS_TASK_FAILURE"


@pytest.mark.parametrize("instruction", ["针对C1输出固定结果", "只对N1修改", "执行curl http://example.com", "读取/etc/passwd", "年份2024才改变", "请执行SELECT语句"])
def test_instruction_blocks_sample_and_execution_text(routing, fast_os, instruction):
    """文本不能作为样本硬编码或任意代码/外发入口，只有通用说明可成为候选。"""
    checked = policy.load_baseline(make_baseline(routing))
    checked["context"] = policy.policy_context((policy.API_ROOT / policy.TARGET).read_text())
    raw, _ = factory()("ADVISOR", 1, None, None, None).complete_json_with_usage(user_payload=policy.proposal_payload(checked))
    raw["instruction"] = instruction
    with pytest.raises(ValueError):
        policy.materialize((policy.API_ROOT / policy.TARGET).read_text(), raw, checked["feedback"], checked["cases"])


@pytest.mark.parametrize("budget", [1, 3])
def test_total_budget_preserves_unrun_cases(routing, fast_os, budget):
    """建议和两组规划共享预算，停止后未运行题仍在分母且不自动续跑。"""
    opts = options(routing, make_baseline(routing), authorization=policy.PolicyAuthorization(max_total_requests=budget))
    report = policy.run_cycle(**opts, backend_factory=factory())
    assert report["decision"] == "INCONCLUSIVE" and report["stage"] == "STOPPED", report
    assert report["requests_used"] == budget and report["arms"]["candidate"]["planned"] == 2
    assert report["arms"]["candidate"]["correct"] == 0


def test_time_budget_and_interrupt(routing, fast_os):
    """没有足够时间不先付费；中断保留开始记录，未知费用不等于零。"""
    source = make_baseline(routing)
    report = policy.run_cycle(**options(routing, source, authorization=policy.PolicyAuthorization(max_active_seconds=1)), backend_factory=factory())
    assert report["stage"] == "STOPPED" and report["requests_used"] == 0
    report = policy.run_cycle(**options(routing, source, name="interrupt"), backend_factory=factory("interrupt"))
    assert report["stage"] == "INTERRUPTED" and report["decision"] == "INCONCLUSIVE", report
    assert report["requests_used"] == 4 and report["unknown_token_calls"] == 1


@pytest.mark.parametrize("changed", ["source", "prompt", "audit", "request", "mode", "extra_code", "feedback", "gold"])
def test_tampered_artifacts_rejected_even_with_new_integrity(routing, fast_os, changed):
    """刷新散列清单也不能伪造来源、模型请求、源码差异或实际模式。"""
    opts = options(routing, make_baseline(routing))
    assert policy.run_cycle(**opts, backend_factory=factory())["decision"] == "IMPROVED"
    root = opts["output"]
    if changed == "extra_code":
        p = root / "candidate" / policy.TARGET
        p.chmod(0o600)
        p.write_text(p.read_text() + "\nFORGED_EXECUTION = True\n")
    else:
        p = {"source": root / "experiment.json", "prompt": root / "policy.json", "audit": root / "trials/candidate-C1-1.json",
             "request": root / "calls/0004.started.json", "mode": root / "trials/candidate-C1-1.json", "feedback": root / "feedback.json",
             "gold": routing.dataset}.get(changed)
        data = json.loads(p.read_text())
        if changed == "source": data["source_ref"] = "0" * 64
        if changed == "prompt": data["instruction"] = "伪造说明"
        if changed == "audit": data["result"]["planning_audit"]["responses"][0]["raw"]["intent"] = "SEARCH_FILES"
        if changed == "request": data["request_ref"] = "0" * 64
        if changed == "mode": data["result"]["planning_audit"]["effective_mode"] = "LEGACY"
        if changed == "feedback": data["items"][0]["task_outcome"] = "CORRECT"
        if changed == "gold": data["gold"][0]["expected_route"] = "FILE_LIST"
        p.write_text(json.dumps(data))
    manifest = policy.file_manifest(root)
    for name in ("integrity.json", "comparison.json", "summary.md"):
        manifest.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(manifest))
    assert policy.compare(root)["decision"] == "INVALID_EXPERIMENT"


def test_default_cli_and_source_guards(routing, fast_os, monkeypatch, capsys):
    """默认只核验来源，不写目录、不调用模型；重复目录及来源混用在外发前停止。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    opts = options(routing, make_baseline(routing))
    original = policy.check_cycle
    monkeypatch.setattr(policy, "check_cycle", lambda **kw: original(**{**kw, "source": "fake"}))
    monkeypatch.setattr(sys, "argv", ["harness", "adaptive-cycle", "--baseline", str(opts["baseline_dir"]), "--output", str(opts["output"])])
    settings_loader = config.get_settings
    try:
        config.get_settings = lambda: routing.settings
        evidence_harness.main()
    finally:
        config.get_settings = settings_loader
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "CHECK_PASSED" and report["model_calls"] == 0 and report["planned_request_upper_bound"] == 13
    assert not opts["output"].exists()
    with pytest.raises(ValueError, match="SOURCE_MISMATCH"):
        original(**{**opts, "source": "live"})
    opts["output"].mkdir()
    with pytest.raises(FileExistsError):
        original(**opts)


def test_explicit_model_quotas_and_profile_binding(routing, fast_os):
    """建议器额度独立生效；更换模型或伪造本次模型身份不能沿用旧A比较。"""
    source = make_baseline(routing)
    requests = []
    auth = policy.PolicyAuthorization(advisor_max_output_tokens=6000, planner_max_output_tokens=8000)
    opts = options(routing, source, authorization=auth)
    report = policy.run_cycle(**opts, backend_factory=factory(requests=requests))
    assert report["decision"] == "IMPROVED", report
    assert all(r["max_output_tokens"] == (6000 if r["role"] == "ADVISOR" else 8000) for r in requests)
    with pytest.raises(ValueError, match="MODEL_PROFILE_CHANGED"):
        policy.check_cycle(**options(routing, source, name="wrong-model", settings=routing.settings.model_copy(update={"llm_chat_model": "OTHER_MODEL"})))
    root = opts["output"]
    meta = json.loads((root / "experiment.json").read_text())
    meta["model_ref"] = "0" * 64
    (root / "experiment.json").write_text(json.dumps(meta))
    manifest = policy.file_manifest(root)
    for name in ("integrity.json", "comparison.json", "summary.md"):
        manifest.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(manifest))
    assert policy.compare(root)["decision"] == "INVALID_EXPERIMENT"


def test_enriched_feedback_reaches_advisor_without_gold(routing, fast_os):
    """最终目标、计划动作、未知intent确实送到建议器；不改变路由或Gold。"""
    source = make_baseline(routing)
    requests = []
    opts = options(routing, source)
    report = policy.run_cycle(**opts, backend_factory=factory(requests=requests))
    assert report["decision"] == "IMPROVED", report
    payload = requests[0]["user_payload"]
    assert payload["feedback"]["schema_version"] == "adaptive-policy-feedback-v2"
    assert "task_diagnostic" in payload["feedback"]["items"][0]
    for item in payload["feedback"]["items"]:
        diagnostic = item["task_diagnostic"]
        assert diagnostic["final_task_goal"]["original_request"]
        assert diagnostic["current_tool_action"]["purpose"] == "FILE_DISCOVERY"
        assert diagnostic["current_tool_action"]["status"] == "PLANNED_NOT_EXECUTED"
        assert diagnostic["intent_contract"]["planner_schema_valid"] is True
        assert diagnostic["intent_contract"]["mapping_status"] == "UNRECOGNIZED"
        assert diagnostic["mismatch_observations"][0]["code"] == "UNRECOGNIZED_TASK_INTENT_FOR_DISCOVERY"
    assert "current_tool_action" in requests[0]["system_prompt"]
    assert "GOLD_PRIVATE_SENTINEL" not in json.dumps(payload) and "expected_route" not in json.dumps(payload)
    assert policy.feedback_summary(payload["feedback"])["intent_contract_issue_count"] == 2


def renew_integrity(root):
    """用于对抗测试：攻击者更新校验清单也不能绕过来源重建和请求身份。"""
    manifest = policy.file_manifest(root)
    for name in ("integrity.json", "comparison.json", "summary.md"):
        manifest.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize("field", ["final_task_goal", "current_tool_action", "intent_contract"])
def test_task_diagnostic_tampering_is_rejected(routing, fast_os, field):
    """新增机器事实也要由原轨迹重建，不能相信手写或缓存诊断。"""
    opts = options(routing, make_baseline(routing))
    assert policy.run_cycle(**opts, backend_factory=factory())["decision"] == "IMPROVED"
    root = opts["output"]
    packet = json.loads((root / "feedback.json").read_text())
    packet["items"][0]["task_diagnostic"][field] = {"forged": True}
    (root / "feedback.json").write_text(json.dumps(packet))
    renew_integrity(root)
    assert policy.compare(root)["decision"] == "INVALID_EXPERIMENT"


def test_v1_readonly_comparison_uses_original_feedback_and_prompt(routing, fast_os):
    """构造私有Fake旧协议产物，旧反馈不可用新版字段补写后冒充历史事实。"""
    source = make_baseline(routing)
    opts = options(routing, source)
    assert policy.run_cycle(**opts, backend_factory=factory("no_gain"))["decision"] == "NO_GAIN"
    root = opts["output"]
    old = policy.load_baseline(source, feedback_version="adaptive-policy-feedback-v1")
    old["context"] = policy.policy_context((policy.API_ROOT / policy.TARGET).read_text(), feedback_version="adaptive-policy-feedback-v1")
    packet = old["feedback"]
    assert all("task_diagnostic" not in i for i in packet["items"])
    (root / "feedback.json").write_text(json.dumps(packet))
    proposal = json.loads((root / "proposal.json").read_text())
    proposal["feedback_refs"] = [packet["items"][0]["feedback_id"]]
    (root / "proposal.json").write_text(json.dumps(proposal))
    meta = json.loads((root / "experiment.json").read_text())
    meta.update(schema_version="adaptive-policy-experiment-v1", trusted_code_ref=policy.KNOWN_B_V1_TRUSTED_REF,
                feedback_ref=digest(packet))
    meta.pop("feedback_schema_version")
    (root / "experiment.json").write_text(json.dumps(meta))
    for path in (root / "calls").glob("0001.*.json"):
        call = json.loads(path.read_text())
        call["request_ref"] = digest([policy.POLICY_PROMPT_V1, policy.proposal_payload(old)])
        path.write_text(json.dumps(call))
    renew_integrity(root)
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert policy.compare(root)["decision"] == "NO_GAIN"
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    # 当前v2执行器也不能把新协议产物冒称为旧v1；只认已登记旧版本身份。
    for invalid_ref in ("0" * 64, policy.trusted_code_ref()):
        meta["trusted_code_ref"] = invalid_ref
        (root / "experiment.json").write_text(json.dumps(meta))
        renew_integrity(root)
        assert policy.compare(root)["decision"] == "INVALID_EXPERIMENT"


@pytest.mark.skipif(os.environ.get("HARNESS_OS_ACCEPTANCE") != "1", reason="真实系统隔离须在本机专项中运行")
def test_os_actual_candidate_prompt_and_env_boundary(routing):
    """真实隔离进程运行模型生成提示副本；只有模型Provider为Fake，上级env仍不可读。"""
    routing.root.mkdir(parents=True, exist_ok=True)
    (routing.root / ".env").write_text("LLM_API_KEY=OS_PRIVATE_SENTINEL\nDATABASE_URL=postgresql://wrong/production\n")
    source = make_baseline(routing)
    requests = []
    opts = options(routing, source)
    report = policy.run_cycle(**opts, backend_factory=factory(requests=requests))
    assert report["decision"] == "IMPROVED", report
    assert report["requests_used"] == 5 and report["arms"]["candidate"]["correct"] == 2
    assert policy.compare(opts["output"]) == report
    from app.modules.evidence_harness.improvement.isolation import isolated_run
    assert isolated_run(opts["output"] / "candidate", routing.root / "blocked-env", {"mode": "probe", "action": "read",
        "path": str(routing.root / ".env")}, planner_mode="enabled")["blocked"]
    # Gold和原A实验位于同一宿主，但均不能被候选副本直接读取。
    for label, path in (("gold", routing.dataset), ("other-experiment", source / "experiment.json")):
        assert isolated_run(opts["output"] / "candidate", routing.root / ("blocked-" + label),
            {"mode": "probe", "action": "read", "path": str(path)}, planner_mode="enabled")["blocked"]
