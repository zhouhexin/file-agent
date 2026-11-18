"""只描述模型返回的类型、长度与固定结构状态，不保留正文、思考内容或任意字段名。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


JsonKind = Literal["missing", "null", "object", "array", "string", "number", "boolean", "unknown"]
ParseOutcome = Literal[
    "HTTP_BODY_EMPTY", "HTTP_JSON_INVALID", "HTTP_TOP_LEVEL_NOT_OBJECT",
    "CHOICES_MISSING", "CHOICES_EMPTY", "CHOICES_TYPE_INVALID", "CHOICE_TYPE_INVALID",
    "MESSAGE_MISSING", "MESSAGE_TYPE_INVALID", "CONTENT_MISSING", "CONTENT_NULL",
    "CONTENT_EMPTY", "CONTENT_NON_TEXT", "MODEL_JSON_INVALID", "MODEL_JSON_NOT_OBJECT", "MODEL_JSON_OBJECT",
]
_MISSING = object()
_FINISH_REASONS = frozenset({"stop", "length", "max_tokens", "content_filter", "tool_calls", "function_call"})
_OUTCOME_MESSAGES = {
    "HTTP_BODY_EMPTY": "HTTP响应正文为空。",
    "HTTP_JSON_INVALID": "HTTP响应正文无法解析为JSON。",
    "HTTP_TOP_LEVEL_NOT_OBJECT": "HTTP层JSON顶层不是对象。",
    "CHOICES_MISSING": "响应缺少choices字段。",
    "CHOICES_EMPTY": "choices是空数组。",
    "CHOICES_TYPE_INVALID": "choices不是数组。",
    "CHOICE_TYPE_INVALID": "choices第一项不是对象。",
    "MESSAGE_MISSING": "choices第一项缺少message。",
    "MESSAGE_TYPE_INVALID": "message不是对象。",
    "CONTENT_MISSING": "message缺少content字段。",
    "CONTENT_NULL": "message.content为null。",
    "CONTENT_EMPTY": "message.content为空字符串或只有空白。",
    "CONTENT_NON_TEXT": "message.content不是字符串。",
    "MODEL_JSON_INVALID": "content非空，但无法解析为JSON对象。",
    "MODEL_JSON_NOT_OBJECT": "content可解析为JSON，但顶层不是对象。",
    "MODEL_JSON_OBJECT": "content已解析为JSON对象，仍须通过后续字段和证据校验。",
}


class ResponseShape(BaseModel):
    """字段均为白名单值或计数；禁止添加响应预览和自由字段，防止诊断泄漏内容。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    http_body_bytes: int | None = Field(default=None, ge=0)
    envelope_type: JsonKind = "missing"
    choices_type: JsonKind = "missing"
    choices_count: int | None = Field(default=None, ge=0)
    first_choice_type: JsonKind = "missing"
    message_type: JsonKind = "missing"
    content_type: JsonKind = "missing"
    content_empty: bool | None = None
    content_chars: int | None = Field(default=None, ge=0)
    reasoning_content_type: JsonKind = "missing"
    reasoning_chars: int | None = Field(default=None, ge=0)
    tool_calls_count: int | None = Field(default=None, ge=0)
    finish_reason: Literal["missing", "null", "other", "stop", "length", "max_tokens", "content_filter", "tool_calls", "function_call"] = "missing"
    output_truncated: bool | None = None
    parsed_json_type: JsonKind | Literal["not_attempted", "unparseable"] = "not_attempted"
    parse_outcome: ParseOutcome | None = None

    def log_fields(self) -> dict:
        """中文形态说明仅由固定状态码生成，不采用模型提供的描述。"""

        return {**self.model_dump(), "summary": _OUTCOME_MESSAGES.get(self.parse_outcome, "尚未解析模型内容。")}


def json_kind(value: Any) -> JsonKind:
    """只返回协议类型名；未知对象也不输出其自定义类名。"""

    if value is _MISSING:
        return "missing"
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number" if type(value) in (int, float) else "unknown"


def inspect_response_shape(data: Any = _MISSING, *, http_body_bytes: int | None = None) -> ResponseShape:
    """逐层描述固定 Chat Completions 字段，不输出键列表、字段值或任何文本片段。"""

    fields: dict[str, Any] = {"envelope_type": json_kind(data), "http_body_bytes": http_body_bytes}
    if not isinstance(data, dict):
        return ResponseShape(**fields)
    choices = data.get("choices", _MISSING)
    fields["choices_type"] = json_kind(choices)
    if not isinstance(choices, list):
        fields["parse_outcome"] = "CHOICES_MISSING" if choices is _MISSING else "CHOICES_TYPE_INVALID"
        return ResponseShape(**fields)
    fields["choices_count"] = len(choices)
    if not choices:
        return ResponseShape(**fields, parse_outcome="CHOICES_EMPTY")
    choice = choices[0]
    fields["first_choice_type"] = json_kind(choice)
    if not isinstance(choice, dict):
        return ResponseShape(**fields, parse_outcome="CHOICE_TYPE_INVALID")
    finish = choice.get("finish_reason", _MISSING)
    fields["finish_reason"] = (
        "missing" if finish is _MISSING else "null" if finish is None
        else finish if isinstance(finish, str) and finish in _FINISH_REASONS else "other"
    )
    fields["output_truncated"] = (
        None if fields["finish_reason"] in {"missing", "null", "other"}
        else fields["finish_reason"] in {"length", "max_tokens"}
    )
    message = choice.get("message", _MISSING)
    fields["message_type"] = json_kind(message)
    if not isinstance(message, dict):
        fields["parse_outcome"] = "MESSAGE_MISSING" if message is _MISSING else "MESSAGE_TYPE_INVALID"
        return ResponseShape(**fields)
    content = message.get("content", _MISSING)
    fields["content_type"] = json_kind(content)
    fields["content_empty"] = (
        None if content is _MISSING else not content.strip() if isinstance(content, str)
        else len(content) == 0 if isinstance(content, (list, dict)) else content is None
    )
    fields["content_chars"] = len(content) if isinstance(content, str) else None
    reasoning = message.get("reasoning_content", _MISSING)
    fields["reasoning_content_type"] = json_kind(reasoning)
    fields["reasoning_chars"] = len(reasoning) if isinstance(reasoning, str) else None
    tools = message.get("tool_calls", _MISSING)
    fields["tool_calls_count"] = len(tools) if isinstance(tools, list) else None
    if content is _MISSING:
        fields["parse_outcome"] = "CONTENT_MISSING"
    elif content is None:
        fields["parse_outcome"] = "CONTENT_NULL"
    elif not isinstance(content, str):
        fields["parse_outcome"] = "CONTENT_NON_TEXT"
    elif not content.strip():
        fields["parse_outcome"] = "CONTENT_EMPTY"
    return ResponseShape(**fields)
