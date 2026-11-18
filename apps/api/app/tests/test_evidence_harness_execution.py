"""第三步验收：真实评估器配确定性模型替身，验证预算、Profile、逐轮Gold与中断。"""

from copy import deepcopy
import json
import time

import pytest

from app.modules.evidence_evaluation import frozen_inputs
from app.modules.evidence_evaluation.frozen_inputs import digest, runtime_config
from app.modules.evidence_harness import feedback, optimizer, executor, comparator, transport
from app.modules.evidence_harness.schemas import ExecutionAuthorization, ExperimentProfile
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic
from app.scripts.evaluate_evidence import EvaluationCase
from app.tests.test_evidence_answer import _settings
from app.tests.test_evidence_evaluation import package, response
from app.tests.test_evidence_harness_feedback import _failed, _save
from app.tests.test_evidence_harness_optimizer import FakeAdvisor


@pytest.fixture
def execution(tmp_path, monkeypatch):
    """三题合成冻结输入，显式Fake建议；不读取用户材料或真实模型配置。"""
    root = tmp_path / "test"
    monkeypatch.setattr(frozen_inputs, "TEST_ROOT", root)
    monkeypatch.setattr(feedback, "TEST_ROOT", root)
    directory = root / "datasets/frozen-v1";directory.mkdir(parents=True)
    settings = _settings().model_copy(update={"evidence_evaluation_max_output_tokens": 6000,
        "evidence_evaluation_timeout_seconds": 60})
    config = runtime_config(settings)
    cases = []
    for cid in ("R001", "R016", "R034"):
        pkg = package(question=cid, quotes=["可定位的真实测试原文。"])
        versions = {e.evidence_id: {"status": "VERIFIED", "current_version_id": e.document_version_id} for e in pkg.evidence_items}
        sha = digest({"package": pkg.model_dump(mode="json"), "versions": versions,
                      "context": {"original_question": cid, "temporal_focus": "HISTORICAL"}})
        cases.append(EvaluationCase(case_id=cid, group="MISSING" if cid == "R016" else "NORMAL",
            package=pkg, versions=versions, temporal_focus="HISTORICAL", original_question=cid,
            expected_issues=[{"type": "MISSING", "evidence_ids": []}] if cid == "R016" else [],
            expected_explanation="GOLD_PRIVATE_SENTINEL", input_sha256=sha, capture_config=config, source="synthetic"))
    serialized = [c.model_dump(mode="json") for c in cases]
    annotations = [{"case_id": c.case_id, "expected_issues": [x.model_dump() for x in c.expected_issues]} for c in cases]
    data = {"cases": serialized, "annotations": annotations,
            "manifest": {"cases_sha256": digest(serialized), "annotation_sha256": digest(annotations)}}
    dataset = directory / "cases.json";_save(dataset, data)
    raw = {"mode": "live", "evaluator_version": "evidence-eval-v1", "sample_count": 3, "repetitions": 3,
        "dataset_sha256": digest(serialized), "dataset_manifest": data["manifest"],
        "runtime_config": config, "runtime_config_sha256": digest(config), "cases": [
            {"case_id": c.case_id, "input_sha256": c.input_sha256, "rounds": [
                {"repetition": n, "result": _failed("timeout")} for n in range(1, 4)]} for c in cases]}
    baseline = directory / "baseline.json";_save(baseline, raw)
    prepared = root / "experiments/harness-v1/feedback"
    feedback.prepare_feedback(dataset_path=dataset, baseline_path=baseline, history_paths=[], output=prepared)
    packet = optimizer.load_feedback(prepared)
    proposal = root / "experiments/harness-v1/proposal"
    optimizer.propose(feedback_dir=prepared, output=proposal, source="fake", client=FakeAdvisor({
        "decision": "PROPOSE_EXPERIMENT", "target": "evaluation_runtime",
        "feedback_refs": [packet.reports[0].trials[-1].feedback_id],
        "change": {"field": "timeout_seconds", "from": 60, "to": 120}, "hypothesis": "测试等待预算假设。"}))
    return dict(root=root, dataset=dataset, settings=settings, feedback_dir=prepared, proposal_dir=proposal,
                output=root / "experiments/harness-v1/run", cases=cases)


