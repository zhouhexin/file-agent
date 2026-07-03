"""LLM 客户端兼容性测试。"""

from __future__ import annotations

import pytest
import httpx

from app.core.config import Settings
from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient
from app.modules.llm.service import LLMIntentService


class _FakeResponse:
    """模拟 OpenAI-compatible 响应对象。"""

    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        """测试响应永远成功。"""

    def json(self) -> dict:
        """返回包含模型文本的响应结构。"""

        return {"choices": [{"message": {"content": self.content}}]}


def test_llm_client_extracts_json_from_markdown_fence(monkeypatch):
    """模型返回 markdown JSON 代码块时，客户端应提取其中的 JSON 对象。"""

    def fake_post(*args, **kwargs):
        """返回带 markdown fence 的模型内容。"""

        return _FakeResponse(
            """```json
{"intent":"SUMMARIZE_DOCUMENTS","user_goal":"总结文件"}
```"""
        )

    monkeypatch.setattr("app.modules.llm.client.httpx.post", fake_post)
    client = OpenAICompatibleLLMClient(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test-model",
        timeout_seconds=180,
    )

    result = client.complete_json(system_prompt="system", user_payload={"message": "总结"})

    assert result["intent"] == "SUMMARIZE_DOCUMENTS"


def test_llm_client_complete_text_does_not_force_json_format(monkeypatch):
    """普通文本调用不应强制 response_format，避免总结类回答被 JSON 解析约束中断。"""

    captured_payload = {}

    def fake_post(*args, **kwargs):
        """记录请求体并返回普通中文文本。"""

        captured_payload.update(kwargs["json"])
        return _FakeResponse("这是一段普通中文总结。")

    monkeypatch.setattr("app.modules.llm.client.httpx.post", fake_post)
    client = OpenAICompatibleLLMClient(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test-model",
        timeout_seconds=180,
    )

    result = client.complete_text(system_prompt="system", user_payload={"message": "总结"})

    assert result == "这是一段普通中文总结。"
    assert "response_format" not in captured_payload


def test_llm_client_wraps_remote_protocol_error(monkeypatch):
    """模型服务断开连接时，客户端必须转成可兜底的 LLMResponseError。"""

    def fake_post(*args, **kwargs):
        """模拟千问兼容接口断开连接。"""

        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    monkeypatch.setattr("app.modules.llm.client.httpx.post", fake_post)
    client = OpenAICompatibleLLMClient(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="qwen-plus",
        timeout_seconds=180,
    )

    with pytest.raises(LLMResponseError) as exc_info:
        client.complete_text(system_prompt="system", user_payload={"message": "总结"})

    assert "LLM 请求失败" in str(exc_info.value)


def test_llm_intent_service_rejects_invalid_schema_response():
    """LLM 意图输出缺少必填字段时，应转成可被 Planner 兜底捕获的 LLMResponseError。"""

    class InvalidIntentClient:
        """模拟模型返回不符合 UserIntentPlan 的 JSON。"""

        def complete_json(self, *, system_prompt: str, user_payload: dict) -> dict:
            """返回用户现场出现过的 error 对象。"""

            return {"error": "Field required"}

    service = LLMIntentService(
        settings=Settings(database_url="postgresql+psycopg2://user:pass@example.com/db", llm_enabled=True),
        client=InvalidIntentClient(),
    )

    with pytest.raises(LLMResponseError) as exc_info:
        service.understand_user_request(
            message="总结这个文件",
            attachments=[{"document_id": "doc-1"}],
            context_documents=[],
        )

    assert "LLM 意图响应不符合 schema" in str(exc_info.value)


def test_llm_client_sends_multimodal_image_url_payload(monkeypatch):
    """多模态 JSON 调用必须按 OpenAI-compatible image_url 格式发送图片。"""

    captured_payload = {}

    def fake_post(*args, **kwargs):
        """记录请求体并返回 OCR JSON。"""

        captured_payload.update(kwargs["json"])
        return _FakeResponse('{"text":"识别文本","confidence":0.9,"warnings":[]}')

    monkeypatch.setattr("app.modules.llm.client.httpx.post", fake_post)
    client = OpenAICompatibleLLMClient(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="MiniMax-M3",
        timeout_seconds=180,
    )

    result = client.complete_multimodal_json(
        system_prompt="system",
        text="ocr",
        image_url="data:image/png;base64,abc",
    )

    user_content = captured_payload["messages"][1]["content"]
    assert captured_payload["model"] == "MiniMax-M3"
    assert user_content[0] == {"type": "text", "text": "ocr"}
    assert user_content[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
    assert result["text"] == "识别文本"
