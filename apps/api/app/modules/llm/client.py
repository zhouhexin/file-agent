"""OpenAI-compatible LLM 客户端。"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict

import httpx

from app.modules.llm.diagnostics import (
    CallStage, SafeErrorDiagnostic, classify_http_error, make_diagnostic,
)
from app.modules.llm.response_shape import ResponseShape, inspect_response_shape, json_kind


class LLMConfigurationError(RuntimeError):
    """LLM 启用但配置不完整时抛出。"""


class LLMResponseError(RuntimeError):
    """模型调用、响应解析或受控校验失败时抛出，兼容既有兜底入口。"""

    def __init__(self, message: str, *, diagnostic: SafeErrorDiagnostic | None = None) -> None:
        """兼容原有异常调用方式；安全诊断独立携带，不从异常文字反向猜测类别。"""

        super().__init__(message)
        self.diagnostic = diagnostic


class OpenAICompatibleLLMClient:
    """调用 OpenAI-compatible Chat Completions 接口。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int,
        max_retries: int = 0,
        retry_delay_seconds: float = 0,
        response_observer: Callable[[ResponseShape, int | None], None] | None = None,
    ) -> None:
        """保存模型调用配置。"""

        if not api_key or not base_url or not model:
            raise LLMConfigurationError("LLM_ENABLED=true 时必须配置 LLM_API_KEY、LLM_BASE_URL 和 LLM_CHAT_MODEL。")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, min(2, int(max_retries)))
        self.retry_delay_seconds = max(0.0, min(5.0, float(retry_delay_seconds)))
        self.response_observer = response_observer

    def complete_json(self, *, system_prompt: str, user_payload: Dict[str, Any]) -> Dict[str, Any]:
        """调用模型并解析 JSON 对象响应。"""

        parsed, _usage = self.complete_json_with_usage(
            system_prompt=system_prompt, user_payload=user_payload,
        )
        return parsed

    def complete_json_with_usage(
        self, *, system_prompt: str, user_payload: Dict[str, Any],
        max_output_tokens: int | None = None,
    ) -> tuple[Dict[str, Any], Dict[str, int]]:
        """供只读评估限制输出并统计成本；旧调用的载荷与返回契约保持一致。"""

        output_limit = (
            {"max_tokens": max_output_tokens} if max_output_tokens is not None else {}
        )
        response = self._post_chat_completion(
            payload={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
                **output_limit,
            }
        )
        data, parsed = self._read_json_completion(response)
        usage = data.get("usage")
        # 第三方接口可能缺失或返回非标准 usage；统计缺失不能破坏旧回答路径。
        safe_usage = {}
        if isinstance(usage, dict):
            for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
                value = usage.get(source)
                if type(value) is int and value >= 0:
                    safe_usage[target] = value
        return parsed, safe_usage

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
        data = _response_json(response)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise _invalid_response(
                "LLM 响应缺少 choices[0].message.content。", response=response, exc=exc,
            ) from exc
        text = str(content or "").strip()
        if not text:
            raise _invalid_response("LLM 文本响应为空。", response=response)
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
        _data, parsed = self._read_json_completion(response)
        return parsed

    def _read_json_completion(self, response: httpx.Response) -> tuple[dict, dict]:
        """成功和格式失败均观察安全形态，保持原JSON对象返回契约与解析兼容性。"""

        try:
            data = _response_json(response)
            shape = inspect_response_shape(data, http_body_bytes=_http_body_bytes(response))
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise _invalid_response(
                    "LLM 响应缺少 choices[0].message.content。", response=response, exc=exc,
                    response_shape=shape,
                ) from exc
            parsed = _parse_json_object_from_content(
                content, http_status=_http_status(response), response_shape=shape,
            )
        except LLMResponseError as exc:
            if isinstance(exc.diagnostic, SafeErrorDiagnostic) and exc.diagnostic.response_shape is not None:
                self._observe_response_shape(exc.diagnostic.response_shape, _http_status(response))
            raise
        self._observe_response_shape(shape.model_copy(update={
            "parsed_json_type": "object", "parse_outcome": "MODEL_JSON_OBJECT",
        }), _http_status(response))
        return data, parsed

    def _observe_response_shape(self, shape: ResponseShape, http_status: int | None) -> None:
        """仅向后端观察器传递类型与计数；诊断写入失败不得改变模型结果或追加重试。"""

        if self.response_observer is not None:
            try:
                self.response_observer(shape, http_status)
            except Exception:
                pass

    def _post_chat_completion(self, *, payload: Dict[str, Any]) -> httpx.Response:
        """统一调用 Chat Completions，并把网络和 HTTP 错误收敛成 LLMResponseError。"""

        for attempt in range(self.max_retries + 1):
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
                if status_code in {408, 429, 500, 502, 503, 504} and attempt < self.max_retries:
                    self._wait_before_retry()
                    continue
                diagnostic = make_diagnostic(
                    category=classify_http_error(exc.response), stage="LLM_REQUEST",
                    exc=exc, http_status=status_code,
                )
                # 接口错误体可能回显密钥、请求正文或文件内容；异常文字也只用固定说明。
                raise LLMResponseError(
                    f"LLM HTTP 请求失败：status={status_code}；{diagnostic.log_fields()['message']}",
                    diagnostic=diagnostic,
                ) from exc
            except httpx.InvalidURL as exc:
                diagnostic = make_diagnostic(category="CONFIGURATION_ERROR", stage="LLM_REQUEST", exc=exc)
                raise LLMResponseError(
                    f"LLM 请求失败：{diagnostic.log_fields()['message']}", diagnostic=diagnostic,
                ) from exc
            except httpx.RequestError as exc:
                retryable = isinstance(
                    exc,
                    (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError),
                )
                if retryable and attempt < self.max_retries:
                    self._wait_before_retry()
                    continue
                diagnostic = make_diagnostic(
                    category=(
                        "TIMEOUT" if isinstance(exc, httpx.TimeoutException)
                        else "CONFIGURATION_ERROR" if isinstance(exc, httpx.UnsupportedProtocol)
                        else "NETWORK_ERROR"
                    ),
                    stage="LLM_REQUEST", exc=exc,
                )
                raise LLMResponseError(
                    f"LLM 请求失败：{diagnostic.log_fields()['message']}", diagnostic=diagnostic,
                ) from exc
        raise AssertionError("LLM retry loop exited without a response")

    def _wait_before_retry(self) -> None:
        """仅对显式启用重试的调用做短暂退避，避免放大网关瞬时故障。"""

        if self.retry_delay_seconds:
            time.sleep(self.retry_delay_seconds)