def _run(execution, scenario="improve", **kwargs):
    """预置四种实际结果，通过原SemanticChecker计算标签，不把Gold送给Fake。"""
    calls = []
    on_call = kwargs.pop("on_call", None)

    def factory(arm, profile, case, repetition):
        class Client:
            def complete_json_with_usage(self, **payload):
                calls.append((arm, profile, case.case_id, repetition, deepcopy(payload)))
                if on_call:
                    on_call()
                if scenario == "interrupt" and len(calls) == 7:
                    raise KeyboardInterrupt
                if scenario == "all_fail" or (case.case_id == "R034" and (arm == "control" or scenario == "no_gain")):
                    raise LLMResponseError("not logged", diagnostic=make_diagnostic(category="TIMEOUT", stage="LLM_REQUEST"))
                if scenario == "partial_gain" and case.case_id == "R034" and repetition > 1:
                    raise LLMResponseError("not logged", diagnostic=make_diagnostic(category="TIMEOUT", stage="LLM_REQUEST"))
                if scenario == "regress" and arm == "candidate" and case.case_id == "R001":
                    return response("UNSUPPORTED"), {"input_tokens": 20, "output_tokens": 10}
                raw = response("UNSUPPORTED" if case.case_id == "R016" else "SUPPORTED")
                usage = {} if scenario == "unknown_usage" else {"input_tokens": 20, "output_tokens": 10}
                return raw, usage
        return Client()

    result = executor.run_experiment(feedback_dir=execution["feedback_dir"], proposal_dir=execution["proposal_dir"],
        output=execution["output"], settings=execution["settings"], source="fake", client_factory=factory,
        authorization=kwargs.pop("authorization", ExecutionAuthorization()), **kwargs)
    return result, calls, comparator.compare_experiment(execution["output"])


@pytest.mark.parametrize("scenario,decision", [("improve", "IMPROVED"), ("no_gain", "NO_GAIN"),
                                              ("regress", "REGRESSION"), ("all_fail", "NO_GAIN")])
def test_independent_gold_scores_improvement_no_gain_regression_and_repeated_failure(execution, scenario, decision):
    """失败仍在9次分母中，正常题退步不能被另一题改善抵消，三次失败不算通过。"""
    result, calls, comparison = _run(execution, scenario)
    assert result["stage"] == "COMPLETED" and comparison["decision"] == decision
    assert len(calls) == 18 and result["total_requests_used"] == 19
    assert comparison["arms"]["control"]["denominator"] == 9
    assert comparison["arms"]["candidate"]["denominator"] == 9
    assert not result["candidate_automatically_adopted"] and not comparison["live_review_candidate"]
    if scenario == "improve":
        assert comparison["arms"]["control"]["strict_matches"] == 6
        assert comparison["arms"]["candidate"]["strict_matches"] == 9 and comparison["meets_review_criteria"]
    if scenario == "regress":
        assert "R001" in comparison["regressed_cases"]
    if scenario == "all_fail":
        assert comparison["arms"]["candidate"]["strict_matches"] == 0
        assert not comparison["meets_review_criteria"]


def test_actual_profiles_and_no_gold_in_model_payload(execution):
    """120秒传到实际客户端工厂，Token额度不变；Gold和解释不进入评估请求。"""
    before = execution["settings"].model_dump()
    result, calls, comparison = _run(execution)
    assert {p.timeout_seconds for arm, p, *_ in calls if arm == "control"} == {60}
    assert {p.timeout_seconds for arm, p, *_ in calls if arm == "candidate"} == {120}
    assert all(p.max_output_tokens == 6000 for _, p, *_ in calls)
    assert all(call[-1]["max_output_tokens"] == 6000 for call in calls)
    assert "GOLD_PRIVATE_SENTINEL" not in json.dumps([call[-1] for call in calls])
    assert "expected_issues" not in json.dumps([call[-1] for call in calls])
    assert before == execution["settings"].model_dump()
    assert comparison["arms"]["candidate"]["per_class"]["MISSING"]["recall"] == 1
    assert comparison["arms"]["candidate"]["per_class"]["OUTDATED"]["recall"] is None
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in execution["output"].rglob("*") if p.is_dir())
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in execution["output"].rglob("*.json"))


