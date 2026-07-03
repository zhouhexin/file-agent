"""OpenAI-compatible LLM 客户端。"""

from __future__ import annotations

import json
import re
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

        response = self._post_chat_completion(
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
        )
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("LLM 响应缺少 choices[0].message.content。") from exc
        parsed = _parse_json_object_from_content(content)
        if not isinstance(parsed, dict):
            raise LLMResponseError("LLM JSON 响应必须是对象。")
        return parsed

    def complete_text(self, *, system_prompt: str, user_payload: Dict[str, Any]) -> str:
        """调用模型并返回普通文本，供总结、讲解和问答类自然语言输出使用。"""

        response = self._post_chat_completion(
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "temperature": 0,
            }
        )
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("LLM 响应缺少 choices[0].message.content。") from exc
        text = str(content or "").strip()
        if not text:
            raise LLMResponseError("LLM 文本响应为空。")
        return text

    def complete_multimodal_json(self, *, system_prompt: str, text: str, image_url: str) -> Dict[str, Any]:
        """调用支持 image_url 的 OpenAI-compatible Chat Completions 并解析 JSON。"""

        response = self._post_chat_completion(
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": text},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    },
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
        )
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("LLM 响应缺少 choices[0].message.content。") from exc
        parsed = _parse_json_object_from_content(content)
        if not isinstance(parsed, dict):
            raise LLMResponseError("LLM JSON 响应必须是对象。")
        return parsed

    def _post_chat_completion(self, *, payload: Dict[str, Any]) -> httpx.Response:
        """统一调用 Chat Completions，并把网络和 HTTP 错误收敛成 LLMResponseError。"""

        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            body = exc.response.text[:300]
            raise LLMResponseError(f"LLM HTTP 请求失败：status={status_code}，model={self.model}，body={body}") from exc
        except httpx.RequestError as exc:
            raise LLMResponseError(f"LLM 请求失败：{exc.__class__.__name__}，model={self.model}，message={exc}") from exc


def _parse_json_object_from_content(content: str) -> Dict[str, Any]:
    """从模型文本中解析 JSON 对象，兼容 markdown 代码块和前后说明文本。"""

    candidates = [content.strip()]
    fenced_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(match.strip() for match in fenced_matches)
    object_match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if object_match:
        candidates.append(object_match.group(0).strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        raise LLMResponseError("LLM JSON 响应必须是对象。")
    raise LLMResponseError("LLM 响应不是合法 JSON。")
