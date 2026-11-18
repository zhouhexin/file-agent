"""验证模型失败的阶段、类别与安全日志，所有 HTTP 响应由本地 fake 提供。"""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.modules.evidence_evaluation.semantic_checker import SemanticChecker
from app.modules.evidence_evaluation.service import log_shadow_result
from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient
from app.modules.llm.diagnostics import SafeErrorDiagnostic
from app.tests.test_evidence_answer import FakeEvidenceClient, _session, _settings, _seed
from app.modules.evidence_answer.service import EvidenceAnswerService
from app.tests.test_evidence_evaluation import FakeClient, evaluate, package, response


SECRET = "sk-diagnostic-test-do-not-log"
PRIVATE_TEXT = "真实文件正文测试哨兵"


def _client():
    """构造真实客户端，HTTP 调用在每个用例中替换，禁止访问真实服务。"""

    return OpenAICompatibleLLMClient(
        api_key=SECRET, base_url="https://example.invalid/v1", model="fake-model", timeout_seconds=20,
    )


def _http_response(status, *, body=None, content=None):
    """错误体和请求 URL 故意含敏感哨兵，确保日志从不复制它们。"""

    kwargs = {"json": body} if content is None else {"content": content}
    return httpx.Response(
        status, request=httpx.Request("POST", f"https://example.invalid/?api_key={SECRET}"), **kwargs,
    )


@pytest.mark.parametrize(
    ("status", "error", "category"),
    [
        (403, {"code": "insufficient_quota"}, "QUOTA_EXHAUSTED"),
        (429, {"type": "insufficient_quota"}, "QUOTA_EXHAUSTED"),
        (403, {"code": "AllocationQuota.FreeTierOnly"}, "QUOTA_EXHAUSTED"),
        (401, {}, "AUTHENTICATION_FAILED"),
        (402, {}, "QUOTA_EXHAUSTED"),
        (403, {}, "PERMISSION_DENIED"),
        (429, {"code": "rate_limit_exceeded"}, "RATE_LIMITED"),
        (408, {}, "TIMEOUT"),
        (504, {}, "TIMEOUT"),
        (500, {}, "PROVIDER_ERROR"),
        (503, {}, "PROVIDER_ERROR"),
        (400, {}, "REQUEST_REJECTED"),
        (404, {}, "REQUEST_REJECTED"),
        # 自由文本声称额度不足不作为分类依据，只有结构化白名单错误码可提升为额度类别。
        (403, {"message": "insufficient_quota"}, "PERMISSION_DENIED"),
    ],
)
def test_http_failure_classification_has_status_without_response_or_url(monkeypatch, status, error, category):
    """准确区分额度、限流和权限，同时保持服务端错误体、密钥与 URL 不可见。"""

    error = {"message": SECRET + PRIVATE_TEXT, **error}
    fake_response = _http_response(status, body={"error": error})
    monkeypatch.setattr("app.modules.llm.client.httpx.post", lambda *args, **kwargs: fake_response)

    with pytest.raises(LLMResponseError) as caught:
        _client().complete_json(system_prompt="测试", user_payload={"text": PRIVATE_TEXT})

    diagnostic = caught.value.diagnostic
    assert diagnostic.category == category
    assert diagnostic.stage == "LLM_REQUEST"
    assert diagnostic.http_status == status
    assert diagnostic.exception_type == "HTTPStatusError"
    safe_output = str(caught.value) + json.dumps(diagnostic.log_fields(), ensure_ascii=False)
    assert SECRET not in safe_output and PRIVATE_TEXT not in safe_output
    assert "example.invalid" not in safe_output


