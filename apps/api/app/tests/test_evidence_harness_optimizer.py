"""第二步验收：真实客户端协议用HTTP替身验证，全部测试禁止外部模型与候选复测。"""

from copy import deepcopy
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness import feedback, optimizer
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic
from app.tests.test_evidence_harness_feedback import sources, _save


class FakeAdvisor:
    """显式标记的测试建议来源；从测试预置响应读取，不写死实际策略结论。"""

    def __init__(self, response, usage=None):
        """保存预置响应及用量，不生成真实策略结论。"""
        self.response = response
        self.usage = usage if usage is not None else {"input_tokens": 20, "output_tokens": 10}
        self.calls = []

    def complete_json_with_usage(self, **kwargs):
        """记录实际构造的载荷，确保Gold和业务原文没有进入请求。"""
        self.calls.append(deepcopy(kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return deepcopy(self.response), self.usage


@pytest.fixture
def prepared(sources):
    """仅在隔离测试目录生成完成的第一步输入。"""
    root, dataset, baseline, _ = sources
    directory = root / "experiments/harness-v1/feedback"
    feedback.prepare_feedback(dataset_path=dataset, baseline_path=baseline, history_paths=[], output=directory)
    packet = optimizer.load_feedback(directory)
    return sources, directory, packet


def _suggestion(packet, *, no_change=False):
    """由用例选择响应，测试不冒充真实模型已经选择120秒。"""
    baseline = next(r for r in packet.reports if r.report_ref == packet.baseline_ref)
    return {"decision": "NO_CHANGE" if no_change else "PROPOSE_EXPERIMENT", "target": "evaluation_runtime",
            "feedback_refs": [baseline.trials[0].feedback_id],
            "change": None if no_change else {"field": "timeout_seconds", "from": 60, "to": 120},
            "hypothesis": "检验更长等待预算能否获得完整响应。"}


def _output(prepared, name="proposal"):
    return prepared[0][0] / f"experiments/harness-v1/{name}"


def _run(prepared, response, **kwargs):
    """仅调用Fake建议器，候选执行器在当前阶段不存在。"""
    client = FakeAdvisor(response, **kwargs)
    result = optimizer.propose(feedback_dir=prepared[1], output=_output(prepared), client=client, source="fake")
    return result, client


@pytest.mark.parametrize("no_change,status", [(False, "VALIDATED"), (True, "NO_CHANGE")])
def test_valid_candidate_or_no_change_is_recorded_without_execution(prepared, no_change, status):
    """建议通过只表示可供后续实验，不修改env、原反馈或触发任何复测。"""
    before = {p.name: p.read_bytes() for p in prepared[1].iterdir()}
    raw = _suggestion(prepared[2], no_change=no_change)
    result, client = _run(prepared, raw)
    assert result["stage"] == status and not result["candidate_executed"]
    assert result["source"] == "fake" and result["model_calls"] == 0 and result["fake_calls"] == 1
    assert len(client.calls) == 1
    saved = json.loads((_output(prepared) / "proposal.json").read_text())
    assert saved["proposal"] == raw
    assert before == {p.name: p.read_bytes() for p in prepared[1].iterdir()}
    if os.name != "nt":  # POSIX mode bits do not describe Windows ACLs.
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in _output(prepared).iterdir())
    with pytest.raises(FileExistsError):
        optimizer.propose(feedback_dir=prepared[1], output=_output(prepared), client=client, source="fake")
    assert len(client.calls) == 1


@pytest.mark.parametrize("mutation,reason", [
    ("two_changes", "PROPOSAL_SCHEMA_INVALID"), ("extra_shell", "PROPOSAL_SCHEMA_INVALID"),
    ("wrong_field", "FIELD_NOT_ALLOWED"), ("wrong_value", "VALUE_NOT_ALLOWED"),
    ("wrong_from", "BASELINE_VALUE_MISMATCH"), ("unchanged", "UNCHANGED_VALUE"),
    ("fake_ref", "UNKNOWN_FEEDBACK_REFERENCE"), ("duplicate_ref", "DUPLICATE_FEEDBACK_REFERENCE"),
    ("secret", "UNSAFE_PROPOSAL_TEXT"), ("no_change_action", "PROPOSAL_SCHEMA_INVALID"),
    ("bool_value", "PROPOSAL_SCHEMA_INVALID"),
])
def test_illegal_proposal_is_rejected_without_rewriting_or_retry(prepared, mutation, reason):
    """合法JSON不等于合法策略；拒绝后不擅改模型建议或再问一次。"""
    raw = _suggestion(prepared[2])
    if mutation == "two_changes": raw["change"] = [raw["change"], {"field": "max_output_tokens", "from": 3000, "to": 6000}]
    elif mutation == "extra_shell": raw["shell"] = "SECRET_DO_NOT_SAVE"
    elif mutation == "wrong_field": raw["change"]["field"] = "temperature"
    elif mutation == "wrong_value": raw["change"]["to"] = 999
    elif mutation == "wrong_from": raw["change"]["from"] = 30
    elif mutation == "unchanged": raw["change"]["to"] = 60
    elif mutation == "fake_ref": raw["feedback_refs"] = ["0" * 64]
    elif mutation == "duplicate_ref": raw["feedback_refs"] *= 2
    elif mutation == "secret": raw["hypothesis"] = "api_key=SECRET_DO_NOT_SAVE"
    elif mutation == "bool_value": raw["change"]["to"] = True
    else: raw["decision"] = "NO_CHANGE"
    result, client = _run(prepared, raw)
    assert result["stage"] == "REJECTED" and not result["candidate_executed"]
    assert len(client.calls) == 1
    assert json.loads((_output(prepared) / "validation.json").read_text())["reason_code"] == reason
    assert "SECRET_DO_NOT_SAVE" not in "".join(p.read_text() for p in _output(prepared).iterdir())


@pytest.mark.parametrize("foreign", [False, True])
def test_previous_failure_or_foreign_context_cannot_be_proposed(sources, foreign):
    """同条件旧失败不重复，不同模型/上下文的反馈不能用来授权当前调整。"""
    root, dataset, baseline, raw = sources
    old = baseline.with_name("old.json")
    _save(old, raw)
    raw["runtime_config"]["max_output_tokens"] = 6000
    if foreign:
        raw["runtime_config"]["model"] = "other-model"
    raw["runtime_config_sha256"] = digest(raw["runtime_config"])
    _save(baseline, raw)
    directory = root / "experiments/harness-v1/history"
    feedback.prepare_feedback(dataset_path=dataset, baseline_path=baseline, history_paths=[old], output=directory)
    packet = optimizer.load_feedback(directory)
    raw = _suggestion(packet)
    raw["change"] = {"field": "max_output_tokens", "from": 6000, "to": 3000}
    if foreign:
        raw["feedback_refs"] = [packet.reports[0].trials[0].feedback_id]
    client = FakeAdvisor(raw)
    output = root / "experiments/harness-v1/result"
    result = optimizer.propose(feedback_dir=directory, output=output, client=client, source="fake")
    assert result["stage"] == "REJECTED" and len(client.calls) == 1
    expected = "FOREIGN_FEEDBACK_CONTEXT" if foreign else "PREVIOUSLY_UNSUCCESSFUL_PROFILE"
    assert json.loads((output / "validation.json").read_text())["reason_code"] == expected


def test_actual_model_payload_excludes_gold_text_identity_and_paths(prepared):
    """检查真正传给client的载荷，而不仅是第一步磁盘文件。"""
    _, client = _run(prepared, _suggestion(prepared[2]))
    sent = json.dumps(client.calls, ensure_ascii=False)
    for value in ("GOLD_PRIVATE_SENTINEL", "QUESTION_PRIVATE_SENTINEL", "QUOTE_PRIVATE_SENTINEL",
                  "MODEL_PRIVATE_SENTINEL", "expected_issues", "expected_explanation", "source-index",
                  "annotation_sha256", "issue_types", "user_id", "conversation_id"):
        assert value not in sent
    assert "allowed_changes" in sent and "response_schema" in sent


@pytest.mark.parametrize("error", [
    LLMResponseError("SECRET_DO_NOT_SAVE", diagnostic=make_diagnostic(category="TIMEOUT", stage="LLM_REQUEST")),
    LLMResponseError("SECRET_DO_NOT_SAVE", diagnostic=make_diagnostic(category="RESPONSE_FORMAT_INVALID", stage="MODEL_RESPONSE_PARSE")),
    RuntimeError("SECRET_DO_NOT_SAVE"),
])
def test_optimizer_failure_is_separate_from_evidence_failure_and_never_retried(prepared, error):
    """建议模型故障另记OPTIMIZER_FAILED，不变成业务MISSING或EVALUATION_FAILED。"""
    result, client = _run(prepared, error)
    assert result["stage"] == "OPTIMIZER_FAILED" and len(client.calls) == 1
    invocation = json.loads((_output(prepared) / "invocation.json").read_text())
    assert invocation["role"] == "OPTIMIZER" and invocation["input_tokens"] is None
    assert not invocation["token_usage_complete"]
    assert "SECRET_DO_NOT_SAVE" not in "".join(p.read_text() for p in _output(prepared).iterdir())


@pytest.mark.parametrize("target", ["feedback", "optimizer-input", "source"])
def test_tampering_prevents_any_call(prepared, target):
    """篡改已有反馈或原始报告后，先拒绝而不是先付费再检查。"""
    sources, directory, packet = prepared
    if target == "source":
        sources[2].write_text("{}")
    else:
        path = directory / f"{target}.json"
        data = json.loads(path.read_text());data["expected_issues"] = ["injected"]
        _save(path, data)
    client = FakeAdvisor(_suggestion(packet))
    with pytest.raises(ValueError):
        optimizer.propose(feedback_dir=directory, output=_output(prepared), client=client, source="fake")
    assert client.calls == [] and not _output(prepared).exists()


def test_preflight_is_zero_call_and_oversized_input_fails_early(prepared, monkeypatch):
    """默认检查不创建目录；输入超预算不截断后伪称完整，也不发请求。"""
    _, payload, output = optimizer.check_propose(feedback_dir=prepared[1], output=_output(prepared))
    assert payload and not output.exists()
    monkeypatch.setattr(optimizer, "MAX_INPUT_CHARS", 10)
    client = FakeAdvisor(_suggestion(prepared[2]))
    with pytest.raises(ValueError, match="OPTIMIZER_INPUT_TOO_LARGE"):
        optimizer.propose(feedback_dir=prepared[1], output=output, client=client, source="fake")
    assert client.calls == [] and not output.exists()


def test_proposal_output_cannot_change_feedback_or_escape_test_root(prepared, tmp_path):
    """输出目录不能覆盖、嵌入旧反馈或越出隔离目录。"""
    client = FakeAdvisor(_suggestion(prepared[2]))
    for output in (prepared[1], prepared[1] / "nested", tmp_path / "outside"):
        with pytest.raises(ValueError):
            optimizer.propose(feedback_dir=prepared[1], output=output, client=client, source="fake")
    assert client.calls == []


def test_linked_feedback_artifact_is_not_read_or_sent(prepared, tmp_path):
    """即使目录本身合法，单个产物被换成符号链接也必须停止。"""
    path = prepared[1] / "optimizer-input.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows user lacks symlink creation privilege")
        raise
    client = FakeAdvisor(_suggestion(prepared[2]))
    with pytest.raises(ValueError, match="SYMLINK_FORBIDDEN"):
        optimizer.propose(feedback_dir=prepared[1], output=_output(prepared), client=client, source="fake")
    assert not client.calls


