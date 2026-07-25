"""文件检索短语策略执行器。

该模块把一个或多个完整短语交给现有两阶段检索，并在返回前执行确定性证据门槛。
它不会把短语拆成 OR，也不会访问文件系统或写入业务事实。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any


MAX_MERGED_RESULTS = 20


class FileSearchPhraseStrategyService:
    """在两阶段检索之上执行完整短语合并和证据过滤。"""

    def __init__(self, *, search_service: Any, tokenizer: Any) -> None:
        """注入现有两阶段检索服务和同一分词器。"""

        self.search_service = search_service
        self.tokenizer = tokenizer

    def search(
        self,
        *,
        original_query: str,
        parsed_query: Any,
        scope: Any,
        phrases: list[str] | tuple[str, ...],
        require_body_evidence: bool,
    ) -> dict[str, Any]:
        """按完整短语逐项搜索并按工作副本去重。

        `require_body_evidence=false` 时也只允许完整短语命中文件名、摘要、分类或正文，
        不保留仅由拆词 OR 产生的文件级候选。
        """

        unique_phrases = list(dict.fromkeys(str(item).strip() for item in phrases if str(item).strip()))[:8]
        merged: dict[str, dict[str, Any]] = {}
        partial = False
        for phrase in unique_phrases:
            phrase_query = replace(
                parsed_query,
                cleaned=phrase,
                terms=(
                    self.tokenizer.tokenize(phrase)
                    if self.tokenizer and hasattr(self.tokenizer, "tokenize")
                    else [phrase]
                ),
            )
            payload = self.search_service.search(
                query=original_query,
                parsed_query=phrase_query,
                scope=scope,
                exact_phrase=phrase,
                require_body_evidence=require_body_evidence,
                include_internal_match_flags=True,
            )
            partial = partial or bool(payload.get("partial"))
            for item in payload.get("results", []):
                if not isinstance(item, dict):
                    continue
                body_hit = bool(item.get("_body_phrase_hit"))
                if require_body_evidence and not body_hit:
                    continue
                if not require_body_evidence and not body_hit and not _metadata_contains_phrase(item, phrase):
                    continue
                key = str(
                    item.get("working_copy_id")
                    or item.get("document_version_id")
                    or item.get("document_id")
                    or ""
                )
                if not key:
                    continue
                existing = merged.get(key)
                if existing is None:
                    existing = {**item, "matched_phrases": []}
                    merged[key] = existing
                if phrase not in existing["matched_phrases"]:
                    existing["matched_phrases"].append(phrase)
                if body_hit:
                    existing["_body_phrase_hit"] = True

        results = list(merged.values())
        results.sort(
            key=lambda item: (
                not bool(item.get("_body_phrase_hit")),
                str(item.get("filename") or ""),
                str(item.get("working_copy_id") or ""),
            )
        )
        results = results[:MAX_MERGED_RESULTS]
        for item in results:
            item.pop("_body_phrase_hit", None)
        return {
            "ok": True,
            "kind": "workspace_file_search",
            "query": original_query,
            "total_returned": len(results),
            "partial": partial,
            "results": results,
            "user_message": _user_message(results, partial),
        }


def _metadata_contains_phrase(item: dict[str, Any], phrase: str) -> bool:
    """检查用户可见文件级字段中的连续短语，不能使用内部拆词分数代替。"""

    needle = _compact(phrase)
    if not needle:
        return False
    haystacks = [
        str(item.get("filename") or ""),
        str(item.get("overview") or ""),
        " ".join(str(value) for value in item.get("category_path", []) if value),
    ]
    return any(needle in _compact(value) for value in haystacks)


def _compact(value: str) -> str:
    """移除空白并统一大小写，用于连续短语比较。"""

    return re.sub(r"\s+", "", str(value or "").lower())


def _user_message(results: list[dict[str, Any]], partial: bool) -> str:
    """生成不包含内部策略信息的简短结果状态。"""

    if results:
        return f"找到 {len(results)} 个相关文件。"
    if partial:
        return "暂未找到相关文件，部分正文索引当前不可用。"
    return "未找到相关文件。"
