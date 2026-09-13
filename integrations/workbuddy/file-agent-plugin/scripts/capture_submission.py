#!/usr/bin/env python3
"""把本轮已验证的 WorkBuddy 附件写成可信提交单。

仅支持已在本机 transcript 中验证过的 ``image_blob_ref``。脚本不读取附件正文，
不把文件路径或原始文件名注入模型上下文；未知附件块一律不导入。
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any


def _capture_unavailable_result() -> dict[str, Any]:
    """明确阻断附件任务的目录回退，同时不影响用户单独授权的目录导入。"""

    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "FILE_AGENT_ATTACHMENT_BRIDGE_UNAVAILABLE。当前轮没有生成可信附件提交单。"
                "如果用户要求导入、归档、解析、OCR、整理或分类本轮附件，必须说明当前附件无法安全提交并停止；"
                "不得用 Bash、cp、Copy-Item 或其他文件工具把附件复制到工作区或授权目录，"
                "也不得调用 file_batch_ingest 冒充附件桥接成功。"
                "只有用户另外明确指定已授权目录进行批量导入时，才允许正常使用 file_batch_ingest。"
            ),
        },
    }


def _workbuddy_home() -> Path:
    """读取插件配置的 WorkBuddy 数据目录。"""

    configured = os.getenv("CODEBUDDY_PLUGIN_OPTION_WORKBUDDY_HOME") or os.getenv(
        "FILE_AGENT_WORKBUDDY_HOME"
    ) or str(Path.home() / ".workbuddy")
    return Path(configured).expanduser().resolve()


def _state_dir() -> Path | None:
    """取得插件私有持久化目录；缺失时保持只读且不创建提交单。"""

    raw = (
        os.getenv("CODEBUDDY_PLUGIN_OPTION_BRIDGE_STATE_DIR")
        or os.getenv("CODEBUDDY_PLUGIN_DATA", "")
    ).strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        return None
    candidate.mkdir(parents=True, exist_ok=True)
    if candidate.is_symlink() or not candidate.is_dir():
        return None
    return candidate.resolve()


def _safe_transcript_path(*, raw_path: object, workbuddy_home: Path) -> Path | None:
    """只接受 WorkBuddy projects 根内的普通 JSONL transcript。"""

    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        projects_root = (workbuddy_home / "projects").resolve(strict=True)
    except OSError:
        return None
    if resolved.suffix.lower() != ".jsonl" or projects_root not in resolved.parents:
        return None
    return resolved if resolved.is_file() else None


def _record_capture_status(
    state_dir: Path,
    *,
    status: str,
    attachment_count: int = 0,
    session_user_count: int = 0,
    latest_image_count: int = 0,
    latest_user_age: str = "unknown",
) -> None:
    """仅保存有限的状态码和数量，避免诊断日志暴露正文、文件名或路径。"""

    target = state_dir / "capture-diagnostics.jsonl"
    try:
        # 诊断文件有上限，不能让大量普通会话无限占用插件私有目录。
        if target.is_symlink() or (target.exists() and (not target.is_file() or target.stat().st_size >= 131072)):
            return
        record = {
            "time": int(time.time()),
            "status": status,
            "attachment_count": attachment_count,
            "session_user_count": min(session_user_count, 1000),
            "latest_image_count": min(latest_image_count, 1000),
            "latest_user_age": latest_user_age,
        }
        with target.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        # 诊断记录失败不能改变附件提交的授权结论。
        return


def _write_manifest(state_dir: Path, manifest: dict[str, Any]) -> bool:
    """原子写入提交单，避免 MCP 读取到半写入 JSON。"""

    submission_ref = str(manifest["submission_ref"])
    submissions_dir = state_dir / "submissions"
    submissions_dir.mkdir(parents=True, exist_ok=True)
    target = submissions_dir / f"{submission_ref}.json"
    temporary = submissions_dir / f".{submission_ref}.{secrets.token_hex(8)}.tmp"
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except (OSError, UnicodeError):
        temporary.unlink(missing_ok=True)
        return False
    return True


def _normalize_host_text(value: str) -> str:
    """把宿主 JSON 中的 UTF-16 代理对还原为可安全写入 UTF-8 的文本。"""

    return value.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le")


def _pending_submission(
    payload: dict[str, Any], *, state_dir: Path
) -> tuple[str | None, str]:
    """在宿主尚未落盘本轮消息时，签发绑定固定 transcript 的待解析引用。"""

    session_id = payload.get("session_id")
    prompt = payload.get("prompt")
    transcript = _safe_transcript_path(
        raw_path=payload.get("transcript_path"), workbuddy_home=_workbuddy_home()
    )
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(prompt, str)
        or not prompt.strip()
        or transcript is None
    ):
        return None, "PENDING_INPUT_REJECTED"
    try:
        prompt = _normalize_host_text(prompt)
    except UnicodeError:
        return None, "PENDING_INPUT_ENCODING_INVALID"
    submission_ref = f"wbsub_v1_{secrets.token_hex(16)}"
    manifest = {
        "version": 2,
        "status": "PENDING_TRANSCRIPT",
        "submission_ref": submission_ref,
        "session_id": session_id,
        "created_at": time.time(),
        "prompt": prompt,
        "transcript_path": str(transcript),
        "attachments": [],
    }
    if not _write_manifest(state_dir, manifest):
        return None, "MANIFEST_WRITE_FAILED"
    return submission_ref, "PENDING_MANIFEST_CREATED"


def _submission_context(*, submission_ref: str, count: int | None) -> dict[str, Any]:
    """只向模型暴露不透明引用，不暴露 transcript、缓存路径或文件名。"""

    count_value = str(count) if count is not None else "pending"
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "FILE_AGENT_HOST_SUBMISSION "
                f"ref={submission_ref}; count={count_value}。"
                "这是宿主为本轮固定附件签发的可信引用；count=pending 表示宿主将在 MCP 调用时"
                "从固定会话记录中完成唯一匹配。文件助手是本连接器面向用户的中文称呼；"
                "当用户说‘请用文件助手归档并分类本轮附件’，或明确要求导入、归档、整理、解析、OCR、分类附件时，"
                "调用 workbuddy_submission_ingest，并且只传 submission_ref；不要索取或构造路径、"
                "附件 ID、文件名或 user_request。仅上传、查看或讨论附件时不要导入。"
            ),
        },
    }


def capture_submission(payload: dict[str, Any]) -> dict[str, Any]:
    """生成不可猜测的提交单引用，并只向 Agent 注入引用和固定路由指令。"""

    if payload.get("hook_event_name") != "UserPromptSubmit":
        return {"continue": True}
    state_dir = _state_dir()
    if state_dir is None:
        return _capture_unavailable_result()
    # WorkBuddy 5.5.3 会在本轮消息落盘前触发 Hook。主路径不能先扫描旧 transcript，
    # 否则既无法取得当前附件，也可能消耗 Hook 的短时限。这里只校验宿主给出的固定引用
    # 并立即签发待解析提交单；MCP 稍后看到完整 transcript 后执行唯一性校验。
    submission_ref, pending_status = _pending_submission(payload, state_dir=state_dir)
    _record_capture_status(state_dir, status=pending_status)
    if submission_ref is None:
        return _capture_unavailable_result()
    return _submission_context(submission_ref=submission_ref, count=None)


def main() -> int:
    """读取 Hook stdin 并仅输出符合 WorkBuddy Hook 契约的 JSON。"""

    try:
        # 宿主通过管道发送 UTF-8；Windows 默认 GBK 解码会破坏中文及附件名称。
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError):
        print(json.dumps(_capture_unavailable_result(), ensure_ascii=True))
        return 0
    result = capture_submission(payload) if isinstance(payload, dict) else {"continue": True}
    # ASCII JSON 转义兼容宿主 UTF-8 读取及 Windows 各种 stdout 编码。
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
