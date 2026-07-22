"""Filesystem MCP 只读桥接层。

该模块只负责把受管相对路径转换为 MCP 可用的安全绝对路径，并把 MCP 输出脱敏。
MCP client 采用懒加载，避免未启用 MCP 时影响普通 API 启动和测试。
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Coroutine, TypeVar


T = TypeVar("T")

READ_TOOLS = {
    "list_allowed_directories",
    "list_directory",
    "list_directory_with_sizes",
    "search_files",
    "directory_tree",
    "get_file_info",
}


class MCPFilesystemError(RuntimeError):
    """Filesystem MCP 调用或路径校验失败。"""


class BackgroundAsyncRunner:
    """在同步 ToolRegistry 中运行异步 MCP 调用的后台事件循环。"""

    def __init__(self) -> None:
        """启动后台事件循环线程。"""

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="mcp-filesystem-loop",
            daemon=True,
        )
        self._thread.start()

    def _run_loop(self) -> None:
        """运行后台事件循环。"""

        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coroutine: Coroutine[Any, Any, T], timeout: int) -> T:
        """同步等待协程结果，并在超时时取消任务。"""

        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise MCPFilesystemError(f"Filesystem MCP call timed out after {timeout}s") from exc


class MCPFilesystemBridge:
    """Filesystem MCP 的安全包装。"""

    def __init__(self) -> None:
        """读取运行时配置，但不立即创建 MCP client。"""

        self.enabled = os.getenv("MCP_FILESYSTEM_ENABLED", "false").lower() == "true"
        self.root = Path(os.getenv("MCP_FILESYSTEM_ROOT", "/managed/workdata")).resolve()
        self.command = os.getenv("MCP_FILESYSTEM_COMMAND", "/usr/local/bin/mcp-server-filesystem")
        self.timeout = int(os.getenv("MCP_FILESYSTEM_TIMEOUT_SECONDS", "45"))
        self.max_output_chars = int(os.getenv("MCP_FILESYSTEM_MAX_OUTPUT_CHARS", "50000"))
        self._client: Any | None = None
        self._tools: dict[str, Any] | None = None
        self._tools_lock: asyncio.Lock | None = None

    def resolve_relative_path(self, value: str | None) -> str:
        """把用户可见的受管相对路径解析为 MCP 允许根内的绝对路径。"""

        original_value = (value or "").strip()
        normalized_value = original_value.replace("\\", "/")
        if normalized_value in {"", "."}:
            return str(self.root)

        # Windows 盘符路径在 Linux/macOS 上会被 PurePosixPath 误判成相对路径；路径校验必须同时
        # 使用 POSIX 和 Windows 语义，避免 ``C:\\...`` 被拼接到受管根下后绕过绝对路径拒绝规则。
        windows_path = PureWindowsPath(original_value)
        posix_path = PurePosixPath(normalized_value)
        relative = PurePosixPath(normalized_value.strip("/"))
        if posix_path.is_absolute() or windows_path.is_absolute() or bool(windows_path.drive):
            raise MCPFilesystemError("Invalid managed relative path")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise MCPFilesystemError("Invalid managed relative path")

        candidate = self.root.joinpath(*relative.parts).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise MCPFilesystemError("Path escapes managed root")
        return str(candidate)

    def sanitize_for_test(self, value: Any) -> Any:
        """测试入口：复用生产脱敏逻辑，不触发 MCP client。"""

        return self._sanitize(value)

    async def _get_tools(self) -> dict[str, Any]:
        """懒加载 MCP Tool 列表。"""

        if self._tools is not None:
            return self._tools
        if self._tools_lock is None:
            self._tools_lock = asyncio.Lock()
        async with self._tools_lock:
            if self._tools is None:
                client = self._get_client()
                tools = await client.get_tools()
                self._tools = {tool.name: tool for tool in tools}
        return self._tools

    def _get_client(self) -> Any:
        """懒加载 LangChain MCP client，避免未安装 adapter 时影响禁用场景。"""

        if self._client is not None:
            return self._client
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as exc:
            raise MCPFilesystemError("langchain-mcp-adapters is not installed") from exc
        self._client = MultiServerMCPClient(
            {
                "filesystem": {
                    "transport": "stdio",
                    "command": self.command,
                    "args": [str(self.root)],
                }
            }
        )
        return self._client

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用只读 MCP Tool。"""

        if not self.enabled:
            raise MCPFilesystemError("Filesystem MCP is disabled")
        if tool_name not in READ_TOOLS:
            raise MCPFilesystemError(f"MCP tool is not allowed: {tool_name}")
        tools = await self._get_tools()
        tool = tools.get(tool_name)
        if tool is None:
            raise MCPFilesystemError(f"MCP tool was not found: {tool_name}")
        result = await tool.ainvoke(arguments)
        return {
            "ok": True,
            "status": "COMPLETED",
            "tool_name": tool_name,
            "result": self._sanitize(result),
        }

    def call_sync(self, runner: BackgroundAsyncRunner, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """同步调用只读 MCP Tool。"""

        return runner.run(self._call(tool_name, arguments), timeout=self.timeout)

    def _sanitize(self, value: Any) -> Any:
        """脱敏 MCP 输出中的容器绝对路径，并限制长文本输出。"""

        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {str(key): self._sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            redacted = _redact_managed_root(value=value, root=self.root)
            if len(redacted) > self.max_output_chars:
                return redacted[: self.max_output_chars] + "\n...[truncated]"
            return redacted
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)


def _redact_managed_root(*, value: str, root: Path) -> str:
    """脱敏根路径并把用户可见逻辑路径统一为 POSIX 分隔符。

    MCP 在 Windows 上返回反斜杠路径，在 Linux 容器中返回正斜杠路径；普通用户只能看到稳定的
    ``workdata:/...`` 逻辑路径，不能看到任一平台的宿主机绝对路径。
    """

    normalized_value = value.replace("\\", "/")
    normalized_root = root.as_posix().rstrip("/")
    flags = re.IGNORECASE if os.name == "nt" else 0
    pattern = re.compile(re.escape(normalized_root), flags=flags)
    if pattern.search(normalized_value) is None:
        return value
    return pattern.sub("workdata:", normalized_value)


_runner: BackgroundAsyncRunner | None = None
_bridge: MCPFilesystemBridge | None = None
_singleton_lock = threading.Lock()


def get_mcp_filesystem() -> tuple[BackgroundAsyncRunner, MCPFilesystemBridge]:
    """返回进程内共享的 MCP runner 和 bridge。"""

    global _runner, _bridge
    with _singleton_lock:
        if _runner is None:
            _runner = BackgroundAsyncRunner()
        if _bridge is None:
            _bridge = MCPFilesystemBridge()
    return _runner, _bridge
