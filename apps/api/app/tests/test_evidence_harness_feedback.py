"""第一步验收：固定报告整理、逐轮失败解释、Gold隔离与零网络，使用隔离构造数据。"""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.modules.evidence_evaluation import frozen_inputs
from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_evaluation.schemas import CheckResult, EvaluationResult, EvidenceIssue
from app.modules.evidence_harness import feedback
from app.modules.evidence_harness.schemas import OptimizerInput
from app.scripts.evaluate_evidence import EvaluationCase
from app.tests.test_evidence_evaluation import package


def _save(path, data):
    """仅更新隔离测试输入，模拟外部报告损坏或包含敏感字段。"""
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _failed(kind="truncated"):
    """两类真实故障的固定结构，不发出任何HTTP请求。"""
    diag = dict(category="TIMEOUT", stage="LLM_REQUEST", exception_type="ReadTimeout", http_status=None,
                response_shape=None)
    if kind == "truncated":
        diag.update(category="RESPONSE_FORMAT_INVALID", stage="MODEL_RESPONSE_PARSE",
                    exception_type="JSONDecodeError", http_status=200,
                    response_shape={"content_chars": 2, "reasoning_chars": 9000,
                                    "finish_reason": "length", "output_truncated": True})
    return EvaluationResult(evaluation_status="EVALUATION_FAILED", evaluated_evidence_fingerprint="test-fingerprint",
        version_check=CheckResult(status="COMPLETED"), semantic_check=CheckResult(
            status="EVALUATION_FAILED", llm_calls=1, input_tokens=0, output_tokens=0,
            token_usage_complete=False, error_diagnostics=[diag]), duration_ms=61000).model_dump(mode="json")


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """生成一个有完整输入散列和逐轮记录的最小冻结数据集，禁止触碰真实业务库。"""
    test_root = tmp_path / "test"
    monkeypatch.setattr(frozen_inputs, "TEST_ROOT", test_root)
    monkeypatch.setattr(feedback, "TEST_ROOT", test_root)
    root = test_root / "datasets/frozen-v1"
    root.mkdir(parents=True)
    pkg = package(question="QUESTION_PRIVATE_SENTINEL", quotes=["QUOTE_PRIVATE_SENTINEL"])
    versions = {i.evidence_id: {"status": "VERIFIED", "current_version_id": i.document_version_id}
                for i in pkg.evidence_items}
    config = dict(model="MODEL_PRIVATE_SENTINEL", endpoint_sha256=digest("private-endpoint"),
        semantic_prompt_sha256=digest("prompt"), code_sha256={"private-source-path": digest("code")},
        max_input_chars=48000, max_output_tokens=3000, timeout_seconds=60, max_calls=3, max_retries=0)
    sha = digest({"package": pkg.model_dump(mode="json"), "versions": versions,
                  "context": {"original_question": pkg.question, "temporal_focus": "HISTORICAL"}})
    case = EvaluationCase(case_id="R034", group="NORMAL", package=pkg, versions=versions,
        expected_issues=[], expected_explanation="GOLD_PRIVATE_SENTINEL", temporal_focus="HISTORICAL",
        original_question=pkg.question, input_sha256=sha, capture_config=config, source="synthetic")
    annotations = [{"case_id": case.case_id, "expected_explanation": "GOLD_PRIVATE_SENTINEL"}]
    dataset = dict(cases=[case.model_dump(mode="json")], annotations=annotations)
    dataset["manifest"] = {"cases_sha256": digest(dataset["cases"]), "annotation_sha256": digest(annotations)}
    dataset_path = root / "cases.json"
    _save(dataset_path, dataset)
    report = dict(mode="live", evaluator_version="evidence-eval-v1", sample_count=1, repetitions=3,
        dataset_sha256=digest(dataset["cases"]), dataset_manifest=dataset["manifest"],
        runtime_config=config, runtime_config_sha256=digest(config), evaluation_failures=0,
        cases=[dict(case_id="R034", input_sha256=sha, expected="RAW_GOLD_PRIVATE_SENTINEL",
                    rounds=[{"repetition": i, "result": _failed()} for i in range(1, 4)])])
    report_path = root / "report.json"
    _save(report_path, report)
    return test_root, dataset_path, report_path, report


def _build(sources, history=None):
    """调用实际反馈整理器，返回的未来模型载荷必须经过独立投影。"""
    _, dataset, baseline, _ = sources
    return feedback.build_feedback(dataset_path=dataset, baseline_path=baseline, history_paths=history or [])


