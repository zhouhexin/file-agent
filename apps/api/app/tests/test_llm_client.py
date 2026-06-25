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
