"""模型调用的安全诊断协议：仅输出白名单类别、阶段、异常类型与 HTTP 状态。"""

from __future__ import annotations

from typing import Literal, get_args

import httpx
from pydantic import BaseModel, ConfigDict, Field
from app.modules.llm.response_shape import ResponseShape


ErrorCategory = Literal[
    "NETWORK_ERROR", "TIMEOUT", "QUOTA_EXHAUSTED", "AUTHENTICATION_FAILED",
    "PERMISSION_DENIED", "RATE_LIMITED", "PROVIDER_ERROR", "REQUEST_REJECTED",
    "RESPONSE_FORMAT_INVALID", "SCHEMA_INVALID", "EVIDENCE_VALIDATION_FAILED",
    "CONFIGURATION_ERROR", "INTERNAL_ERROR", "UNKNOWN_LLM_ERROR",
]
CallStage = Literal[
    "LLM_REQUEST", "HTTP_RESPONSE_PARSE", "MODEL_RESPONSE_PARSE",
    "SEMANTIC_PREPARE", "SEMANTIC_SCHEMA_VALIDATION", "SEMANTIC_EVIDENCE_VALIDATION",
    "SEMANTIC_RESULT_MERGE", "SHADOW_SETUP",
    "INTENT_PLAN_VALIDATION",
    "ANSWER_SUPPORT_REPAIR",
]
SafeExceptionType = Literal[
    "ConnectError", "ReadError", "WriteError", "CloseError", "NetworkError",
    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
    "TimeoutError", "RemoteProtocolError", "LocalProtocolError", "ProtocolError",
    "ProxyError", "RequestError", "HTTPStatusError", "JSONDecodeError", "ValidationError",
    "InvalidURL", "UnsupportedProtocol",
    "EvaluationValidationError", "LLMResponseError", "LLMConfigurationError",
    "RuntimeError", "ValueError", "TypeError", "AttributeError", "IndexError", "KeyError", "Exception",
]
# 自定义异常类名也可能携带外部数据；不允许把任意 __class__.__name__ 写入日志。
_SAFE_EXCEPTION_TYPES = frozenset(get_args(SafeExceptionType))
_MESSAGES: dict[str, str] = {
    "NETWORK_ERROR": "模型接口连接失败或连接中断。",
    "TIMEOUT": "模型接口请求超时。",
    "QUOTA_EXHAUSTED": "模型账号额度、余额不足或计费受限。",
    "AUTHENTICATION_FAILED": "模型接口认证失败，请检查密钥配置。",
    "PERMISSION_DENIED": "模型接口拒绝访问，请检查账号或模型权限。",
    "RATE_LIMITED": "模型接口请求频率或并发达到限制。",
    "PROVIDER_ERROR": "模型服务端发生错误。",
    "REQUEST_REJECTED": "模型接口拒绝请求，请检查接口和参数兼容性。",
    "RESPONSE_FORMAT_INVALID": "模型接口返回空内容、无效 JSON 或不符合协议的响应结构。",
    "SCHEMA_INVALID": "模型返回的字段、类型或状态不符合受控协议。",
    "EVIDENCE_VALIDATION_FAILED": "模型结果未通过证据引用或事实定位校验。",
    "CONFIGURATION_ERROR": "模型连接配置不完整或无效。",
    "INTERNAL_ERROR": "评估程序发生内部异常。",
    "UNKNOWN_LLM_ERROR": "模型调用失败，现有信息不足以确定具体原因。",
}


class SafeErrorDiagnostic(BaseModel):
    """禁止任意响应内容、路径、密钥或自由异常文本进入诊断字段。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    category: ErrorCategory
    stage: CallStage
    exception_type: SafeExceptionType
    http_status: int | None = Field(default=None, ge=100, le=599)
    response_shape: ResponseShape | None = None

    def log_fields(self) -> dict:
        """中文说明只能由类别映射生成，不能采用接口或模型提供的自由文本。"""

        return {
            **self.model_dump(), "message": _MESSAGES[self.category],
            "response_shape": self.response_shape.log_fields() if self.response_shape is not None else None,
        }


def make_diagnostic(
    *, category: ErrorCategory, stage: CallStage, exc: Exception | None = None,
    http_status: int | None = None,
    response_shape: ResponseShape | None = None,
) -> SafeErrorDiagnostic:
    """只保存已验证的类别、阶段和异常类名，绝不调用 str(exc) 收集详情。"""

    name = type(exc).__name__ if exc is not None else "LLMResponseError"
    return SafeErrorDiagnostic(
        category=category, stage=stage,
        exception_type=name if name in _SAFE_EXCEPTION_TYPES else "Exception",
        http_status=http_status,
        response_shape=response_shape,
    )


def classify_http_error(response: httpx.Response) -> ErrorCategory:
    """仅在内存读取结构化错误码识别额度，不输出服务端消息或未知错误码。"""

    quota_codes = {
        "insufficient_quota", "quota_exceeded", "insufficient_balance", "balance_insufficient",
        "arrearage", "allocationquota.freetieronly", "allocationquota.free_tier_only",
    }
    try:
        data = response.json()
        error = data.get("error", data) if isinstance(data, dict) else None
        if isinstance(error, dict) and any(
            isinstance(error.get(key), str) and error[key].casefold() in quota_codes
            for key in ("code", "type")
        ):
            return "QUOTA_EXHAUSTED"
    except (ValueError, TypeError):
        # HTML、空响应或其他非 JSON 错误仍按 HTTP 状态分类，不解析其中的文字。
        pass
    status = response.status_code
    if status == 402:
        return "QUOTA_EXHAUSTED"
    if status == 401:
        return "AUTHENTICATION_FAILED"
    if status == 403:
        return "PERMISSION_DENIED"
    if status == 429:
        return "RATE_LIMITED"
    if status in {408, 504}:
        return "TIMEOUT"
    return "PROVIDER_ERROR" if status >= 500 else "REQUEST_REJECTED"
