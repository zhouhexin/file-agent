"""为已发生冲突的标准文件名生成可读消歧候选。"""

from __future__ import annotations

import re
from pathlib import Path


_SEPARATOR_PATTERN = re.compile(r"\s*[-_—–]+\s*")
_YEAR_PREFIX_PATTERN = re.compile(r"^(?:19|20)\d{2}(?:\d{4})?_")
_PERSON_PATTERN = re.compile(r"^[\u3400-\u9fff·]{2,4}$")
_DATE_OR_VERSION_PATTERN = re.compile(
    r"^(?:v(?:er(?:sion)?)?\s*)?\d+(?:\.\d+)*$|^(?:19|20)\d{2}(?:\d{2}){0,2}$",
    re.IGNORECASE,
)
_SEMANTIC_MARKERS = (
    "学院",
    "部门",
    "办公室",
    "学工办",
    "中心",
    "专业",
    "软件工程",
    "网络工程",
    "计科",
    "科研",
    "教学",
    "人才",
    "修订",
    "修改",
    "提交",
    "最终",
    "正式",
    "新版",
    "旧文件",
    "新文件",
    "new",
    "all",
    "版",
)
_GENERIC_SOURCE_DIRECTORIES = {
    "办公",
    "外来应聘",
    "材料",
    "附件",
    "附件材料",
    "个人填写",
    "分管提交",
    "考查试讲表",
    "考察试讲表",
    "原始考察表",
}


def readable_collision_filename_candidates(
    *,
    standard_filename: str,
    original_filename: str,
    source_relative_path: str,
) -> list[str]:
    """按语义字段、原名差异、源目录依次生成不透明度最低的候选名。"""

    target = Path(standard_filename)
    target_stem = target.stem.strip()
    suffix = target.suffix
    original_stem = Path(original_filename).stem.strip()
    qualifiers = [
        _semantic_qualifier(original_stem, target_stem),
        _filename_difference_qualifier(original_stem, target_stem),
        _nearest_meaningful_parent(source_relative_path, target_stem),
    ]
    candidates: list[str] = []
    for qualifier in qualifiers:
        candidate = _append_qualifier(target_stem, suffix, qualifier)
        if candidate and candidate != standard_filename and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _semantic_qualifier(original_stem: str, target_stem: str) -> str | None:
    segments = [_clean_qualifier(item) for item in _SEPARATOR_PATTERN.split(original_stem)]
    meaningful = [
        item
        for item in segments[1:]
        if item
        and _is_semantic_segment(item)
        and _normalized(item) not in _normalized(target_stem)
    ]
    return "_".join(meaningful[-2:]) if meaningful else None


def _filename_difference_qualifier(
    original_stem: str,
    target_stem: str,
) -> str | None:
    target_core = _YEAR_PREFIX_PATTERN.sub("", target_stem)
    original_folded = original_stem.casefold()
    target_folded = target_core.casefold()
    if original_folded == target_folded:
        return None
    if target_folded and target_folded in original_folded:
        start = original_folded.index(target_folded)
        difference = f"{original_stem[:start]}_{original_stem[start + len(target_core):]}"
        cleaned = _clean_qualifier(difference)
        return cleaned or None

    trailing = [
        _clean_qualifier(item)
        for item in _SEPARATOR_PATTERN.split(original_stem)[1:]
    ]
    trailing = [
        item
        for item in trailing
        if item and _normalized(item) not in _normalized(target_stem)
    ]
    if trailing:
        return "_".join(trailing[-3:])

    common_prefix_length = 0
    for original_char, target_char in zip(original_folded, target_folded):
        if original_char != target_char:
            break
        common_prefix_length += 1
    if common_prefix_length >= 4:
        return _clean_qualifier(original_stem[common_prefix_length:]) or None
    return None


def _nearest_meaningful_parent(
    source_relative_path: str,
    target_stem: str,
) -> str | None:
    parts = [
        item.strip()
        for item in str(source_relative_path or "").replace("\\", "/").split("/")
        if item.strip()
    ]
    for part in reversed(parts[:-1]):
        cleaned = _clean_qualifier(part)
        if not cleaned or cleaned in _GENERIC_SOURCE_DIRECTORIES:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}年?", cleaned):
            continue
        if _normalized(cleaned) in _normalized(target_stem):
            continue
        return cleaned
    return None


def _is_semantic_segment(value: str) -> bool:
    folded = value.casefold()
    return bool(
        _PERSON_PATTERN.fullmatch(value)
        or _DATE_OR_VERSION_PATTERN.fullmatch(value)
        or any(marker.casefold() in folded for marker in _SEMANTIC_MARKERS)
    )


def _append_qualifier(stem: str, suffix: str, qualifier: str | None) -> str | None:
    cleaned = _clean_qualifier(qualifier or "")
    if not cleaned:
        return None
    available_stem_length = max(1, 240 - len(suffix) - len(cleaned) - 1)
    return f"{stem[:available_stem_length]}_{cleaned}{suffix}"


def _clean_qualifier(value: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", str(value or "").strip())
    cleaned = re.sub(r"^[\s._()（）\[\]【】]+|[\s._()（）\[\]【】]+$", "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned[:48]


def _normalized(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())