def _settings():
    """合成配置只配合HTTP替身，绝不加载用户env。"""
    return SimpleNamespace(llm_enabled=True, llm_chat_model="MODEL_PRIVATE_SENTINEL",
                           llm_base_url="private-endpoint", llm_api_key="KEY_DO_NOT_SAVE")


def test_real_client_wiring_uses_exact_safe_payload_one_request_and_zero_retries(prepared, monkeypatch):
    """用HTTP替身穿过真实客户端，证明接线是实际调用而非规则生成建议。"""
    calls = []
    raw = _suggestion(prepared[2])

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(raw)}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 15}},
            request=httpx.Request("POST", "https://fake.test/chat/completions"))

    monkeypatch.setattr(httpx, "post", post)
    client = optimizer.build_optimizer_client(_settings(), prepared[2])
    result = optimizer.propose(feedback_dir=prepared[1], output=_output(prepared), client=client, source="live")
    assert result["stage"] == "VALIDATED" and len(calls) == 1
    assert client.max_retries == 0 and calls[0][1]["timeout"] == 60
    assert calls[0][1]["json"]["max_tokens"] == optimizer.MAX_OUTPUT_TOKENS
    assert json.loads(calls[0][1]["json"]["messages"][1]["content"]) == optimizer.request_payload(prepared[2])
    assert "KEY_DO_NOT_SAVE" not in "".join(p.read_text() for p in _output(prepared).iterdir())


