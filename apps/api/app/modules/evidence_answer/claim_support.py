"""在局部原文中核对结论的否定关系，避免同页无关的禁止条款污染其他事实。"""

from __future__ import annotations

import re
from typing import Any


_CLAUSE_SPLIT = re.compile(r"[。；;！？!?，]+|(?<!\d),|,(?!\d)")
_NUMBERS = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?%?")
_NEGATIONS = ("不需要", "无需", "不得", "禁止", "尚未", "未", "没有", "不")


def _has_negation(text: str) -> bool:
    """忽略不改变肯定关系的连接词；禁止、无需、未等仍须保留。"""

    for phrase in ("不仅", "不但", "未来"):
        text = text.replace(phrase, "")
    return any(marker in text for marker in _NEGATIONS)


def _units(text: str) -> list[str]:
    """PDF软换行不是语义边界；按句号、分号和分句切分，保留千分位数字。"""

    return [part.strip() for part in _CLAUSE_SPLIT.split("".join(text.splitlines())) if part.strip()]


def polarity_rejection_reason(
    *, claim: str, quotes: list[str], tokenizer: Any,
    stop_terms: set[str], required_overlap: float,
) -> str | None:
    """只检查与每个结论分句最匹配且数字相符的原文分句，仍拒绝否定翻转。

    不改变外层数字与词项门槛，不从低分、不相关片段挑一个极性相同的句子凑支持。
    找不到局部支撑时交回已有的受控修复，不能自动视为结论正确。
    """

    evidence_units = [unit for quote in quotes for unit in _units(quote)]
    candidates = [
        (unit, set(tokenizer.tokenize(unit)), {value.replace(",", "") for value in _NUMBERS.findall(unit)})
        for unit in evidence_units
    ]
    for unit in _units(claim):
        terms = {value for value in tokenizer.tokenize(unit) if len(value) >= 2 and not value.isdigit() and value not in stop_terms}
        if not terms:
            continue
        numbers = {value.replace(",", "") for value in _NUMBERS.findall(unit)}
        scored = [
            (len(terms & source_terms) / len(terms), source)
            for source, source_terms, source_numbers in candidates
            if numbers <= source_numbers
        ]
        best = max((score for score, _source in scored), default=0)
        if best < required_overlap:
            return "CLAIM_LOCAL_SUPPORT_MISSING"
        # 同分但极性冲突时拒绝，不能从冲突片段中挑一边冒充可靠支持。
        polarities = {_has_negation(source) for score, source in scored if score == best}
        if polarities != {_has_negation(unit)}:
            return "CLAIM_POLARITY_MISMATCH"
    return None