def test_total_requests_include_prior_advisor_and_stop_before_send(execution):
    """总额度3次包含此前建议1次；第3个评估请求必须在发送前被拦截。"""
    result, calls, comparison = _run(execution, authorization=ExecutionAuthorization(max_total_requests=3))
    assert len(calls) == 2 and result["total_requests_used"] == 3
    assert result["stage"] == "STOPPED" and comparison["decision"] == "INCONCLUSIVE"
    assert comparison["stop_reason"] == "REQUEST_BUDGET_EXHAUSTED"
    assert comparison["arms"]["control"]["strict_matches"] == 2
    assert comparison["arms"]["candidate"]["strict_matches"] == 0


def test_wall_budget_stops_before_first_request_if_full_timeout_cannot_fit(execution):
    """不把实际120秒静默裁成更小值，剩余预算不够完整请求就停止。"""
    result, calls, comparison = _run(execution, authorization=ExecutionAuthorization(max_active_seconds=1))
    assert calls == [] and result["total_requests_used"] == 1
    assert comparison["decision"] == "INCONCLUSIVE"
    assert comparison["stop_reason"] == "ACTIVE_TIME_BUDGET_EXHAUSTED"


def test_keyboard_interrupt_retains_prior_rounds_and_unknown_active_call(execution):
    """第7次评估中断时保留前6轮和该次调用开始/中断记录，拒绝自动恢复。"""
    result, calls, comparison = _run(execution, "interrupt")
    assert result["stage"] == "INTERRUPTED" and len(calls) == 7
    assert result["total_requests_used"] == 8 and comparison["decision"] == "INCONCLUSIVE"
    assert len(list((execution["output"] / "control/rounds").glob("*.json"))) == 6
    assert comparison["arms"]["control"]["unknown_usage_requests"] == 1
    with pytest.raises(FileExistsError):
        _run(execution)


def test_unknown_usage_is_not_zero_cost_or_fake_savings(execution):
    """模型返回缺usage时完成率可比较，但不能声称总费用已知或更省钱。"""
    _, _, comparison = _run(execution, "unknown_usage")
    assert comparison["decision"] == "IMPROVED"
    assert not comparison["cost_comparable"] and not comparison["total_token_usage_complete"]
    assert comparison["arms"]["candidate"]["unknown_usage_requests"] == 9


def test_partial_improvement_is_not_ready_for_review(execution):
    """7/9虽比6/9好，也不能跨过9/9且原题不退步的复核门槛。"""
    _, _, comparison = _run(execution, "partial_gain")
    assert comparison["decision"] == "IMPROVED"
    assert comparison["arms"]["candidate"]["strict_matches"] == 7
    assert not comparison["meets_review_criteria"]


def test_active_clock_exhaustion_prevents_following_request(execution):
    """一次调用返回时活跃时间已耗尽，保留这轮并阻止下一次请求。"""
    clock = [0.0]
    def elapsed():
        clock[0] = 1801.0
    result, calls, comparison = _run(execution, clock=lambda: clock[0], on_call=elapsed)
    assert len(calls) == 1 and result["total_requests_used"] == 2
    assert comparison["decision"] == "INCONCLUSIVE"
    assert comparison["stop_reason"] == "ACTIVE_TIME_BUDGET_EXHAUSTED"


def test_compare_is_read_only_and_ignores_cached_model_claims(execution):
    """独立比较只重算Gold和逐轮记录，不覆盖产物或采用已有摘要自述。"""
    _run(execution)
    out = execution["output"]
    original = {str(p): p.read_bytes() for p in out.rglob("*.json")}
    result = comparator.compare_experiment(out)
    assert result["arms"]["candidate"]["strict_matches"] == 9
    assert original == {str(p): p.read_bytes() for p in out.rglob("*.json")}


