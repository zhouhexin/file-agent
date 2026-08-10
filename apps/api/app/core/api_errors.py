"""统一 File Agent HTTP API 的错误响应边界。

业务模块仍可使用 FastAPI ``HTTPException`` 表达状态码和安全提示；本模块负责在应用边界统一转换，
避免前端同时处理 ``detail``、裸字符串和多种自定义错误结构。未捕获异常只返回通用提示和 request_id，
不得把堆栈、服务器路径或内部异常文本暴露给普通用户。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException


HTTP_ERROR_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    410: "GONE",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


class ApiErrorDetail(BaseModel):
    """前端和运维可以稳定解析的错误主体。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: Any | None = None
    request_id: str | None = None


class ApiErrorResponse(BaseModel):
    """所有 JSON API 错误使用的顶层 Envelope。"""

    model_config = ConfigDict(extra="forbid")

    error: ApiErrorDetail


def register_api_error_handlers(app: FastAPI) -> None:
    """注册统一异常处理器；调用方只应在应用初始化时注册一次。"""

    app.add_exception_handler(HTTPException, _http_exception_handler)
    # 路由未命中等框架级错误直接使用 Starlette 基类，必须覆盖同一个 Envelope。
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """把现有 ``HTTPException(detail=...)`` 兼容转换为统一错误结构。"""

    assert isinstance(exc, StarletteHTTPException)
    code, message, details = _normalize_http_detail(
        status_code=exc.status_code,
        detail=exc.detail,
    )
    return _error_response(
        request=request,
        status_code=exc.status_code,
        code=code,
        message=message,
        details=details,
        headers=exc.headers,
    )


async def _validation_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """返回结构化参数错误，但不回显请求正文或文件内容。"""

    assert isinstance(exc, RequestValidationError)
    details = [
        {
            "location": [str(part) for part in item.get("loc", ())],
            "message": str(item.get("msg") or "参数无效"),
            "type": str(item.get("type") or "validation_error"),
        }
        for item in exc.errors()
    ]
    return _error_response(
        request=request,
        status_code=422,
        code="VALIDATION_ERROR",
        message="请求参数校验失败。",
        details=details,
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """隐藏未捕获异常细节，并把 request_id 留给日志关联。"""

    return internal_error_response(request)


def internal_error_response(request: Request) -> JSONResponse:
    """供最外层请求中间件复用统一 500 响应，确保异常路径也带 request_id。"""

    return _error_response(
        request=request,
        status_code=500,
        code="INTERNAL_ERROR",
        message="服务器内部错误，请稍后重试。",
    )


def _normalize_http_detail(
    *,
    status_code: int,
    detail: Any,
) -> tuple[str, str, Any | None]:
    """兼容字符串和既有结构化 detail，不允许业务代码绕过统一 Envelope。"""

    default_code = HTTP_ERROR_CODES.get(status_code, f"HTTP_{status_code}")
    if isinstance(detail, dict):
        raw_error = detail.get("error") if isinstance(detail.get("error"), dict) else detail
        code = str(raw_error.get("code") or default_code)
        message = str(raw_error.get("message") or "请求失败。")
        details = raw_error.get("details")
        if details is None:
            remaining = {
                key: value
                for key, value in raw_error.items()
                if key not in {"code", "message", "request_id"}
            }
            details = remaining or None
        return code, message, details
    return default_code, str(detail or "请求失败。"), None


def _error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """构造最终 JSON 响应；request_id 只用于追踪，不泄露运行上下文。"""

    request_id = getattr(request.state, "request_id", None)
    payload = ApiErrorResponse(
        error=ApiErrorDetail(
            code=code,
            message=message,
            details=details,
            request_id=request_id,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json", exclude_none=True),
        headers=headers,
    )