def _parse_json_object_from_content(
    content: str, *, http_status: int | None = None, response_shape: ResponseShape | None = None,
) -> Dict[str, Any]:
    """从模型文本中解析 JSON 对象，兼容 markdown 代码块和前后说明文本。"""

    shape = response_shape or ResponseShape(content_type=json_kind(content))
    if not isinstance(content, str):
        raise _invalid_response("LLM JSON 内容必须是文本。", http_status=http_status,
                                response_shape=shape.model_copy(update={"parse_outcome": "CONTENT_NULL" if content is None else "CONTENT_NON_TEXT"}))
    if not content.strip():
        raise _invalid_response("LLM 响应不是合法 JSON。", http_status=http_status,
                                response_shape=shape.model_copy(update={"parse_outcome": "CONTENT_EMPTY"}))
    candidates = [content.strip()]
    fenced_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(match.strip() for match in fenced_matches)
    object_match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if object_match:
        candidates.append(object_match.group(0).strip())

    last_error = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed
        raise _invalid_response("LLM JSON 响应必须是对象。", http_status=http_status, response_shape=shape.model_copy(update={
            "parsed_json_type": json_kind(parsed), "parse_outcome": "MODEL_JSON_NOT_OBJECT",
        }))
    raise _invalid_response("LLM 响应不是合法 JSON。", http_status=http_status, exc=last_error,
                            response_shape=shape.model_copy(update={"parsed_json_type": "unparseable", "parse_outcome": "MODEL_JSON_INVALID"}))


def _response_json(response: httpx.Response) -> Dict[str, Any]:
    """把空响应或非 JSON 响应统一收敛为可降级的 LLMResponseError。"""

    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raw_content = getattr(response, "content", b"")
        if not raw_content:
            raise _invalid_response(
                "LLM HTTP 响应为空。", response=response, stage="HTTP_RESPONSE_PARSE", exc=exc,
                response_shape=inspect_response_shape(http_body_bytes=_http_body_bytes(response)).model_copy(update={"parse_outcome": "HTTP_BODY_EMPTY"}),
            ) from exc
        raise _invalid_response(
            "LLM HTTP 响应不是合法 JSON。", response=response, stage="HTTP_RESPONSE_PARSE", exc=exc,
            response_shape=inspect_response_shape(http_body_bytes=_http_body_bytes(response)).model_copy(update={"parse_outcome": "HTTP_JSON_INVALID"}),
        ) from exc
    if not isinstance(data, dict):
        raise _invalid_response(
            "LLM HTTP JSON 响应必须是对象。", response=response, stage="HTTP_RESPONSE_PARSE",
            response_shape=inspect_response_shape(data, http_body_bytes=_http_body_bytes(response)).model_copy(update={"parse_outcome": "HTTP_TOP_LEVEL_NOT_OBJECT"}),
        )
    return data


def _http_status(response: Any) -> int | None:
    """真实 HTTP 响应才记录状态；测试替身或未知传输不能猜测为 200。"""

    value = getattr(response, "status_code", None)
    return value if type(value) is int and 100 <= value <= 599 else None


def _http_body_bytes(response: Any) -> int | None:
    """仅统计已读取HTTP字节数；不把正文转换为字符串或复制到日志。"""

    content = getattr(response, "content", None)
    return len(content) if isinstance(content, bytes) else None


def _invalid_response(
    message: str, *, response: Any = None, http_status: int | None = None,
    stage: CallStage = "MODEL_RESPONSE_PARSE", exc: Exception | None = None,
    response_shape: ResponseShape | None = None,
) -> LLMResponseError:
    """构造安全的格式错误；调用方消息必须为固定文本，不能回显模型输出。"""

    return LLMResponseError(message, diagnostic=make_diagnostic(
        category="RESPONSE_FORMAT_INVALID", stage=stage, exc=exc,
        http_status=_http_status(response) if response is not None else http_status,
        response_shape=response_shape,
    ))
