"""WorkBuddy 标准连接器包的静态契约测试。"""

from __future__ import annotations

import json
import re
from pathlib import Path


def test_workbuddy_connector_package_is_complete_and_safe() -> None:
    """连接器必须符合 MCP + Skill 结构，且凭证只使用表单占位符。"""

    root = Path(__file__).resolve().parents[3] / "integrations" / "workbuddy" / "file-agent-connector"
    meta = json.loads((root / "connector-meta.json").read_text(encoding="utf-8"))
    mcp = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    schema = json.loads((root / "token-schema.json").read_text(encoding="utf-8"))

    assert meta["source"] == "file-agent-local"
    assert meta["name_zh"] == "文件助手"
    assert meta["type"] == "mcp"
    assert meta["auth_mode"] == "token"
    assert len(mcp["mcpServers"]) == 1
    server = mcp["mcpServers"]["file-agent"]
    assert server["disabledTools"] == ["workbuddy_attachment_ingest", "workbuddy_submission_ingest"]
    field_keys = {field["key"] for field in schema["fields"]}
    placeholders = set(re.findall(r"\$\{([A-Z0-9_]+)\}", json.dumps(server)))
    assert placeholders <= field_keys
    token_field = next(field for field in schema["fields"] if field["key"] == "FILE_AGENT_ACCESS_TOKEN")
    assert token_field["type"] == "password"
    assert (root / "icon.svg").is_file()
    assert (root / "skills" / "file-agent" / "SKILL.md").is_file()
    assert "请用文件助手" in (root / "skills" / "file-agent" / "SKILL.md").read_text(encoding="utf-8")