@pytest.mark.parametrize(
    ("exception_class", "category"),
    [
        (httpx.ConnectError, "NETWORK_ERROR"),
        (httpx.RemoteProtocolError, "NETWORK_ERROR"),
        (httpx.ProxyError, "NETWORK_ERROR"),
        (httpx.ReadError, "NETWORK_ERROR"),
        (httpx.ReadTimeout, "TIMEOUT"),
        (httpx.ConnectTimeout, "TIMEOUT"),
        (httpx.UnsupportedProtocol, "CONFIGURATION_ERROR"),
        (httpx.InvalidURL, "CONFIGURATION_ERROR"),
    ],
)
def test_transport_failure_does_not_invent_http_status(monkeypatch, exception_class, category):
    """网络或超时没有 HTTP 响应时记录 null，不伪造 200 或错误状态。"""

    def failed_post(*args, **kwargs):
        """模拟带敏感文本的传输错误。"""

        raise exception_class(SECRET + PRIVATE_TEXT)

    monkeypatch.setattr("app.modules.llm.client.httpx.post", failed_post)
    with pytest.raises(LLMResponseError) as caught:
        _client().complete_json(system_prompt="测试", user_payload={})

    diagnostic = caught.value.diagnostic
    assert diagnostic.category == category
    assert diagnostic.stage == "LLM_REQUEST"
    assert diagnostic.http_status is None
    assert diagnostic.exception_type == exception_class.__name__
    assert SECRET not in str(caught.value) and PRIVATE_TEXT not in str(caught.value)


@pytest.mark.parametrize(
    ("body", "content", "stage"),
    [
        (None, b"", "HTTP_RESPONSE_PARSE"),
        (None, b"<html>private-response</html>", "HTTP_RESPONSE_PARSE"),
        ([], None, "HTTP_RESPONSE_PARSE"),
        ({"error": "invalid-shape"}, None, "MODEL_RESPONSE_PARSE"),
        ({"choices": [{"message": {"content": "not-json"}}]}, None, "MODEL_RESPONSE_PARSE"),
        ({"choices": [{"message": {"content": "[]"}}]}, None, "MODEL_RESPONSE_PARSE"),
        ({"choices": [{"message": {"content": None}}]}, None, "MODEL_RESPONSE_PARSE"),
    ],
)
def test_http_success_with_bad_content_records_parsing_stage(monkeypatch, body, content, stage):
    """HTTP 200 不等于有效模型结果，错误定位到 HTTP 包或模型内容解析。"""

    fake_response = _http_response(200, body=body, content=content)
    monkeypatch.setattr("app.modules.llm.client.httpx.post", lambda *args, **kwargs: fake_response)
    with pytest.raises(LLMResponseError) as caught:
        _client().complete_json(system_prompt="测试", user_payload={})

    diagnostic = caught.value.diagnostic
    assert diagnostic.category == "RESPONSE_FORMAT_INVALID"
    assert diagnostic.http_status == 200
    assert diagnostic.stage == stage


