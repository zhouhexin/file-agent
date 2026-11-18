"""Adaptive基线A阶段验收，Fake只替换模型；OS验收另行验证真实权限限制。"""

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.improvement import planning_evaluation as evaluation
from app.modules.evidence_harness.improvement import executor, service, comparator, isolation
from app.modules.evidence_harness.improvement.schemas import PlanningEvaluationAuthorization
from app.modules.evidence_harness.routing.runtime import run_planning
from app.modules.evidence_harness.routing.dataset import load_dataset
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic
from app.tests.test_routing_harness import routing


def options(routing, name="planning", **updates):
    """源数据集保持原来的legacy模式，本次模式只在新实验身份中覆盖。"""
    result = {"dataset_path": routing.dataset, "output": routing.root / f"experiments/harness-v1/{name}",
              "planner_mode": "enabled", "settings": routing.settings,
              "authorization": PlanningEvaluationAuthorization(), "source": "fake"}
    result.update(updates)
    return result


def model_factory(kind="success", requests=None):
    """返回真实PlannerDecision形状，绝不依赖Gold产生预测；非法响应交原服务回退。"""
    requests = requests if requests is not None else []
    def factory(case, repetition):
        """每道题创建独立计数，服务内回退与修复继续使用同一Provider。"""
        class Client:
            """仅模拟模型Provider，不替换真实后端校验。"""
            def complete_json_with_usage(self, **request):
                """区分两种请求schema，回退不会偷偷变成无模型的固定答案。"""
                payload = request["user_payload"]
                adaptive = "decision_type" in payload["output_schema"].get("required", [])
                requests.append({"case_id": case.case_id, "adaptive": adaptive, **deepcopy(request)})
                if kind == "interrupt":
                    raise KeyboardInterrupt
                if kind == "all_failed" or kind == "http_fallback" and adaptive:
                    raise LLMResponseError("private", diagnostic=make_diagnostic(category="TIMEOUT", stage="LLM_REQUEST"))
                content = "引用原文" in payload["message"]
                if not adaptive:
                    query = "培训通知"
                    if kind == "source_repair" and content:
                        query = "关于开展培训的通知" if payload["observation"] else None
                    return {"intent": "ANSWER_QUESTION_FROM_FILES" if content else "SEARCH_FILES",
                            "user_goal": payload["message"], "managed_query": query,
                            "required_capabilities": ["evidence_answer"] if content else ["file_search"]}, {"input_tokens": 10, "output_tokens": 5}
                if kind in {"schema_fallback", "source_repair"}:
                    return {"intent": "broken"}, {"input_tokens": 10, "output_tokens": 5}
                result = {"decision_type": "TOOL_PLAN", "intent": "ANSWER_QUESTION_FROM_FILES" if content else "SEARCH_FILES",
                          "user_goal": payload["message"], "selected_skill_ids": ["file-search"],
                          "scope": {"source": "unspecified", "document_ids": []},
                          "tool_plan": {"plan_id": "model-plan", "steps": [{"step_id": "search", "skill_id": "file-search",
                              "tool_name": "hybrid-search", "literal_input": {"query": "培训通知"}}]}}
                if kind == "unknown_tool":
                    result["tool_plan"]["steps"][0]["tool_name"] = "forbidden-tool"
                if kind == "foreign_id":
                    result["scope"]["document_ids"] = ["FOREIGN_DOCUMENT"]
                if kind == "confirmation":
                    result["tool_plan"]["steps"][0]["requires_confirmation"] = True
                if kind == "clarify":
                    result = {"decision_type": "CLARIFY", "intent": "UNKNOWN", "user_goal": payload["message"],
                              "clarification": {"question": "请指定来源文件"}}
                return result, {"input_tokens": 10, "output_tokens": 5}
        return Client()
    return factory


@pytest.fixture
def in_process_planning(monkeypatch, routing):
    """快速协议测试只跑可信原版规划，明确用Fake替代OS隔离；真实隔离另有专项。"""
    monkeypatch.setattr(evaluation, "probe_sandbox", lambda *a, **k: {
        "compile": True, "read_blocked": True, "write_blocked": True, "network_blocked": True, "exec_blocked": True})

    def isolated(code, scratch, request, *, broker, timeout, planner_mode):
        """模型请求仍经过真实宿主代理和预算，只有进程边界被此快速测试替换。"""
        from app.modules.evidence_harness.routing.schemas import RoutingCase
        class Client:
            """模拟worker IPC，不获得模型凭证。"""
            def complete_json(self, **kwargs):
                """原规划服务决定请求内容，代理负责校验和计数。"""
                response = broker({"type": "MODEL_REQUEST", **kwargs})
                if response["status"] != "COMPLETED":
                    raise LLMResponseError("BROKER_MODEL_FAILED", diagnostic=make_diagnostic(category="INTERNAL_ERROR", stage="LLM_REQUEST"))
                return response["raw"]
        case = RoutingCase.model_validate(request["case"])
        assert case.adaptive_mode == planner_mode
        return run_planning(case, catalog=request["catalog"], client=Client(), settings=routing.settings,
                            capture_audit=request["capture_audit"], quiet=True)
    monkeypatch.setattr(executor, "isolated_run", isolated)


