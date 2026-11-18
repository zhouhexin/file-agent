"""返回形态诊断的类型、边界与日志安全测试；不调用真实模型或网络。"""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.logging import log_context
from app.modules.evidence_evaluation.service import build_semantic_checker, log_shadow_result
from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient
from app.modules.llm.response_shape import ResponseShape
from app.tests.test_evidence_answer import _settings
from app.tests.test_evidence_evaluation import evaluate, response


SECRET = "sk-response-shape-private-data"


def _client(observer=None):
    """使用真实解析代码，HTTP在测试内替换。"""

    return OpenAICompatibleLLMClient(
        api_key=SECRET, base_url="https://example.invalid/v1", model="fake", timeout_seconds=20,
        response_observer=observer,
    )


def _http(body):
    """URL故意带敏感哨兵，日志必须只保留形态。"""

    return httpx.Response(200, json=body, request=httpx.Request("POST", f"https://example.invalid/?api_key={SECRET}"))


@pytest.mark.parametrize(
    ("content", "content_type", "outcome", "parsed_type"),
    [
        (None, "null", "CONTENT_NULL", "not_attempted"),
        ("", "string", "CONTENT_EMPTY", "not_attempted"),
        (" \n ", "string", "CONTENT_EMPTY", "not_attempted"),
        ([], "array", "CONTENT_NON_TEXT", "not_attempted"),
        ({"secret_field": SECRET}, "object", "CONTENT_NON_TEXT", "not_attempted"),
        ("[1,2]", "string", "MODEL_JSON_NOT_OBJECT", "array"),
        ('"plain-text"', "string", "MODEL_JSON_NOT_OBJECT", "string"),
        ("null", "string", "MODEL_JSON_NOT_OBJECT", "null"),
        ("true", "string", "MODEL_JSON_NOT_OBJECT", "boolean"),
        ("42", "string", "MODEL_JSON_NOT_OBJECT", "number"),
        ("not-json " + SECRET, "string", "MODEL_JSON_INVALID", "unparseable"),
    ],
)
def test_content_forms_are_distinguished_without_saving_values(monkeypatch, content, content_type, outcome, parsed_type):
    """曾经同一条笼统错误的各类返回，现在有独立、可核对的形态。"""

    observations = []
    http = _http({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
    monkeypatch.setattr("app.modules.llm.client.httpx.post", lambda *args, **kwargs: http)
    with pytest.raises(LLMResponseError) as caught:
        _client(lambda shape, status: observations.append((shape, status))).complete_json(system_prompt="测试", user_payload={})

    shape = caught.value.diagnostic.response_shape
    assert shape.content_type == content_type
    assert shape.parse_outcome == outcome
    assert shape.parsed_json_type == parsed_type
    assert shape.content_chars == (len(content) if isinstance(content, str) else None)
    assert shape.http_body_bytes == len(http.content)
    assert observations == [(shape, 200)]
    assert SECRET not in shape.model_dump_json()
    assert "secret_field" not in shape.model_dump_json()


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        ({}, "CHOICES_MISSING"),
        ({"choices": []}, "CHOICES_EMPTY"),
        ({"choices": {}}, "CHOICES_TYPE_INVALID"),
        ({"choices": [None]}, "CHOICE_TYPE_INVALID"),
        ({"choices": [{}]}, "MESSAGE_MISSING"),
        ({"choices": [{"message": None}]}, "MESSAGE_TYPE_INVALID"),
        ({"choices": [{"message": {"reasoning_content": SECRET}}]}, "CONTENT_MISSING"),
    ],
)
def test_missing_or_wrong_envelope_fields_have_fixed_outcomes(monkeypatch, body, outcome):
    """缺字段、空列表和错误类型不能都记成模型JSON解析失败。"""

    monkeypatch.setattr("app.modules.llm.client.httpx.post", lambda *args, **kwargs: _http(body))
    with pytest.raises(LLMResponseError) as caught:
        _client().complete_json(system_prompt="测试", user_payload={})
    assert caught.value.diagnostic.response_shape.parse_outcome == outcome
    assert SECRET not in caught.value.diagnostic.model_dump_json()


@pytest.mark.parametrize("body,outcome", [(b"", "HTTP_BODY_EMPTY"), (b"<html>private</html>", "HTTP_JSON_INVALID"), (b"[]", "HTTP_TOP_LEVEL_NOT_OBJECT")])
def test_http_body_shape_is_recorded_even_before_model_content(monkeypatch, body, outcome):
    """HTTP外层为空、非JSON或顶层数组要定位在外层，不误归为content错误。"""

    observations = []
    http = httpx.Response(200, content=body, request=httpx.Request("POST", "https://example.invalid"))
    monkeypatch.setattr("app.modules.llm.client.httpx.post", lambda *args, **kwargs: http)
    with pytest.raises(LLMResponseError) as caught:
        _client(lambda shape, status: observations.append(shape)).complete_json(system_prompt="测试", user_payload={})
    shape = caught.value.diagnostic.response_shape
    assert shape.parse_outcome == outcome and shape.http_body_bytes == len(body)
    assert caught.value.diagnostic.stage == "HTTP_RESPONSE_PARSE"
    assert observations == [shape]


