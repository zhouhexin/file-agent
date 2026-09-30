"""在 FastAPI 解析 multipart 前限制 Excel 回传请求的实际网络字节数。"""

from __future__ import annotations

import re

from starlette.responses import JSONResponse

from app.core.logging import new_request_id


class ExcelWritebackBodyLimitMiddleware:
    """只约束新回传端点，不改变其他现有上传入口的请求契约。"""

    def __init__(self, app, *, max_body_bytes: int = 36 * 1024 * 1024):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope.get("type") != "http" or scope.get("method") != "POST"
                or not (re.fullmatch(r"/api/excel-writeback/(?:corrections/)?tasks/[^/]+/publish", path)
                        or path == "/api/excel-writeback/manual-revisions/publish")):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore") or new_request_id()

        async def reject():
            response = JSONResponse(
                {"error": {"code": "WRITEBACK_REQUEST_TOO_LARGE",
                           "message": "Excel 回传请求体超过安全上限。", "request_id": request_id}},
                status_code=413,
                headers={"X-Request-ID": request_id},
            )
            await response(scope, receive, send)

        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_body_bytes:
            await reject()
            return
        consumed = 0
        exceeded = False
        started = False
        pending_start = None

        async def bounded_receive():
            nonlocal consumed, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_body_bytes:
                    # Starlette multipart 解析器会吞掉 receive 异常并将它误报为 400；
                    # 标记超限后向内层发 disconnect，最终由本层统一产生 413。
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def tracked_send(message):
            nonlocal started, pending_start
            if exceeded:
                return
            if message.get("type") == "http.response.start":
                pending_start = message
                return
            if pending_start is not None:
                await send(pending_start)
                started = True
                pending_start = None
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not started:
            await reject()
        elif pending_start is not None:
            await send(pending_start)
