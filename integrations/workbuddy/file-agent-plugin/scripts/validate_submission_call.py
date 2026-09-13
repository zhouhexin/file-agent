#!/usr/bin/env python3
"""在 WorkBuddy 调用 MCP 前校验附件桥接提交单引用。"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


_REF_PATTERN = re.compile(r"^wbsub_v1_[a-f0-9]{32}$")
_TOOL_PATTERN = re.compile(r"^mcp__.*file.agent.*__workbuddy_submission_ingest$")


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    """拒绝伪造、过期或跨会话提交单；无关工具不受影响。"""

    if not _TOOL_PATTERN.fullmatch(str(payload.get("tool_name") or "")):
        return {"continue": True}
    tool_input = payload.get("tool_input")
    session_id = payload.get("session_id")
    state_raw = (
        os.getenv("CODEBUDDY_PLUGIN_OPTION_BRIDGE_STATE_DIR")
        or os.getenv("CODEBUDDY_PLUGIN_DATA", "")
    ).strip()
    if not isinstance(tool_input, dict) or not isinstance(session_id, str) or not state_raw:
        return {"continue": False, "stopReason": "文件助手附件提交单校验失败，请重新发送附件和任务。"}
    submission_ref = tool_input.get("submission_ref")
    if not isinstance(submission_ref, str) or not _REF_PATTERN.fullmatch(submission_ref):
        return {"continue": False, "stopReason": "文件助手附件提交单引用无效，请重新发送附件和任务。"}
    path = Path(state_raw).expanduser() / "submissions" / f"{submission_ref}.json"
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("session_id") != session_id:
            raise ValueError
        created_at = manifest.get("created_at")
        if not isinstance(created_at, (int, float)) or time.time() - created_at > 600:
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        return {"continue": False, "stopReason": "文件助手附件提交单已失效，请重新发送附件和任务。"}
    return {"continue": True}


def main() -> int:
    """读取 PreToolUse 输入并输出 Hook 决策。"""

    try:
        # 与捕获 Hook 一样固定按 UTF-8 读取宿主管道，避免依赖 Windows 区域设置。
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        print(json.dumps({"continue": False, "stopReason": "文件助手 Hook 输入无效。"}))
        return 0
    print(json.dumps(validate(payload) if isinstance(payload, dict) else {"continue": True}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
