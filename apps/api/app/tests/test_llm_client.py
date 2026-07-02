"""LLM 客户端兼容性测试。"""

from __future__ import annotations

from app.modules.llm.client import OpenAICompatibleLLMClient


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
        timeout_seconds=30,
    )

    result = client.complete_json(system_prompt="system", user_payload={"message": "总结"})

    assert result["intent"] == "SUMMARIZE_DOCUMENTS"


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
        timeout_seconds=30,
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
