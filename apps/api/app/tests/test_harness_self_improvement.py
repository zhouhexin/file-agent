"""自主补丁分阶段验收，使用固定假模型保护源码、Gold、权限和预算。"""

from copy import deepcopy
import json
from pathlib import Path
import os

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.routing import service
from app.modules.evidence_harness.improvement import feedback, patches
from app.tests.test_routing_harness import routing, kwargs, factory


def source():
    """读取工作树实际源码，测试不能假定HEAD包含用户未提交修改。"""
    return (Path(__file__).resolve().parents[2] / patches.TARGET).read_text()


def packet(routing, kind="improve"):
    """通过真实规划节点产生有来源的Fake实验，避免直接捏造诊断产物。"""
    options = kwargs(routing)
    service.run_cycle(**options, backend_factory=factory(kind))
    return feedback.build_feedback(options["output"]), options["output"]


def proposal(text, payload, guard="intent_plan.intent not in EVIDENCE_ANSWER_INTENTS"):
    """仅供测试的模型输出，真实生成入口必须调用Provider。"""
    return {"decision": "PROPOSE_PATCH", "feedback_refs": [payload["items"][0]["feedback_id"]],
            "baseline_sha256": patches.source_hash(text), "guard": guard, "reason": "保留正文任务语义",
            "diagnosis": "BUSINESS_FAILURE", "evidence_summary": "任务评分与实际规划记录提示路由问题",
            "hypothesis": "验证明确正文意图是否应跳过通用搜索前置判断", "no_change_reason": None}


def test_feedback_from_verified_trials_and_safe_projection(routing):
    """机器反馈无问题、Gold、密钥和原始身份，且能重建来源。"""
    data, experiment = packet(routing)
    assert len(data["items"]) == 4
    assert any(i["observation"]["model_route"] == "CONTENT_ANSWER" for i in data["items"])
    serialized = json.dumps(data, ensure_ascii=False)
    assert "GOLD_PRIVATE_SENTINEL" not in serialized and "PRIVATE_KEY_SENTINEL" not in serialized
    assert "expected_route" not in serialized and "培训通知" not in serialized
    before = {str(p): p.read_bytes() for p in experiment.rglob("*") if p.is_file()}
    output = experiment.with_name("root-feedback")
    feedback.prepare_feedback(experiment=experiment, output=output)
    assert feedback.load_feedback(output) == data
    assert before == {str(p): p.read_bytes() for p in experiment.rglob("*") if p.is_file()}
    path = output / "root-cause-feedback.json"
    changed = json.loads(path.read_text())
    changed["items"][0]["observation"]["reason"] = "FORGED_REASON"
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="CHANGED"):
        feedback.load_feedback(output)


def test_model_failures_not_proof_of_ineffective_prompt(routing):
    """全部模型失败后的确定性结果照常记录，但策略不具备有效比较依据。"""
    data, _ = packet(routing, "planner_timeout")
    assert all(i["observation"]["reason"] == "MODEL_CALL_FAILED" for i in data["items"])
    assert all(i["observation"]["fallback"] is True for i in data["items"])
    assert all(not i["observation"]["strategy_evaluable"] for i in data["items"])


def test_legacy_missing_trace_and_normalized_intents():
    """老数据缺记录就说明缺失，枚举名字不同但含义相同不报覆盖。"""
    result = {"plan": {"intent": "EVIDENCE_DISCOVERY", "tool_plan": {"slots": {
        "llm_intent_plan": {"intent": "ANSWER_QUESTION_FROM_FILES"}}}}}
    row = feedback.summarize_trial(result, [{"status": "COMPLETED"}])
    assert row["reason"] == "NO_OVERRIDE_OBSERVED"
    assert row["missing_information"] == ["RULE_TRACE_NOT_RECORDED"]
    assert feedback.summarize_trial({}, [])["reason"] == "UNKNOWN"


def test_patch_changes_only_condition_and_preserves_original(routing):
    """补丁确实形成源码差异，但不应用到当前项目，也不碰其他分支。"""
    data, _ = packet(routing)
    text = source()
    output = patches.materialize(text, proposal(text, data), data)
    assert output["stage"] == "VALIDATED" and "+++ b/" + patches.TARGET in output["patch"]
    assert source() == text and output["source"] != text
    assert output["candidate_sha256"] == patches.source_hash(output["source"])