@pytest.mark.parametrize("finish,expected,truncated", [("length", "length", True), ("stop", "stop", False), (SECRET, "other", None)])
def test_finish_reason_and_reasoning_are_only_safe_metadata(monkeypatch, finish, expected, truncated):
    """识别长度结束与只有思考没有正文，未知finish_reason不能泄漏自由文字。"""

    http = _http({"choices": [{"finish_reason": finish, "message": {"content": "", "reasoning_content": SECRET, "tool_calls": [{"secret": SECRET}]}}]})
    monkeypatch.setattr("app.modules.llm.client.httpx.post", lambda *args, **kwargs: http)
    with pytest.raises(LLMResponseError) as caught:
        _client().complete_json(system_prompt="测试", user_payload={})
    shape = caught.value.diagnostic.response_shape
    assert shape.finish_reason == expected and shape.output_truncated is truncated
    assert shape.reasoning_content_type == "string" and shape.reasoning_chars == len(SECRET)
    assert shape.content_empty is True and shape.content_chars == 0
    assert shape.tool_calls_count == 1
    assert SECRET not in shape.model_dump_json()


@pytest.mark.parametrize("case", ["success", "empty_length", "schema_invalid"])
def test_evaluation_factory_logs_shape_with_run_context_and_failure_details(monkeypatch, tmp_path, case):
    """验证生产评估工厂到真实JSONL的链路，成功JSON和后置schema失败也能关联同一运行。"""

    content = json.dumps(response()) if case == "success" else "" if case == "empty_length" else "{}"
    http = _http({"choices": [{"finish_reason": "length" if case == "empty_length" else "stop", "message": {"content": content, "reasoning_content": SECRET}}]})
    monkeypatch.setattr("app.modules.llm.client.httpx.post", lambda *args, **kwargs: http)
    settings = _settings().model_copy(update={"log_dir": str(tmp_path)})
    client = build_semantic_checker(settings).client
    with log_context(agent_run_id="shape-run", user_id="test-user", conversation_id="test-conversation"):
        result = evaluate(client=client)
        log_shadow_result(result, settings=settings)
    text = next(tmp_path.glob("file-agent-*.log")).read_text()
    records = [json.loads(line) for line in text.splitlines()]
    shapes = [record for record in records if record["event"] == "evidence_evaluation.llm_response_shape"]
    assert len(shapes) == 1 and shapes[0]["agent_run_id"] == "shape-run"
    assert shapes[0]["http_status"] == 200
    expected = "CONTENT_EMPTY" if case == "empty_length" else "MODEL_JSON_OBJECT"
    assert shapes[0]["response_shape"]["parse_outcome"] == expected
    if case == "empty_length":
        shadow = next(row for row in records if row["event"] == "evidence_evaluation.shadow")
        assert shadow["error_details"][0]["response_shape"]["output_truncated"] is True
    elif case == "schema_invalid":
        assert result.semantic_check.error_codes == ["SEMANTIC_SCHEMA_INVALID"]
    else:
        assert result.evaluation_status == "COMPLETED"
    assert SECRET not in text and "private</html>" not in text


def test_observer_failure_does_not_change_successful_json_or_retry(monkeypatch):
    """日志观察器异常不能破坏正常回答，也不能额外请求模型。"""

    calls = []

    def post(*args, **kwargs):
        """记录实际HTTP次数。"""

        calls.append(kwargs)
        return _http({"choices": [{"message": {"content": '{"ok":true}'}}]})

    def broken_observer(shape, status):
        """模拟日志故障，不把其异常原文传播出去。"""

        raise RuntimeError(SECRET)

    monkeypatch.setattr("app.modules.llm.client.httpx.post", post)
    assert _client(broken_observer).complete_json(system_prompt="测试", user_payload={}) == {"ok": True}
    assert len(calls) == 1 and "response_observer" not in calls[0]["json"]


def test_shape_schema_rejects_previews_free_text_and_dynamic_keys():
    """字段白名单确保诊断接口无法塞入正文预览、任意结束原因或模型动态键名。"""

    for invalid in ({"content_preview": SECRET}, {"finish_reason": SECRET}, {"keys": [SECRET]}):
        with pytest.raises(ValidationError):
            ResponseShape.model_validate(invalid)