def test_default_check_preserves_original_mode_and_input(routing):
    """显式模式作为新实验身份，不能修改或重新散列原冻结输入冒充同一数据。"""
    original = routing.dataset.read_bytes()
    checked = service.check_planning_evaluation(**options(routing))
    assert checked["planned_request_upper_bound"] == 6 and checked["authorization_covers_upper_bound"]
    assert all(c.adaptive_mode == "enabled" for c in checked["cases"])
    assert all(c.adaptive_mode == "legacy" for c in checked["dataset"].inputs.cases)
    assert routing.dataset.read_bytes() == original and not checked["output"].exists()
    with pytest.raises(ValueError, match="MODE_INVALID"):
        service.check_planning_evaluation(**options(routing, planner_mode="shadow"))


@pytest.mark.parametrize("mode", ["enabled", "legacy"])
def test_native_plans_replayed_without_model_or_log_writes(routing, in_process_planning, mode):
    """真实节点产生计划；同为hybrid-search的正文发现与文件搜索必须正确区分。"""
    requests = []
    opts = options(routing, planner_mode=mode)
    before = routing.dataset.read_bytes()
    report = service.run_planning_evaluation(**opts, backend_factory=model_factory(requests=requests))
    assert report["decision"] == "EVALUATED" and report["correct"] == report["requested_planner_correct"] == 2
    assert report["requests_used"] == len(requests) == 2 and not report["automatically_adopted"]
    effective = "ADAPTIVE" if mode == "enabled" else "LEGACY"
    assert report["effective_modes"] == {effective: 2}
    # legacy模型只决定意图；必须与Adaptive生成工具步骤的事实分开记录。
    audits = [json.loads(p.read_text())["result"]["planning_audit"] for p in (opts["output"] / "trials").glob("*.json")]
    assert all(a["model_generated_plan"] is (mode == "enabled") and a["model_generated_intent"] for a in audits)
    assert [r["predicted_route"] for r in report["rows"]] == ["CONTENT_ANSWER", "FILE_LIST"]
    logs = {str(p): p.read_bytes() for p in (routing.root / "logs").rglob("*") if p.is_file()}
    assert comparator.compare_planning_evaluation(opts["output"]) == report
    assert len(requests) == 2 and routing.dataset.read_bytes() == before
    assert logs == {str(p): p.read_bytes() for p in (routing.root / "logs").rglob("*") if p.is_file()}
    for r in requests:
        text = json.dumps(r)
        assert "GOLD_PRIVATE_SENTINEL" not in text and "PRIVATE_KEY_SENTINEL" not in text
        assert "expected_route" not in text


@pytest.mark.parametrize("kind", ["schema_fallback", "unknown_tool", "foreign_id", "http_fallback"])
def test_legacy_fallback_is_not_credited_as_adaptive(routing, in_process_planning, kind):
    """schema、工具、文件范围、HTTP失败均走真实回退，不能以最终答对冒充Adaptive成功。"""
    opts = options(routing)
    report = service.run_planning_evaluation(**opts, backend_factory=model_factory(kind))
    assert report["decision"] == "INCONCLUSIVE" and report["correct"] == 2
    assert report["requested_planner_correct"] == 0 and report["effective_modes"] == {"LEGACY": 2}
    assert report["requests_used"] == 4 and all(r["fallback_kind"] == "ADAPTIVE_TO_LEGACY" for r in report["rows"])
    audits = [json.loads(p.read_text())["result"]["planning_audit"] for p in (opts["output"] / "trials").glob("*.json")]
    if kind in {"unknown_tool", "foreign_id"}:
        assert all(a["validation_events"][0]["status"] == "REJECTED" for a in audits)
    if kind == "schema_fallback":
        assert all(not a["responses"][0]["schema_valid"] for a in audits)
    assert comparator.compare_planning_evaluation(opts["output"]) == report