def test_live_request_timeout_cannot_trigger_internal_retry(prepared, monkeypatch):
    """真实客户端网络故障也只能发送一次，错误文本不进入结果。"""
    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("SECRET_DO_NOT_SAVE")

    monkeypatch.setattr(httpx, "post", timeout)
    client = optimizer.build_optimizer_client(_settings(), prepared[2])
    result = optimizer.propose(feedback_dir=prepared[1], output=_output(prepared), client=client, source="live")
    assert result["stage"] == "OPTIMIZER_FAILED" and len(calls) == 1
    diagnostic = json.loads((_output(prepared) / "invocation.json").read_text())["diagnostic"]
    assert diagnostic["category"] == "TIMEOUT" and diagnostic["exception_type"] == "ReadTimeout"


def test_live_client_cannot_enable_hidden_retries(prepared):
    """live接线拒绝被改成内部重试的客户端，不让一次建议变成多次计费。"""
    client = optimizer.build_optimizer_client(_settings(), prepared[2])
    client.max_retries = 1
    with pytest.raises(ValueError, match="OPTIMIZER_CLIENT_BUDGET_INVALID"):
        optimizer.propose(feedback_dir=prepared[1], output=_output(prepared), client=client, source="live")
    assert not _output(prepared).exists()


def test_cli_defaults_to_check_and_does_not_build_client(prepared, monkeypatch, capsys):
    """少写--live时既不请求模型，也不落下假冒真实建议的产物。"""
    import sys
    from app.scripts import evidence_harness

    def forbidden(*args, **kwargs):
        pytest.fail("Default propose must not construct a live client")

    monkeypatch.setattr(evidence_harness, "build_optimizer_client", forbidden)
    monkeypatch.setattr(sys, "argv", ["harness", "propose", "--feedback", str(prepared[1]),
                                     "--output", str(_output(prepared))])
    evidence_harness.main()
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "CHECK_PASSED" and result["model_calls"] == 0
    assert not _output(prepared).exists()


def test_missing_authorization_or_provider_change_is_rejected(prepared):
    """既有LLM授权仍生效，不能悄悄切换模型或服务后复用旧反馈。"""
    settings = _settings();settings.llm_enabled = False
    with pytest.raises(ValueError, match="OPTIMIZER_LLM_DISABLED"):
        optimizer.build_optimizer_client(settings, prepared[2])
    settings.llm_enabled = True;settings.llm_chat_model = "different"
    with pytest.raises(ValueError, match="OPTIMIZER_PROVIDER_DIFFERS"):
        optimizer.build_optimizer_client(settings, prepared[2])