def test_same_proposal_cannot_renew_budget_with_another_output_directory(execution):
    """同一建议禁止并发或换目录再领总预算，中断后也不能隐式重复已付费请求。"""
    _run(execution, "interrupt")
    execution["output"] = execution["output"].with_name("another-run")
    with pytest.raises(ValueError, match="ALREADY_RESERVED"):
        _run(execution)
    assert not execution["output"].exists()


def test_cli_run_without_live_only_checks(execution, monkeypatch, capsys):
    """默认run仅显示18次新请求与已有建议1次，不构造实际执行或请求进程。"""
    import sys
    from functools import lru_cache
    from app.scripts import evidence_harness
    from app.core import config
    def check(**kwargs):
        profile = ExperimentProfile(max_input_chars=48000, max_output_tokens=6000,
                                    max_calls=3, timeout_seconds=60, max_retries=0)
        return {"profiles": {"control": profile, "candidate": profile}}
    def forbidden(**kwargs):
        pytest.fail("Missing --live must not execute")
    monkeypatch.setattr(config, "get_settings", lru_cache()(lambda: execution["settings"]))
    monkeypatch.setattr(evidence_harness, "check_execution", check)
    monkeypatch.setattr(evidence_harness, "run_experiment", forbidden)
    monkeypatch.setattr(sys, "argv", ["harness", "run", "--feedback", str(execution["feedback_dir"]),
        "--proposal", str(execution["proposal_dir"]), "--output", str(execution["output"])])
    evidence_harness.main()
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "CHECK_PASSED" and result["new_model_calls"] == 0
    assert result["planned_new_requests"] == 18 and not execution["output"].exists()


def test_no_change_or_rejected_suggestion_never_starts_execution(execution):
    """没有可执行候选时即止步，不将NO_CHANGE当成默认参数实验。"""
    path = execution["proposal_dir"] / "manifest.json"
    data = json.loads(path.read_text());data["stage"] = "NO_CHANGE";_save(path, data)
    calls = []
    def forbidden(*args):
        calls.append(1)
        pytest.fail("Rejected suggestions must not execute")
    with pytest.raises(ValueError, match="PROPOSAL_NOT_EXECUTABLE"):
        executor.run_experiment(feedback_dir=execution["feedback_dir"], proposal_dir=execution["proposal_dir"],
            output=execution["output"], settings=execution["settings"], source="fake",
            authorization=ExecutionAuthorization(), client_factory=forbidden)
    assert not calls and not execution["output"].exists()


@pytest.mark.parametrize("target", ["experiment", "round", "gold", "integrity"])
def test_tampering_is_invalid_instead_of_rescored_as_improvement(execution, target):
    """不能修改Profile、某轮结果、Gold或完整性列表来让候选过关。"""
    _run(execution)
    out = execution["output"]
    if target == "gold":
        path = execution["dataset"];data = json.loads(path.read_text());data["cases"][0]["expected_explanation"] = "changed"
    elif target == "round":
        path = next((out / "candidate/rounds").glob("*.json"));data = json.loads(path.read_text());data["result"]["issues"] = []
        data["input_sha256"] = "0" * 64
    elif target == "experiment":
        path = out / "experiment.json";data = json.loads(path.read_text());data["profiles"]["candidate"]["timeout_seconds"] = 60
    else:
        path = out / "integrity.json";data = {}
    _save(path, data)
    assert comparator.compare_experiment(out)["decision"] == "INVALID_EXPERIMENT"


def test_unfinalized_experiment_is_inconclusive_and_does_not_resume(execution):
    """模拟硬中断缺少最终清单，允许只读检查，不能拿完整旧轮次冒充正式完成。"""
    _run(execution)
    for name in ("integrity.json", "manifest.json", "run-summary.json", "comparison.json"):
        (execution["output"] / name).unlink()
    result = comparator.compare_experiment(execution["output"])
    assert result["decision"] == "INCONCLUSIVE" and result["execution_stage"] == "UNFINALIZED"


