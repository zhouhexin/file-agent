"""OpenAI-compatible LLM 客户端。"""

from __future__ import annotations

import json
from typing import Any, Dict

import httpx


class LLMConfigurationError(RuntimeError):
    """LLM 启用但配置不完整时抛出。"""


class LLMResponseError(RuntimeError):
    """LLM 返回非预期结构时抛出。"""


class OpenAICompatibleLLMClient:
    """调用 OpenAI-compatible Chat Completions 接口。"""

    def __init__(self, *, api_key: str, base_url: str, model: str, timeout_seconds: int) -> None:
        """保存模型调用配置。"""

        if not api_key or not base_url or not model:
            raise LLMConfigurationError("LLM_ENABLED=true 时必须配置 LLM_API_KEY、LLM_BASE_URL 和 LLM_CHAT_MODEL。")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def complete_json(self, *, system_prompt: str, user_payload: Dict[str, Any]) -> Dict[str, Any]:
        """调用模型并解析 JSON 对象响应。"""

        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("LLM 响应缺少 choices[0].message.content。") from exc
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMResponseError("LLM 响应不是合法 JSON。") from exc
        if not isinstance(parsed, dict):
            raise LLMResponseError("LLM JSON 响应必须是对象。")
        return parsed
