"""把 F2 JSON 输出收敛为项目内稳定结构。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.modules.file_rename.executor_protocol import RenameExecutorError


@dataclass(frozen=True)
class F2ReportItem:
    """经过路径边界校验的 F2 单项报告。"""

    before_relative_path: str
    after_relative_path: str
    status: str


def parse_f2_report(content: str, *, root_path: Path) -> list[F2ReportItem]:
    """兼容解析 F2 数组或常见包装对象，未知结构直接拒绝。"""

    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RenameExecutorError("F2_INVALID_JSON", "F2 未返回合法 JSON。") from exc

    rows = _extract_rows(payload)
    parsed: list[F2ReportItem] = []
    for row in rows:
        before = _read_string(row, {"original", "input", "source", "before", "old", "from"})
        after = _read_string(row, {"renamed", "output", "target", "after", "new", "to"})
        status = _read_string(row, {"status", "state", "result"}, required=False) or "ok"
        if not before or not after:
            raise RenameExecutorError("F2_INVALID_JSON", "F2 JSON 缺少源路径或目标路径。")
        parsed.append(
            F2ReportItem(
                before_relative_path=_to_relative_path(before, root_path=root_path),
                after_relative_path=_to_relative_path(after, root_path=root_path),
                status=status.lower(),
            )
        )
    return parsed


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """只接受明确的数组或单层结果包装。"""

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("items", "results", "files", "renames", "matches"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
        if rows is None and _looks_like_report_row(payload):
            rows = [payload]
        if rows is None:
            raise RenameExecutorError("F2_INVALID_JSON", "无法识别 F2 JSON 结果结构。")
    else:
        raise RenameExecutorError("F2_INVALID_JSON", "F2 JSON 顶层类型不受支持。")
    if not all(isinstance(row, dict) for row in rows):
        raise RenameExecutorError("F2_INVALID_JSON", "F2 JSON 项必须是对象。")
    return rows


def _looks_like_report_row(payload: dict[str, Any]) -> bool:
    """判断对象本身是否是单项报告。"""

    keys = {str(key).lower() for key in payload}
    return bool(keys.intersection({"original", "input", "source", "before", "old"}))


def _read_string(row: dict[str, Any], aliases: set[str], *, required: bool = True) -> str:
    """按大小写不敏感别名读取字符串字段。"""

    normalized = {str(key).lower(): value for key, value in row.items()}
    matches = [normalized[key] for key in aliases if key in normalized and normalized[key] is not None]
    if len(matches) > 1 and len({str(value) for value in matches}) > 1:
        raise RenameExecutorError("F2_INVALID_JSON", "F2 JSON 包含冲突的同义字段。")
    if not matches:
        if required:
            return ""
        return ""
    value = matches[0]
    if not isinstance(value, (str, int, float)):
        raise RenameExecutorError("F2_INVALID_JSON", "F2 JSON 路径或状态字段类型不合法。")
    return str(value).strip()


def _to_relative_path(value: str, *, root_path: Path) -> str:
    """把 F2 返回路径转换为受管根目录相对路径。"""

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    try:
        relative = candidate.resolve(strict=False).relative_to(root_path.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RenameExecutorError("F2_UNEXPECTED_FILE", "F2 返回了受管目录外的文件。") from exc
    return relative.as_posix()
