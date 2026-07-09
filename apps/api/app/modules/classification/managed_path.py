"""基于受管目录子目录的动态分类候选生成。

该模块只把 `PATH_AS_CATEGORY` 受管目录中已经存在的父目录转换为分类建议，
不负责移动、复制或修改文件；真实文件整理必须继续走 OperationPlan。
"""

from __future__ import annotations

from typing import Any


TAXONOMY_KEY = "managed_path_categories"
TAXONOMY_VERSION = "managed-path-v1"


def match_managed_path_categories(
    *,
    filename: str,
    text: str,
    category_rows: list[tuple[str, str, str, int]],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """根据文件名和正文，把文件匹配到受管目录已有子目录分类。"""

    candidates: list[dict[str, Any]] = []
    title_text = filename or ""
    body_text = text or ""
    for order, row in enumerate(category_rows):
        root_key, _display_name, category_path, file_count = row
        parts = _split_category_path(category_path)
        if not parts:
            continue
        score, signals, reason = _score_path_category(
            parts=parts,
            title_text=title_text,
            body_text=body_text,
            file_count=file_count,
        )
        if score <= 0:
            continue
        candidates.append(
            {
                "name": "/".join(parts),
                "category_path": parts,
                "confidence": round(min(0.92, max(0.45, score)), 2),
                "status": "SUGGESTED",
                "source": "managed_path",
                "evidence": signals,
                "taxonomy_key": TAXONOMY_KEY,
                "taxonomy_version": TAXONOMY_VERSION,
                "managed_root_key": root_key,
                "candidate_reason": reason,
                "_order": order,
            }
        )

    if not candidates:
        return [_managed_other_category()]
    candidates.sort(key=lambda item: (-float(item["confidence"]), int(item["_order"])))
    return [
        {key: value for key, value in candidate.items() if key != "_order"}
        for candidate in candidates[:max(0, min(limit, 8))]
    ]


def _score_path_category(
    *,
    parts: list[str],
    title_text: str,
    body_text: str,
    file_count: int,
) -> tuple[float, list[str], str]:
    """计算单个目录分类的匹配分数。"""

    signals = _path_signals(parts)
    title_hits = [signal for signal in signals if signal and signal in title_text]
    body_hits = [signal for signal in signals if signal and signal in body_text]
    matched = _unique([*title_hits, *body_hits])
    if not matched:
        return 0.0, [], ""

    leaf = parts[-1]
    full_path = "/".join(parts)
    score = 0.0
    if leaf in title_text:
        score += 0.42
    if leaf in body_text:
        score += 0.3
    if full_path in title_text or full_path in body_text:
        score += 0.12
    score += min(0.22, 0.07 * len([item for item in title_hits if item != leaf]))
    score += min(0.16, 0.04 * len([item for item in body_hits if item != leaf]))
    if file_count > 1:
        # 已有样本文件越多，说明该目录作为分类越稳定；只给很小加权，避免压过正文证据。
        score += min(0.05, file_count * 0.005)

    reasons: list[str] = []
    if title_hits:
        reasons.append(f"文件名命中：{'、'.join(title_hits[:5])}")
    if body_hits:
        reasons.append(f"正文命中：{'、'.join(body_hits[:5])}")
    return score, matched, "；".join(reasons)


def _path_signals(parts: list[str]) -> list[str]:
    """从目录路径拆出可匹配信号词。"""

    signals: list[str] = []
    for part in parts:
        normalized = part.strip()
        if normalized:
            signals.append(normalized)
            signals.extend(_split_loose_tokens(normalized))
    signals.append("/".join(parts))
    return _unique([item for item in signals if len(item) >= 2])


def _split_category_path(category_path: str) -> list[str]:
    """把数据库中的 POSIX 分类路径拆成展示用路径数组。"""

    return [part.strip() for part in str(category_path or "").replace("\\", "/").split("/") if part.strip()]


def _split_loose_tokens(value: str) -> list[str]:
    """按常见文件夹命名分隔符拆出辅助信号词。"""

    tokens = [value]
    for separator in ("_", "-", " ", "　", "、", "，", ","):
        next_tokens: list[str] = []
        for token in tokens:
            next_tokens.extend(token.split(separator))
        tokens = next_tokens
    return [token.strip() for token in tokens if token.strip()]


def _unique(values: list[str]) -> list[str]:
    """保持顺序去重。"""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _managed_other_category() -> dict[str, Any]:
    """没有命中任何动态目录时返回待复核的其他分类。"""

    return {
        "name": "其他",
        "category_path": ["其他"],
        "confidence": 0.2,
        "status": "NEEDS_REVIEW",
        "source": "managed_path",
        "evidence": [],
        "taxonomy_key": TAXONOMY_KEY,
        "taxonomy_version": TAXONOMY_VERSION,
    }