@pytest.mark.parametrize("guard", ["__import__('os').system('id')", "intent_plan.__class__",
    "intent_plan.intent == 'R002'", "intent_plan.intent == '273cc59f-91fe-4a63-8196-95aba5233504'",
    "attachments[0]", "open('/etc/passwd')", "any(attachments)"])
def test_patch_rejects_execution_and_sample_literals(routing, guard):
    """任意调用、动态对象访问、样本特例不能进入候选代码。"""
    data, _ = packet(routing)
    with pytest.raises(ValueError):
        patches.materialize(source(), proposal(source(), data, guard), data)


def test_patch_binding_and_no_change(routing):
    """错误基线或反馈必须拒绝；NO_CHANGE不夹带补丁。"""
    data, _ = packet(routing)
    raw = proposal(source(), data)
    raw["baseline_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        patches.materialize(source(), raw, data)
    raw = proposal(source(), data)
    raw.update(decision="NO_CHANGE", guard=None, hypothesis=None, no_change_reason="INSUFFICIENT_EVIDENCE")
    assert patches.materialize(source(), raw, data)["stage"] == "NO_CHANGE"
    raw["path"] = "/etc/passwd"
    with pytest.raises(ValueError):
        patches.materialize(source(), raw, data)


@pytest.fixture
def self_input(routing):
    """正文含列出并无附件，复现模型正确而程序前置规则覆盖的问题。"""
    from app.modules.evidence_harness.routing import dataset
    inputs = json.loads(routing.inputs.read_text())
    inputs["cases"][0]["question"] = "只根据培训通知，列出培训对象和时间，并引用原文。"
    routing.inputs.write_text(json.dumps(inputs, ensure_ascii=False))
    routing.dataset = routing.dataset.with_name("self-cases.json")
    dataset.freeze(inputs_path=routing.inputs, gold_path=routing.gold, output=routing.dataset)
    _, experiment = packet(routing)
    routing.feedback = experiment.with_name("machine-feedback")
    feedback.prepare_feedback(experiment=experiment, output=routing.feedback)
    return routing


def test_default_check_no_files_no_model_and_source_binding(self_input):
    """默认检查不能提前开始付费实验，也不能混用其他数据集。"""
    from app.modules.evidence_harness.improvement import service as improvement
    from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
    output = self_input.root / "experiments/harness-v1/self-check"
    result = improvement.check_cycle(feedback_dir=self_input.feedback, dataset_path=self_input.dataset,
        output=output, settings=self_input.settings, authorization=ImprovementAuthorization(max_rounds=2), source="fake")
    assert result["model_calls"] == 0 and result["planned_request_upper_bound"] == 26
    assert not output.exists()
    with pytest.raises(ValueError):
        improvement.check_cycle(feedback_dir=self_input.feedback, dataset_path=self_input.dataset,
            output=output, settings=self_input.settings, authorization=ImprovementAuthorization(), source="live")


def test_actual_override_rule_is_observed_and_normal_search_not_reported(self_input):
    """通过真实调用栈确认前置规则触发，而不是仅凭两种枚举名称猜根因。"""
    packet = feedback.load_feedback(self_input.feedback)
    overrides = [i for i in packet["items"] if i["observation"]["reason"] == "BACKEND_ROUTE_OVERRIDE"]
    assert overrides
    assert all(i["observation"]["rule_ids"] == ["LEGACY_FILE_SEARCH_PREEMPTION"] for i in overrides)
    assert all(i["observation"]["certainty"] == "OBSERVED" for i in overrides)
    assert all(i["observation"]["reason"] == "NO_OVERRIDE_OBSERVED" for i in packet["items"]
               if i["case_ref"] == digest("N1"))


def test_feedback_separates_both_wrong_from_same_success(self_input):
    """两组相同但都错且无接口错误，不能把NO_GAIN投影成任务正确。"""
    data = feedback.load_feedback(self_input.feedback)
    assert data["schema_version"] == "root-cause-feedback-v2"
    bad = [i for i in data["items"] if i["case_ref"] == digest("C1")]
    good = [i for i in data["items"] if i["case_ref"] == digest("N1")]
    assert len(bad) == len(good) == 2
    assert all(i["task_outcome"] == "INCORRECT" and i["execution_state"] == "COMPLETED" for i in bad)
    assert all(i["task_outcome"] == "CORRECT" for i in good)
    assert data["source_comparison"]["decision"] == "NO_GAIN"
    assert all(i["change_kind"] == "PROMPT_STRATEGY" for i in data["items"])
    assert all(not i["error_categories"] for i in data["items"])
    assert "expected_route" not in json.dumps(data)


def test_old_feedback_is_verified_and_upgraded_without_writes(routing):
    """保留旧v1字节及引用，建议请求从同一来源重算v2，不能原地迁移或信缓存评分。"""
    from app.modules.evidence_evaluation.frozen_inputs import write_new_json
    data, experiment = packet(routing)
    old = feedback.build_feedback(experiment, version="root-cause-feedback-v1")
    output = experiment.with_name("old-feedback")
    write_new_json(output / "root-cause-feedback.json", old)
    write_new_json(output / "source.json", {"experiment": str(experiment.relative_to(routing.root)),
                                           "feedback_ref": digest(old)})
    before = {str(p): p.read_bytes() for p in output.glob("*.json")}
    assert feedback.load_feedback(output) == old
    (experiment / "comparison.json").write_text('{"decision":"IMPROVED"}')
    assert feedback.load_advisor_feedback(output) == data
    assert before == {str(p): p.read_bytes() for p in output.glob("*.json")}


def test_unrun_and_failed_execution_do_not_become_business_verdicts():
    """没有任务输出时不评分；已有最终输出的对错与模型报错分别记录。"""
    assert feedback.task_signal({"status": "NOT_RUN", "correct": False}, {})["task_outcome"] == "UNSCORED"
    signal = feedback.task_signal({"status": "COMPLETED", "correct": True}, {"failed_calls": 1})
    assert signal["task_outcome"] == "CORRECT" and signal["execution_state"] == "MODEL_FAILED"


@pytest.mark.parametrize("reason", ["INSUFFICIENT_EVIDENCE", "OUTSIDE_ALLOWED_SCOPE"])
def test_known_task_errors_do_not_force_patch_generation(self_input, reason):
    """已知错误仍可因证据不足或修改范围限制而合法不改，不能强制产生补丁。"""
    data = feedback.load_feedback(self_input.feedback)
    raw = proposal(source(), data)
    raw.update(decision="NO_CHANGE", guard=None, hypothesis=None, no_change_reason=reason)
    assert patches.materialize(source(), raw, data)["stage"] == "NO_CHANGE"


def test_no_problem_claim_requires_no_known_task_error(self_input):
    """两组都错时声称没有问题会明确拒绝；两组都对仍可正常不修改。"""
    data = feedback.load_feedback(self_input.feedback)
    raw = proposal(source(), data)
    raw.update(decision="NO_CHANGE", guard=None, hypothesis=None, diagnosis="NO_ACTIONABLE_FAILURE",
               no_change_reason="NO_ACTIONABLE_FAILURE", reason="两组结果相同且没有接口异常")
    with pytest.raises(ValueError, match="NO_CHANGE_CONTRADICTS_TASK_FAILURE"):
        patches.materialize(source(), raw, data)
    clean = deepcopy(data)
    for i in clean["items"]:
        i["task_outcome"] = "CORRECT"
    assert patches.materialize(source(), raw, clean)["stage"] == "NO_CHANGE"


@pytest.mark.parametrize("kind,evaluable,verdict,valid", [("PROMPT_STRATEGY", True, "NO_GAIN", False),
    ("CODE_PATCH", False, "INCONCLUSIVE", False), ("CODE_PATCH", True, "NO_GAIN", True),
    ("CODE_PATCH", True, "REGRESSION", True)])
def test_verified_no_gain_requires_actual_code_history(self_input, kind, evaluable, verdict, valid):
    """提示无收益、未执行或失败不能当作代码候选已证伪，只有实际有效历史可引用。"""
    data = feedback.load_feedback(self_input.feedback)
    raw = proposal(source(), data)
    ref = digest("verified-attempt")
    raw.update(decision="NO_CHANGE", guard=None, hypothesis=None, no_change_reason="VERIFIED_NO_GAIN",
               no_change_attempt_ref=ref)
    history = [{"change_kind": kind, "strategy_evaluable": evaluable, "decision": verdict, "patch_ref": ref}]
    if valid:
        assert patches.materialize(source(), raw, data, history=history)["stage"] == "NO_CHANGE"
    else:
        with pytest.raises(ValueError, match="NO_CHANGE_VERIFIED_ATTEMPT_MISSING"):
            patches.materialize(source(), raw, data, history=history)


@pytest.mark.parametrize("field,value", [("hypothesis", ""), ("evidence_summary", " "),
                                        ("no_change_reason", "INSUFFICIENT_EVIDENCE")])
def test_proposal_schema_rejects_incomplete_patch_explanation(routing, field, value):
    """新协议必须真正记录依据和可测试假设，不能只输出旧式补丁。"""
    data, _ = packet(routing)
    raw = proposal(source(), data)
    raw[field] = value
    with pytest.raises(ValueError):
        patches.materialize(source(), raw, data)


def test_duplicate_guard_forms_have_same_patch_identity(routing):
    """同一补丁不能通过加括号变成新候选重复消耗预算。"""
    data, _ = packet(routing)
    left = patches.materialize(source(), proposal(source(), data, "True"), data)
    right = patches.materialize(source(), proposal(source(), data, "(True)"), data)
    assert left["patch_ref"] == right["patch_ref"]


def test_execution_failure_is_feedback_without_gold(self_input, tmp_path):
    """未进入业务题的候选检查失败也应传回下一轮，不能丢掉这类失败。"""
    from app.modules.evidence_harness.improvement.service import next_feedback
    directory = tmp_path / "round"
    directory.mkdir()
    (directory / "round-summary.json").write_text('{"stage":"ISOLATION_FAILED","error_code":"TypeError"}')
    initial = feedback.load_feedback(self_input.feedback)
    updated = next_feedback(initial, directory, 1, [])
    assert updated["items"][-1]["observation"]["reason"] == "CANDIDATE_EXECUTION_FAILED"
    assert updated["items"][-1]["observation"]["stage"] == "ISOLATION_FAILED"
    assert "expected_route" not in json.dumps(updated)


@pytest.mark.parametrize("kind,expected", [("no_change", "NO_CHANGE"), ("invalid", "REJECTED"),
                                          ("timeout", "OPTIMIZER_FAILED"), ("interrupt", "INTERRUPTED")])
def test_standalone_generator_preserves_current_source_and_quota(self_input, kind, expected):
    """独立建议器失败和不修改时不能运行候选，真实发送额度与授权一致。"""
    from app.modules.evidence_harness.improvement import service as improvement
    from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
    output = self_input.root / f"experiments/harness-v1/propose-{kind}"
    seen, original = [], source()
    result = improvement.propose_only(feedback_dir=self_input.feedback, output=output,
        settings=self_input.settings, authorization=ImprovementAuthorization(advisor_max_output_tokens=6000),
        source="fake", backend=self_backend(kind, seen)("ADVISOR", 1, None, None, None))
    assert result["stage"] == expected and result["requests_used"] == 1
    assert not result["candidate_executed"] and result["model_calls"] == 0
    assert seen[0][2]["max_output_tokens"] == 6000
    assert source() == original and not (output / "candidate").exists()


@pytest.mark.parametrize("kind,reason", [("no_change", "NO_CHANGE"), ("invalid", "REJECTED"),
                                       ("timeout", "INFRASTRUCTURE_FAILURE_LIMIT"), ("interrupt", "USER_INTERRUPTED")])
def test_cycle_early_stops_have_no_candidate_requests(self_input, monkeypatch, kind, reason):
    """此测试只替换OS探针，不运行候选；早停不能偷偷请求规划或覆盖旧目录。"""
    from app.modules.evidence_harness.improvement import service as improvement, comparator
    from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
    monkeypatch.setattr(improvement, "probe_sandbox", lambda *args, **kwargs: {"fake_probe": True})
    output = self_input.root / f"experiments/harness-v1/early-{kind}"
    seen = []
    options = dict(feedback_dir=self_input.feedback, dataset_path=self_input.dataset,
                   output=output, settings=self_input.settings, source="fake",
                   authorization=ImprovementAuthorization(max_infrastructure_failures=1),
                   backend_factory=self_backend(kind, seen))
    result = improvement.run_cycle(**options)
    assert result["stop_reason"] == reason and result["requests_used"] == 1
    assert len(seen) == 1 and seen[0][0] == "ADVISOR"
    assert comparator.compare(output)["decision"] != "INVALID_EXPERIMENT"
    with pytest.raises(FileExistsError):
        improvement.run_cycle(**options)


def test_misread_no_change_is_rejected_once_without_forcing_patch(self_input, monkeypatch):
    """复现本次真实误读：明确拒绝矛盾依据，仍只调用一次建议，不强改为补丁或重试。"""
    from app.modules.evidence_harness.improvement import service as improvement, comparator
    from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
    monkeypatch.setattr(improvement, "probe_sandbox", lambda *args, **kwargs: {"fake_probe": True})
    output = self_input.root / "experiments/harness-v1/misread"
    seen = []
    result = improvement.run_cycle(feedback_dir=self_input.feedback, dataset_path=self_input.dataset, output=output,
        settings=self_input.settings, authorization=ImprovementAuthorization(), source="fake",
        backend_factory=self_backend("misread", seen))
    assert result["decision"] == "NOT_RUN" and result["requests_used"] == len(seen) == 1
    assert result["rounds"][0]["rejection_code"] == "NO_CHANGE_CONTRADICTS_TASK_FAILURE"
    assert not (output / "rounds/0001/candidate.patch").exists()
    assert comparator.compare(output) == result


def test_comparator_rebuilds_score_and_history_after_tamper(self_input, monkeypatch):
    """即使重写完整性清单，反馈中的对错和伪造历史也必须能从原始结果独立核验。"""
    from app.modules.evidence_harness.improvement import service as improvement, comparator, isolation
    from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
    monkeypatch.setattr(improvement, "probe_sandbox", lambda *args, **kwargs: {"fake_probe": True})
    output = self_input.root / "experiments/harness-v1/tampered-judgment"
    improvement.run_cycle(feedback_dir=self_input.feedback, dataset_path=self_input.dataset, output=output,
        settings=self_input.settings, authorization=ImprovementAuthorization(), source="fake",
        backend_factory=self_backend("no_change"))
    path = output / "rounds/0001/feedback.json"
    original = path.read_text()
    modified = json.loads(original)
    modified["items"][0]["task_outcome"] = "CORRECT"
    path.write_text(json.dumps(modified))

    def renew():
        """模拟重新生成外层散列，比较器仍不能信手改的业务评分。"""
        manifest = isolation.file_manifest(output)
        for name in ("integrity.json", "comparison.json", "summary.md"):
            manifest.pop(name, None)
        (output / "integrity.json").write_text(json.dumps(manifest))

    renew()
    assert comparator.compare(output)["decision"] == "INVALID_EXPERIMENT"
    path.write_text(original)
    (output / "rounds/0001/history.json").write_text('{"attempts":[{"decision":"NO_GAIN"}]}')
    renew()
    assert comparator.compare(output)["decision"] == "INVALID_EXPERIMENT"


def test_journal_budget_is_global_and_unknown_usage_stays_unknown(tmp_path):
    """调用前持久化和计数，失败耗额度，下一轮不能重新得到同一份预算。"""
    from app.modules.evidence_harness.improvement.executor import Journal
    from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
    from app.modules.evidence_harness.executor import ExperimentStopped
    from app.modules.llm.client import LLMResponseError
    class Broken:
        """固定失败的Provider，验证失败也计入总预算。"""
        def complete_json_with_usage(self, **kwargs):
            """固定Provider错误，不产生真实请求。"""
            raise LLMResponseError("do not expose")
    journal = Journal(tmp_path, ImprovementAuthorization(max_total_requests=1))
    with pytest.raises(LLMResponseError):
        journal.call(Broken(), role="ADVISOR", round_id=1, system_prompt="fixed", user_payload={})
    with pytest.raises(ExperimentStopped):
        journal.call(Broken(), role="ADVISOR", round_id=2, system_prompt="fixed", user_payload={})
    record = json.loads((tmp_path / "calls/0001.finished.json").read_text())
    assert journal.used == 1 and record["input_tokens"] is None and record["output_tokens"] is None


def test_time_budget_prevents_model_dispatch(tmp_path):
    """不足完整调用等待预算时直接停止，不先发请求再发现来不及。"""
    from app.modules.evidence_harness.improvement.executor import Journal
    from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
    from app.modules.evidence_harness.executor import ExperimentStopped
    journal = Journal(tmp_path, ImprovementAuthorization(max_active_seconds=1))
    with pytest.raises(ExperimentStopped):
        journal.call(None, role="ADVISOR", round_id=1, system_prompt="", user_payload={})
    assert journal.used == 0 and not (tmp_path / "calls").exists()


def test_snapshot_excludes_gold_judge_and_secrets(tmp_path):
    """隔离副本不含测试、评分器、脚本、env或私有资料，源码来自实际工作树。"""
    from app.modules.evidence_harness.improvement.isolation import snapshot
    manifest = snapshot(tmp_path / "source")
    assert patches.TARGET in manifest and manifest[patches.TARGET] == patches.source_hash(source())
    assert not any("tests" in p.split("/") or "comparator" in p or p.endswith(".env") for p in manifest)
    assert "app/modules/evidence_harness/improvement/worker.py" in manifest


def test_broker_rejects_prompt_and_input_mutations(self_input):
    """没有凭证的候选也不能借可信代理发送任意Prompt、原文或新增字段。"""
    from app.modules.evidence_harness.improvement.executor import validate_planner_request
    from app.modules.llm.prompts import USER_INTENT_SYSTEM_PROMPT
    from app.modules.llm.schemas import UserIntentPlan
    from app.modules.evidence_harness.routing.dataset import load_dataset
    data = load_dataset(self_input.dataset)
    case = data.inputs.cases[0]
    request = {"type": "MODEL_REQUEST", "system_prompt": USER_INTENT_SYSTEM_PROMPT,
               "user_payload": {"message": case.question, "attachments": [], "context_documents": [],
                   "catalog_snapshot": data.inputs.catalog_snapshot, "output_schema": UserIntentPlan.model_json_schema(),
                   "observation": {}}}
    validate_planner_request(request, case, data.inputs.catalog_snapshot)
    request["user_payload"]["message"] += "EXFILTRATION"
    with pytest.raises(ValueError):
        validate_planner_request(request, case, data.inputs.catalog_snapshot)
    request["system_prompt"] = "REPLACE_SYSTEM"
    with pytest.raises(ValueError):
        validate_planner_request(request, case, data.inputs.catalog_snapshot)


def explicit_test_settings(directory):
    """仅包含隔离worker允许的固定配置，不使用操作者.env或宿主密钥。"""
    return {"database_url": "postgresql://offline:offline@127.0.0.1/file_agent_unit_tests",
            "llm_enabled": True, "adaptive_planner_mode": "legacy", "log_dir": str(directory / "logs")}


def test_worker_settings_do_not_read_env_or_ambient_variables(tmp_path, monkeypatch):
    """模拟上级.env存在且读取必报错，仍返回明确传入的无密钥配置。"""
    from app.core import config
    from app.modules.evidence_harness.improvement.worker import configure_test_settings
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LLM_API_KEY=ENV_PRIVATE_SENTINEL\nADAPTIVE_PLANNER_MODE=enabled\n")
    monkeypatch.setenv("LLM_API_KEY", "AMBIENT_PRIVATE_SENTINEL")
    monkeypatch.setenv("ADAPTIVE_PLANNER_MODE", "enabled")
    original = config.get_settings

    def forbidden():
        """禁止连查找都发生，而不是吞掉PermissionError后碰运气。"""
        raise AssertionError("DOTENV_MUST_NOT_BE_ACCESSED")

    monkeypatch.setattr(config, "find_dotenv_file", forbidden)
    monkeypatch.setattr(config, "load_dotenv_file", forbidden)
    try:
        settings = configure_test_settings(explicit_test_settings(tmp_path))
        assert config.get_settings() is settings
        assert settings.adaptive_planner_mode == "legacy" and settings.llm_api_key == ""
        assert settings.log_dir == str(tmp_path / "logs")
    finally:
        # 测试在宿主调用bootstrap，必须主动恢复，不能影响公共fixture清理。
        config.get_settings = original


@pytest.mark.parametrize("change", [None, {"llm_api_key": "FORBIDDEN"}, {"database_url": "postgresql://real/production"},
    {"log_dir": "/private/tmp/outside-logs"}, {"adaptive_planner_mode": "shadow"}, {"llm_enabled": 1},
    {"extra": "FORBIDDEN"}])
def test_worker_rejects_missing_or_untrusted_settings(tmp_path, monkeypatch, change):
    """独立进程不能通过配置引入真实库、密钥、任意日志目录或未声明字段。"""
    from app.core import config
    from app.modules.evidence_harness.improvement.worker import configure_test_settings
    monkeypatch.chdir(tmp_path)
    original = config.get_settings
    raw = None if change is None else {**explicit_test_settings(tmp_path), **change}
    with pytest.raises(ValueError, match="ISOLATED_TEST_SETTINGS_INVALID"):
        configure_test_settings(raw)
    assert config.get_settings is original


def self_backend(kind="two_rounds", seen=None):
    """只有模型输出被替换，隔离验收必须运行真实候选源码及真实规划节点。"""
    seen = [] if seen is None else seen
    def build(role, round_id, arm, case, repetition):
        """按轮次构造确定性输出，实际源码执行仍交给系统隔离。"""
        class Client:
            """保留请求协议的假模型，不读取人工答案。"""
            def complete_json_with_usage(self, **request):
                """记录实际输入，第二轮必须获得第一轮反馈和结论。"""
                seen.append((role, round_id, deepcopy(request)))
                if kind == "interrupt":
                    raise KeyboardInterrupt
                if kind == "timeout" or kind == "planner_fail" and role == "PLANNER":
                    from app.modules.llm.client import LLMResponseError
                    raise LLMResponseError("private")
                payload = request["user_payload"]
                if role == "ADVISOR":
                    guard = "True" if round_id == 1 and kind == "two_rounds" else "intent_plan.intent not in EVIDENCE_ANSWER_INTENTS"
                    raw = {"decision": "PROPOSE_PATCH", "baseline_sha256": payload["source_context"]["baseline_sha256"],
                           "feedback_refs": [payload["feedback"]["items"][0]["feedback_id"]],
                           "guard": guard, "reason": "根据路由覆盖反馈增加语义保护",
                           "diagnosis": "BUSINESS_FAILURE", "evidence_summary": "开发集任务错误且源码存在前置搜索条件",
                           "hypothesis": "验证语义保护能否减少正文路由错误", "no_change_reason": None}
                    if kind == "no_change":
                        raw.update(decision="NO_CHANGE", guard=None, hypothesis=None,
                                   no_change_reason="INSUFFICIENT_EVIDENCE", diagnosis="INSUFFICIENT_EVIDENCE")
                    if kind == "misread":
                        raw.update(decision="NO_CHANGE", guard=None, hypothesis=None, diagnosis="NO_ACTIONABLE_FAILURE",
                                   no_change_reason="NO_ACTIONABLE_FAILURE", reason="两组相同且接口没有报错")
                    if kind == "invalid":
                        raw["guard"] = "open('/etc/passwd')"
                    return raw, {"input_tokens": 100, "output_tokens": 30}
                content = "引用原文" in payload["message"] or kind == "regress" and arm == "candidate"
                return {"intent": "ANSWER_QUESTION_FROM_FILES" if content else "SEARCH_FILES",
                        "user_goal": payload["message"], "managed_query": "培训通知",
                        "required_capabilities": ["evidence_answer"] if content else ["file_search"]}, {
                        "input_tokens": 20, "output_tokens": 10}
        return Client()
    return build


OS_ACCEPTANCE = os.environ.get("HARNESS_OS_ACCEPTANCE") == "1"


@pytest.mark.skipif(not OS_ACCEPTANCE, reason="完整OS验收由固定本机验收脚本启动，普通pytest不绕过隔离")
def test_os_isolated_two_round_code_improvement(self_input):
    """真实OS隔离和真实源码补丁两轮试跑，只有Provider是Fake，不能冒充真实模型改善。"""
    from app.modules.evidence_harness.improvement import service as improvement, comparator
    from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
    output = self_input.root / "experiments/harness-v1/self-two-rounds"
    # 真正复现部署结构：隔离临时目录的上级有.env，但该文件不允许子进程读取。
    ancestor_env = self_input.root / ".env"
    ancestor_env.write_text("LLM_API_KEY=UNREADABLE_ENV_SENTINEL\nDATABASE_URL=postgresql://wrong/production\n")
    ancestor_bytes = ancestor_env.read_bytes()
    original = source()
    seen = []
    report = improvement.run_cycle(feedback_dir=self_input.feedback, dataset_path=self_input.dataset,
        output=output, settings=self_input.settings, source="fake", authorization=ImprovementAuthorization(
            max_rounds=2, max_total_requests=26), backend_factory=self_backend(seen=seen))
    assert report["decision"] == "IMPROVED", [report, [json.loads(p.read_text())["result"]
        for p in output.glob("rounds/0001/trials/*.json")]]
    assert [r["decision"] for r in report["rounds"]] == ["NO_GAIN", "IMPROVED"]
    assert report["requests_used"] == len(seen) == 10
    assert report["candidate_retained"] and not report["automatically_adopted"]
    assert source() == original
    second = next(r[2]["user_payload"] for r in seen if r[:2] == ("ADVISOR", 2))
    first = next(r[2]["user_payload"] for r in seen if r[:2] == ("ADVISOR", 1))
    assert len(second["feedback"]["items"]) > len(first["feedback"]["items"])
    assert second["history"][0]["decision"] == "NO_GAIN"
    assert second["history"][0]["change_kind"] == "CODE_PATCH"
    assert second["history"][0]["task_counts"]["candidate"]["correct"] == 1
    code_feedback = [i for i in second["feedback"]["items"] if i.get("change_kind") == "CODE_PATCH"]
    assert len(code_feedback) == 4 and sum(i["task_outcome"] == "INCORRECT" for i in code_feedback) == 2
    for _, _, request in seen:
        text = json.dumps(request)
        assert "GOLD_PRIVATE_SENTINEL" not in text and "PRIVATE_KEY_SENTINEL" not in text
        assert "expected_route" not in text
    assert comparator.compare(output) == report
    from app.modules.evidence_harness.improvement.isolation import isolated_run
    assert isolated_run(output / "baseline", self_input.root / "blocked-env-probe",
        {"mode": "probe", "action": "read", "path": str(ancestor_env)})["blocked"]
    assert ancestor_env.read_bytes() == ancestor_bytes
    # 缓存分数被改不能影响重新计算；实际源码被改则实验失效。
    (output / "comparison.json").write_text('{"decision":"REGRESSION"}')
    assert comparator.compare(output) == report
    target = output / "rounds/0002/candidate" / patches.TARGET
    target.chmod(0o600)
    target.write_text(target.read_text() + "\n# tamper\n")
    assert comparator.compare(output)["decision"] == "INVALID_EXPERIMENT"


@pytest.mark.skipif(not OS_ACCEPTANCE, reason="真实OS隔离验收使用固定本机脚本")
@pytest.mark.parametrize("kind,budget,expected,used", [("planner_fail", 19, "INCONCLUSIVE", 5),
    ("improve", 2, "INCONCLUSIVE", 2), ("regress", 19, "REGRESSION", 5)])
def test_os_failure_partial_budget_and_regression(self_input, kind, budget, expected, used):
    """真实隔离中失败不算无收益，预算包含建议，正常搜索退步不能被正文改善掩盖。"""
    from app.modules.evidence_harness.improvement import service as improvement, comparator
    from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
    output = self_input.root / f"experiments/harness-v1/os-{kind}-{budget}"
    report = improvement.run_cycle(feedback_dir=self_input.feedback, dataset_path=self_input.dataset,
        output=output, settings=self_input.settings, source="fake", authorization=ImprovementAuthorization(
            max_total_requests=budget), backend_factory=self_backend(kind))
    assert report["decision"] == expected, report
    assert report["requests_used"] == used and not report["candidate_retained"]
    assert comparator.compare(output) == report
    if kind == "planner_fail":
        assert report["failed_model_calls"] == 4 and report["unknown_token_calls"] == 4
    if budget == 2:
        assert report["stop_reason"] == "REQUEST_BUDGET_EXHAUSTED"
        assert report["rounds"][0]["arms"]["candidate"]["planned"] == 2