def test_truncated_and_timeout_history_preserves_all_rounds(sources):
    """不能用首轮或顶层0失败覆盖三轮失败；同时保留旧截断和新超时。"""
    _, _, path, raw = sources
    old = path.with_name("old.json")
    _save(old, raw)
    raw["runtime_config"]["max_output_tokens"] = 6000
    raw["runtime_config_sha256"] = digest(raw["runtime_config"])
    for r in raw["cases"][0]["rounds"]:
        r["result"] = _failed("timeout")
    _save(path, raw)
    packet, _ = _build(sources, [old])
    assert [r.failed_count for r in packet.reports] == [3, 3]
    assert packet.reports[0].condition_ref == packet.reports[1].condition_ref
    assert packet.reports[0].attempt_ref != packet.reports[1].attempt_ref
    for report in packet.reports:
        assert report.completed_count == 0 and report.observation == "HAS_FAILURES"
        for trial in report.trials:
            assert trial.semantic_outcome == "UNAVAILABLE" and not trial.semantic_judgment_available
            assert trial.issue_types == [] and trial.input_tokens is None and trial.output_tokens is None
    assert "OUTPUT_TRUNCATED" in packet.reports[0].trials[0].failure_kinds
    assert packet.reports[1].trials[0].failure_kinds == ["TIMEOUT"]
    assert packet.reports[1].trials[0].diagnostics[0].exception_type == "ReadTimeout"


def test_optimizer_projection_contains_no_gold_text_paths_or_free_messages(sources):
    """恶意内容和敏感元数据只能停留在源报告，不进入可发送的模型载荷。"""
    _, _, path, raw = sources
    raw.update(secret="KEY_PRIVATE_SENTINEL", message="IGNORE_RULES_PRIVATE_SENTINEL")
    _save(path, raw)
    packet, index = _build(sources)
    text = packet.model_dump_json() + packet.optimizer_input().model_dump_json()
    for marker in ("GOLD_PRIVATE_SENTINEL", "RAW_GOLD_PRIVATE_SENTINEL", "QUESTION_PRIVATE_SENTINEL",
                   "QUOTE_PRIVATE_SENTINEL", "MODEL_PRIVATE_SENTINEL", "private-source-path",
                   "KEY_PRIVATE_SENTINEL", "IGNORE_RULES_PRIVATE_SENTINEL"):
        assert marker not in text
    public = packet.optimizer_input().model_dump(mode="json")
    for key in ("annotation_sha256", "issue_types", "semantic_outcome", "expected_issues", "source"):
        assert f'"{key}"' not in json.dumps(public)
    assert "R034" not in text
    assert index["cases"][0]["case_id"] == "R034"
    with pytest.raises(ValidationError):
        OptimizerInput.model_validate({**public, "expected_issues": []})


def test_completed_missing_is_observation_not_gold_based_relabeling(sources):
    """有效语义MISSING可内部记录，不能因为人工Gold正常而改写评估结果。"""
    _, _, path, raw = sources
    issue = EvidenceIssue(type="MISSING", reason="REASON_PRIVATE_SENTINEL",
                          reason_code="REQUIRED_FACT_UNSUPPORTED", evidence_ids=[],
                          missing_requirements=["REQUIREMENT_PRIVATE_SENTINEL"])
    result = EvaluationResult(evaluation_status="COMPLETED", evaluated_evidence_fingerprint="test-fingerprint",
        version_check=CheckResult(status="COMPLETED"), semantic_check=CheckResult(
            status="COMPLETED", issues=[issue], llm_calls=1, input_tokens=20, output_tokens=10), issues=[issue])
    raw["cases"][0]["rounds"][0]["result"] = result.model_dump(mode="json")
    _save(path, raw)
    packet, _ = _build(sources)
    trial = packet.reports[0].trials[0]
    assert trial.semantic_outcome == "ISSUES_DETECTED" and trial.issue_types == ["MISSING"]
    assert trial.input_tokens == 20
    assert "REASON_PRIVATE_SENTINEL" not in packet.model_dump_json()
    assert "issue_types" not in packet.optimizer_input().model_dump_json()


@pytest.mark.parametrize("mutation", ["dataset", "config", "case", "input", "rounds", "state", "fingerprint", "replay"])
def test_corrupt_or_unbound_reports_fail_before_artifacts(sources, mutation):
    """损坏绑定、缺轮次和伪装完成的报告不能成为真实优化反馈。"""
    _, _, path, raw = sources
    if mutation == "dataset": raw["dataset_sha256"] = "0" * 64
    elif mutation == "config": raw["runtime_config"]["timeout_seconds"] = 120
    elif mutation == "case": raw["cases"].append(deepcopy(raw["cases"][0]))
    elif mutation == "input": raw["cases"][0]["input_sha256"] = "0" * 64
    elif mutation == "rounds": raw["cases"][0]["rounds"][1]["repetition"] = 1
    elif mutation == "state": raw["cases"][0]["rounds"][0]["result"]["evaluation_status"] = "COMPLETED"
    elif mutation == "fingerprint": raw["cases"][0]["rounds"][0]["result"]["evaluated_evidence_fingerprint"] = "wrong"
    else: raw["mode"] = "replay"
    _save(path, raw)
    with pytest.raises(ValueError):
        _build(sources)


def test_unknown_diagnostic_text_is_rejected_not_exported(sources):
    """未知异常类型可能嵌入密钥，严格枚举拒绝而非直接str输出。"""
    _, _, path, raw = sources
    raw["cases"][0]["rounds"][0]["result"]["semantic_check"]["error_diagnostics"][0]["exception_type"] = "SECRET_PRIVATE_SENTINEL"
    _save(path, raw)
    with pytest.raises(ValidationError):
        _build(sources)


