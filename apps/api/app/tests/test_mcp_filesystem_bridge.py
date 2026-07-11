"""Filesystem MCP 桥接层测试。"""

from __future__ import annotations

import pytest

from app.modules.agent.mcp_filesystem_bridge import MCPFilesystemBridge, MCPFilesystemError


def test_mcp_filesystem_bridge_resolves_only_managed_relative_paths(monkeypatch, tmp_path):
    """Bridge 只能解析受管根内的相对路径，并统一转换反斜杠。"""

    root = tmp_path / "workdata"
    root.mkdir()
    monkeypatch.setenv("MCP_FILESYSTEM_ROOT", str(root))
    bridge = MCPFilesystemBridge()

    assert bridge.resolve_relative_path(None) == str(root.resolve())
    assert bridge.resolve_relative_path("党办\\2026") == str((root / "党办" / "2026").resolve())

    with pytest.raises(MCPFilesystemError):
        bridge.resolve_relative_path("../secret")

    with pytest.raises(MCPFilesystemError):
        bridge.resolve_relative_path(str(tmp_path / "outside"))


def test_mcp_filesystem_bridge_sanitizes_paths_and_truncates_output(monkeypatch, tmp_path):
    """Bridge 输出不能暴露容器绝对路径，超长文本需要截断。"""

    root = tmp_path / "workdata"
    root.mkdir()
    monkeypatch.setenv("MCP_FILESYSTEM_ROOT", str(root))
    monkeypatch.setenv("MCP_FILESYSTEM_MAX_OUTPUT_CHARS", "20")
    bridge = MCPFilesystemBridge()

    sanitized = bridge.sanitize_for_test(
        {
            "path": str(root / "党办" / "通知.pdf"),
            "content": "x" * 80,
        }
    )

    assert sanitized["path"] == "workdata:/党办/通知.pdf"
    assert sanitized["content"].endswith("...[truncated]")