@pytest.mark.parametrize("kind", ["all_failed", "confirmation", "clarify", "interrupt"])
def test_failure_confirmation_clarification_and_interrupt(routing, in_process_planning, kind):
    """不执行危险Tool，未评分与模型失败区分，澄清由模型提出后由后端接受。"""
    report = service.run_planning_evaluation(**options(routing), backend_factory=model_factory(kind))
    assert report["decision"] != "INVALID_EXPERIMENT", report
    if kind == "all_failed":
        assert report["effective_modes"] == {"RULE": 2} and report["failed_model_calls"] == 4
        assert report["requested_planner_correct"] == 0 and report["unknown_token_calls"] == 4
    if kind == "confirmation":
        assert report["correct"] == 0
        assert all("CONFIRMATION_REQUIRED" in r["violation_codes"] for r in report["rows"])
    if kind == "clarify":
        assert report["effective_modes"] == {"ADAPTIVE": 2} and all(r["predicted_route"] == "OTHER" for r in report["rows"])
    if kind == "interrupt":
        assert report["stage"] == "INTERRUPTED" and report["requests_used"] == 1
        assert report["unknown_token_calls"] == 1 and report["planned"] == 2


def test_budget_and_unrun_denominators(routing, in_process_planning):
    """预算包含失败/回退，未执行不从分母去除，时间不足不先发请求。"""
    report = service.run_planning_evaluation(**options(routing, authorization=PlanningEvaluationAuthorization(max_total_requests=1)),
                                           backend_factory=model_factory())
    assert report["stage"] == "STOPPED" and report["planned"] == 2 and report["recorded"] == 1
    assert report["feedback"]["items"][1]["task_outcome"] == "UNSCORED"
    report = service.run_planning_evaluation(**options(routing, name="time", authorization=PlanningEvaluationAuthorization(max_active_seconds=1)),
                                           backend_factory=model_factory())
    assert report["stage"] == "STOPPED" and report["requests_used"] == 0


def varied_dataset(routing, question):
    """只在测试私有目录另建题干变体，原数据集字节保持不变。"""
    from app.modules.evidence_evaluation.frozen_inputs import write_new_json
    raw = json.loads(routing.dataset.read_text())
    raw["inputs"]["cases"][0]["question"] = question
    raw["inputs_sha256"] = digest(raw["inputs"])
    output = routing.dataset.with_name("varied-cases.json")
    write_new_json(output, raw)
    return output


def test_source_repair_fits_three_request_upper_bound(routing, in_process_planning):
    """一次Adaptive加Legacy的来源修复共三请求，全部有身份记录且算入预算。"""
    path = varied_dataset(routing, "只根据《关于开展培训的通知》，说明培训对象并引用原文。")
    requests = []
    report = service.run_planning_evaluation(**options(routing, dataset_path=path),
                                           backend_factory=model_factory("source_repair", requests))
    assert report["requests_used"] == len(requests) == 5, report
    assert report["effective_modes"] == {"LEGACY": 2} and report["correct"] == 2
    assert sum(r["case_id"] == "C1" for r in requests) == 3


def test_preflight_is_explicit_and_not_credited_to_model(routing, in_process_planning):
    """完整文件名触发受控预检时没有调用模型，报告必须如实显示。"""
    path = varied_dataset(routing, "总结一下培训通知.pdf")
    report = service.run_planning_evaluation(**options(routing, dataset_path=path), backend_factory=model_factory())
    assert report["effective_modes"].get("PREFLIGHT") == 1, report
    assert not report["rows"][0]["requested_planner_exercised"]
    assert report["requests_used"] == 1


def test_no_tool_handler_is_called(routing, in_process_planning, monkeypatch):
    """包括需要确认的计划都仅作检查，不能实际执行任何Tool。"""
    from app.modules.agent.tool_registry import ToolRegistry
    def forbidden(*args, **kwargs):
        """只要发生真实Tool调度就使测试立即失败。"""
        raise AssertionError("NO_HANDLER_ALLOWED")
    monkeypatch.setattr(ToolRegistry, "invoke", forbidden)
    report = service.run_planning_evaluation(**options(routing), backend_factory=model_factory("confirmation"))
    assert report["decision"] == "EVALUATED" and report["correct"] == 0