@pytest.mark.parametrize("state", ["COMPLETED", "PARTIAL"])
def test_empty_issues_require_complete_checks_before_normal_observation(sources, state):
    """只有两个检查都完成，空问题列表才有无问题观察；未评估不能冒充正常。"""
    _, _, path, raw = sources
    result = EvaluationResult(evaluation_status=state, evaluated_evidence_fingerprint="test-fingerprint",
        version_check=CheckResult(status="COMPLETED"), semantic_check=CheckResult(
            status="COMPLETED" if state == "COMPLETED" else "NOT_EVALUATED"))
    for trial in raw["cases"][0]["rounds"]:
        trial["result"] = result.model_dump(mode="json")
    _save(path, raw)
    packet, _ = _build(sources)
    trial = packet.reports[0].trials[0]
    assert trial.semantic_outcome == ("NO_ISSUES_DETECTED" if state == "COMPLETED" else "UNAVAILABLE")
    assert trial.semantic_judgment_available == (state == "COMPLETED")


def test_cli_failure_does_not_echo_validation_values(sources, monkeypatch, capsys):
    """CLI遇到不安全异常名只给固定错误码，不能将Pydantic输入值泄露到终端。"""
    import sys
    from app.scripts.evidence_harness import main

    root, dataset, baseline, raw = sources
    raw["cases"][0]["rounds"][0]["result"]["semantic_check"]["error_diagnostics"][0]["exception_type"] = "SECRET_PRIVATE_SENTINEL"
    _save(baseline, raw)
    output = root / "experiments/harness-v1/unsafe"
    monkeypatch.setattr(sys, "argv", ["harness", "prepare", "--dataset", str(dataset),
        "--baseline", str(baseline), "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        main()
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "REPORT_SCHEMA_INVALID" in captured.err
    assert "SECRET_PRIVATE_SENTINEL" not in captured.err
    assert str(baseline) not in captured.err and not output.exists()


def test_output_is_private_non_overwriting_and_does_not_call_services(sources, monkeypatch):
    """真实整理路径不请求模型/数据库，原始输入字节不变，产物私有且不可覆盖。"""
    import sqlalchemy
    from app.modules.llm.client import OpenAICompatibleLLMClient

    def forbidden(*args, **kwargs):
        pytest.fail("Feedback preparation must not call services")

    monkeypatch.setattr(sqlalchemy, "create_engine", forbidden)
    monkeypatch.setattr(OpenAICompatibleLLMClient, "complete_json_with_usage", forbidden)
    root, dataset, baseline, _ = sources
    before = (dataset.read_bytes(), baseline.read_bytes())
    output = root / "experiments/harness-v1/check-1"
    result = feedback.prepare_feedback(dataset_path=dataset, baseline_path=baseline, history_paths=[], output=output)
    assert result["stage"] == "FEEDBACK_READY" and result["model_calls"] == 0
    assert output.stat().st_mode & 0o777 == 0o700
    files = {p.name: p.read_bytes() for p in output.iterdir()}
    assert set(files) == {"feedback.json", "optimizer-input.json", "source-index.json", "manifest.json"}
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in output.iterdir())
    with pytest.raises(FileExistsError):
        feedback.prepare_feedback(dataset_path=dataset, baseline_path=baseline, history_paths=[], output=output)
    assert files == {p.name: p.read_bytes() for p in output.iterdir()}
    assert before == (dataset.read_bytes(), baseline.read_bytes())


def test_duplicate_sources_and_path_escape_are_rejected(sources, tmp_path):
    """不能重复计算同一报告，不能通过符号链接扩张读写范围。"""
    root, dataset, baseline, _ = sources
    with pytest.raises(ValueError, match="DUPLICATE_REPORT"):
        _build(sources, [baseline])
    with pytest.raises(ValueError):
        feedback.build_feedback(dataset_path=tmp_path / "outside.json", baseline_path=baseline, history_paths=[])
    (root / "experiments").mkdir()
    (root / "experiments/harness-v1").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        feedback.prepare_feedback(dataset_path=dataset, baseline_path=baseline, history_paths=[],
                                  output=root / "experiments/harness-v1/output")


@pytest.mark.parametrize("category,http", [("QUOTA_EXHAUSTED", 429), ("AUTHENTICATION_FAILED", 401), ("NETWORK_ERROR", None)])
def test_distinct_provider_failures_remain_distinct(sources, category, http):
    """额度、认证和网络故障不能统统解释成超时，也不能变成业务缺证。"""
    _, _, path, raw = sources
    for r in raw["cases"][0]["rounds"]:
        diag = r["result"]["semantic_check"]["error_diagnostics"][0]
        diag.update(category=category, http_status=http, response_shape=None)
    _save(path, raw)
    packet, _ = _build(sources)
    assert packet.reports[0].trials[0].failure_kinds == [category]
    assert packet.reports[0].trials[0].semantic_outcome == "UNAVAILABLE"