def test_invalid_proposal_and_multibatch_input_have_zero_execution_calls(execution):
    """未通过校验的建议、Fake伪装live、代码/参数改变都不能发出复测请求。"""
    with pytest.raises(ValueError, match="LIVE_FAKE_SOURCE_MISMATCH"):
        executor.check_execution(feedback_dir=execution["feedback_dir"], proposal_dir=execution["proposal_dir"],
            output=execution["output"], settings=execution["settings"], authorization=ExecutionAuthorization(), source="live")
    settings = execution["settings"].model_copy(update={"llm_chat_model": "other"})
    with pytest.raises(ValueError, match="RUNTIME_DIFFERS"):
        executor.check_execution(feedback_dir=execution["feedback_dir"], proposal_dir=execution["proposal_dir"],
            output=execution["output"], settings=settings, authorization=ExecutionAuthorization(), source="fake")
    assert not execution["output"].exists()


def test_multibatch_preflight_rejects_without_creating_run(execution, monkeypatch):
    """当前19次授权只覆盖单批三题，不将多批悄悄塞入原预算。"""
    monkeypatch.setattr(executor.SemanticChecker, "_batches", lambda *_: ([[], []], False))
    with pytest.raises(ValueError, match="SINGLE_BATCH_REQUIRED"):
        executor.check_execution(feedback_dir=execution["feedback_dir"], proposal_dir=execution["proposal_dir"],
            output=execution["output"], settings=execution["settings"], authorization=ExecutionAuthorization(), source="fake")
    assert not execution["output"].exists()


def test_additional_request_in_same_round_is_blocked_before_backend(execution):
    """即使检查器以后增加隐藏补调，同一题轮第二次请求也不能突破授权。"""
    calls = []
    class Backend:
        def complete_json_with_usage(self, **kwargs):
            calls.append(kwargs)
            return response(), {}
    profile = ExperimentProfile(max_input_chars=48000, max_output_tokens=6000,
                                max_calls=3, timeout_seconds=60, max_retries=0)
    budget = executor.RequestBudget(ExecutionAuthorization(), 1, 0)
    client = executor.BudgetedClient(backend=Backend(), budget=budget, profile=profile,
                                    directory=execution["root"] / "test-calls")
    kwargs = dict(system_prompt="test", user_payload={}, max_output_tokens=6000)
    client.complete_json_with_usage(**kwargs)
    with pytest.raises(executor.ExperimentStopped):
        client.complete_json_with_usage(**kwargs)
    assert len(calls) == 1 and budget.used == 2


def _stall_worker(connection, credentials, timeout, kwargs):
    """进程测试不访问网络，等待父进程在硬超时后清理。"""
    time.sleep(5)


def test_process_transport_has_hard_wait_and_cleans_up_without_retry():
    """即使底层一直不返回，也不让HTTP逐块read超时无限延长总等待。"""
    client = transport.ProcessTransport({}, .05, worker=_stall_worker)
    started = time.monotonic()
    with pytest.raises(LLMResponseError) as error:
        client.complete_json_with_usage(system_prompt="test", user_payload={}, max_output_tokens=6000)
    assert time.monotonic() - started < 3
    assert error.value.diagnostic.category == "TIMEOUT"


def test_live_worker_receives_actual_120_second_profile(monkeypatch):
    """直接用HTTP客户端替身验证IPC子进程构造的实际参数，无真实网络请求。"""
    seen = {}
    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)
        def complete_json_with_usage(self, **kwargs):
            seen["payload"] = kwargs
            return response(), {"input_tokens": 1, "output_tokens": 1}
    class Connection:
        def send(self, item): seen["returned"] = item
        def close(self): pass
    monkeypatch.setattr(transport, "OpenAICompatibleLLMClient", Client)
    transport._request_worker(Connection(), {"api_key": "fake", "base_url": "fake", "model": "fake"}, 120,
                              {"system_prompt": "test", "user_payload": {}, "max_output_tokens": 6000})
    assert seen["timeout_seconds"] == 120 and seen["max_retries"] == 0
    assert seen["payload"]["max_output_tokens"] == 6000 and seen["returned"][0] == "ok"