def test_cli_defaults_to_check_without_directory_or_model(routing, monkeypatch, capsys):
    """独立入口默认零请求，现有冻结输入无需改写或重新上传。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    checked = options(routing)
    monkeypatch.setattr(service, "check_planning_evaluation", lambda **kwargs: evaluation.check_evaluation(**{**kwargs, "source": "fake"}))
    monkeypatch.setattr(sys, "argv", ["harness", "planning-evaluate", "--dataset", str(routing.dataset),
        "--output", str(checked["output"]), "--planner-mode", "enabled"])
    original = config.get_settings
    try:
        config.get_settings = lambda: routing.settings
        evidence_harness.main()
    finally:
        config.get_settings = original
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "CHECK_PASSED" and result["model_calls"] == 0 and result["planned_request_upper_bound"] == 6
    assert not checked["output"].exists()


def test_invalid_source_and_reuse_stop_before_dispatch(routing, in_process_planning):
    """真实/Fake混用、同名目录和向旧实验添加子目录都在外发前拒绝。"""
    with pytest.raises(ValueError, match="SOURCE_INVALID"):
        evaluation.check_evaluation(**options(routing, source="live"))
    opts = options(routing)
    service.run_planning_evaluation(**opts, backend_factory=model_factory())
    with pytest.raises(FileExistsError):
        service.check_planning_evaluation(**opts)
    with pytest.raises(ValueError, match="INSIDE_EXISTING"):
        service.check_planning_evaluation(**options(routing, output=opts["output"] / "nested"))


@pytest.mark.parametrize("target", ["mode", "result", "audit", "request", "gold"])
def test_independent_compare_rejects_tampering_even_with_new_integrity(routing, in_process_planning, target):
    """重写外层散列也不能让伪造模式、计划或Gold变成有效独立结果。"""
    opts = options(routing)
    service.run_planning_evaluation(**opts, backend_factory=model_factory())
    root = opts["output"]
    if target == "mode":
        path = root / "experiment.json"
        data = json.loads(path.read_text()); data["planner_mode"] = "legacy"
    elif target == "gold":
        path = routing.dataset
        data = json.loads(path.read_text()); data["gold"][0]["expected_route"] = "FILE_LIST"
    elif target == "request":
        path = root / "calls/0001.finished.json"
        data = json.loads(path.read_text()); data["request_ref"] = "0" * 64
    else:
        path = root / "trials/C1-1.json"
        data = json.loads(path.read_text())
        if target == "audit":
            data["result"]["planning_audit"]["effective_mode"] = "LEGACY"
        else:
            data["result"]["plan"]["intent"] = "SEARCH_FILES"
    path.write_text(json.dumps(data))
    manifest = isolation.file_manifest(root)
    for name in ("integrity.json", "comparison.json", "feedback.json", "summary.md"):
        manifest.pop(name, None)
    (root / "integrity.json").write_text(json.dumps(manifest))
    assert comparator.compare_planning_evaluation(root)["decision"] == "INVALID_EXPERIMENT"


def test_adaptive_proxy_rejects_unknown_prompts_and_changed_payload(routing):
    """新增Adaptive支持不等于允许候选任意外发Prompt或冒用schema。"""
    from app.modules.agent.adaptive_planner import ADAPTIVE_PLANNER_SYSTEM_PROMPT
    from app.modules.agent.planner_contracts import PlannerDecision
    case = evaluation.effective_case(load_dataset(routing.dataset).inputs.cases[0], "enabled")
    catalog = load_dataset(routing.dataset).inputs.catalog_snapshot
    request = {"type": "MODEL_REQUEST", "system_prompt": ADAPTIVE_PLANNER_SYSTEM_PROMPT,
               "user_payload": {"message": case.question, "attachments": [], "context_documents": [],
                   "catalog_snapshot": catalog, "observation": {}, "output_schema": PlannerDecision.model_json_schema()}}
    executor.validate_planner_request(request, case, catalog, phase="ADAPTIVE")
    request["user_payload"]["observation"] = {"anything": "forbidden"}
    with pytest.raises(ValueError):
        executor.validate_planner_request(request, case, catalog, phase="ADAPTIVE")
    request["system_prompt"] = "uncontrolled"
    with pytest.raises(ValueError):
        executor.validate_planner_request(request, case, catalog, phase="ADAPTIVE")


OS_ACCEPTANCE = os.environ.get("HARNESS_OS_ACCEPTANCE") == "1"


@pytest.mark.skipif(not OS_ACCEPTANCE, reason="真实系统隔离须通过本机固定验收脚本运行")
@pytest.mark.parametrize("kind", ["success", "foreign_id", "all_failed"])
def test_os_adaptive_planning_keeps_parent_env_and_model_secrets_out(routing, kind):
    """OS真实隔离+真实规划节点，只有Provider为Fake；上级.env存在且不能读取。"""
    root = routing.root
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("LLM_API_KEY=OS_PRIVATE_SENTINEL\nDATABASE_URL=postgresql://wrong/production\n")
    opts = options(routing)
    report = service.run_planning_evaluation(**opts, backend_factory=model_factory(kind))
    assert report["decision"] == ("EVALUATED" if kind == "success" else "INCONCLUSIVE"), report
    assert report["requested_planner_correct"] == (2 if kind == "success" else 0)
    assert comparator.compare_planning_evaluation(opts["output"]) == report
    assert isolation.isolated_run(opts["output"] / "baseline", root / "forbidden-env-read",
        {"mode": "probe", "action": "read", "path": str(root / ".env")}, planner_mode="enabled")["blocked"]