@pytest.mark.parametrize("case", ["schema", "evidence", "internal", "unknown_llm"])
def test_semantic_validation_and_internal_errors_are_distinct(case):
    """格式、引用和内部错误分别定位，不把未知 LLM 错误误报成网络或额度问题。"""

    raw = response()
    if case == "schema":
        raw["requirements"][0]["status"] = SECRET
        expected = ("SCHEMA_INVALID", "SEMANTIC_SCHEMA_VALIDATION", "ValidationError", "SEMANTIC_SCHEMA_INVALID")
    elif case == "evidence":
        raw["requirements"][0]["evidence_ids"] = ["invented-id"]
        expected = ("EVIDENCE_VALIDATION_FAILED", "SEMANTIC_EVIDENCE_VALIDATION", "EvaluationValidationError", "UNKNOWN_EVIDENCE_ID")
    elif case == "internal":
        # 任意自定义类名也不能混入日志，未知名字归为 Exception。
        raw = type("Sensitive_" + SECRET, (Exception,), {})(SECRET)
        expected = ("INTERNAL_ERROR", "LLM_REQUEST", "Exception", "SEMANTIC_CHECK_FAILED")
    else:
        raw = LLMResponseError(SECRET)
        expected = ("UNKNOWN_LLM_ERROR", "LLM_REQUEST", "LLMResponseError", "SEMANTIC_CHECK_FAILED")

    result = SemanticChecker(client=FakeClient(raw)).check(package())
    diagnostic = result.error_diagnostics[0]
    assert result.status == "EVALUATION_FAILED" and not result.issues
    assert (diagnostic.category, diagnostic.stage, diagnostic.exception_type, result.error_codes[0]) == expected
    assert diagnostic.http_status is None
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize("case", ["quota", "network", "json", "schema"])
def test_real_shadow_jsonl_propagates_safe_diagnostics_and_keeps_outdated(monkeypatch, tmp_path, case):
    """真实评估编排和 JSONL 写入保留原因与版本问题，绝不泄漏失败响应或原文。"""

    calls = []

    def fake_post(*args, **kwargs):
        """在统一客户端入口制造失败，覆盖异常跨层传递链。"""

        calls.append(kwargs)
        if case == "network":
            raise httpx.ConnectError(SECRET + PRIVATE_TEXT)
        if case == "quota":
            return _http_response(403, body={"error": {"code": "insufficient_quota", "message": SECRET + PRIVATE_TEXT}})
        content = SECRET + PRIVATE_TEXT if case == "json" else json.dumps({"bad_field": SECRET + PRIVATE_TEXT})
        return _http_response(200, body={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr("app.modules.llm.client.httpx.post", fake_post)
    result = evaluate(current="version_new", client=_client())
    log_shadow_result(result, settings=_settings().model_copy(update={"log_dir": str(tmp_path)}), agent_run_id="diagnostic-run")

    record_text = next(tmp_path.glob("file-agent-*.log")).read_text()
    record = json.loads(record_text)
    expected = {"quota": "QUOTA_EXHAUSTED", "network": "NETWORK_ERROR", "json": "RESPONSE_FORMAT_INVALID", "schema": "SCHEMA_INVALID"}[case]
    assert record["status"] == "EVALUATION_FAILED"
    assert record["error_details"][0]["category"] == expected
    assert record["error_code"] == "SEMANTIC_" + expected
    assert record["message"] == record["error_details"][0]["message"]
    assert record["issue_types"] == ["OUTDATED"]
    assert record["llm_calls"] == len(calls) == 1
    assert not record["token_usage_complete"]
    assert SECRET not in record_text and PRIVATE_TEXT not in record_text
    assert package().question not in record_text
    assert all(item.quote not in record_text for item in package().evidence_items)


def test_diagnostic_schema_rejects_free_text_and_untrusted_class_names():
    """协议层拒绝原始异常文本、任意类别、路径式类名和无效 HTTP 状态。"""

    valid = {"category": "NETWORK_ERROR", "stage": "LLM_REQUEST", "exception_type": "ConnectError"}
    for invalid in (
        {"message": SECRET}, {"exception_type": "/private/secret"},
        {"category": SECRET}, {"stage": SECRET}, {"http_status": 999},
    ):
        with pytest.raises(ValidationError):
            SafeErrorDiagnostic.model_validate({**valid, **invalid})


def test_shadow_setup_configuration_error_is_logged_without_blocking_answer(tmp_path):
    """模型配置不完整时，记录初始化阶段；旁观评估故障仍不阻断原回答。"""

    with _session() as db:
        _seed(db)
        service = EvidenceAnswerService(
            db=db, user_id="user-1", conversation_id="conversation-1", client=FakeEvidenceClient(),
            settings=_settings().model_copy(update={
                "log_dir": str(tmp_path), "evidence_evaluation_mode": "shadow", "llm_api_key": "",
            }),
        )
        answer = service.answer(question="当前申报截止时间是什么？", document_ids=["document-1"])
        assert answer["ok"]
        records = [json.loads(line) for path in tmp_path.glob("file-agent-*.log") for line in path.read_text().splitlines()]
        diagnostic = next(item for item in records if item["event"] == "evidence_evaluation.shadow")["error_details"][0]
        assert diagnostic["category"] == "CONFIGURATION_ERROR"
        assert diagnostic["stage"] == "SHADOW_SETUP"
        assert diagnostic["exception_type"] == "LLMConfigurationError"
        assert diagnostic["http_status"] is None
