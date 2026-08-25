"""文件检索短语策略执行器。

该模块把一个或多个完整短语交给现有两阶段检索，并在返回前执行确定性证据门槛。
它不会把短语拆成 OR，也不会访问文件系统或写入业务事实。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import log_event


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
        log_event(
            "retrieval.phrase_strategy.started",
            tool_name="hybrid-search",
            status="RUNNING",
            phrase_count=len(unique_phrases),
            require_body_evidence=require_body_evidence,
            message="受控短语检索开始",
        )
        merged: dict[str, dict[str, Any]] = {}
        partial = False
        candidate_limit_reached = False
        successful_phrase_count = 0
        last_error: SQLAlchemyError | None = None
        for phrase in unique_phrases:
            phrase_fingerprint = hashlib.sha256(
                phrase.lower().encode("utf-8")
            ).hexdigest()[:12]
            phrase_query = replace(
                parsed_query,
                cleaned=phrase,
                terms=(
                    self.tokenizer.tokenize(phrase)
                    if self.tokenizer and hasattr(self.tokenizer, "tokenize")
                    else [phrase]
                ),
            )
            try:
                payload = self.search_service.search(
                    query=original_query,
                    parsed_query=phrase_query,
                    scope=scope,
                    exact_phrase=phrase,
                    require_body_evidence=require_body_evidence,
                    include_internal_match_flags=True,
                )
            except SQLAlchemyError as exc:
                # TwoStageFileSearchService 已用 savepoint 隔离每段 SQL。单个正式
                # 别名失败时保留其他别名的有效结果；若全部失败，循环结束后仍把
                # 异常交给原有摘要级降级链路处理。
                partial = True
                last_error = exc
                log_event(
                    "retrieval.phrase_strategy.phrase_failed",
                    level="WARNING",
                    tool_name="hybrid-search",
                    status="DEGRADED",
                    phrase_fingerprint=phrase_fingerprint,
                    phrase_chars=len(phrase),
                    error_code=exc.__class__.__name__,
                    message="单个受控短语检索失败，继续处理其余短语",
                )
                continue
            successful_phrase_count += 1
            partial = partial or bool(payload.get("partial"))
            candidate_limit_reached = candidate_limit_reached or bool(
                payload.get("candidate_limit_reached")
            )
            raw_result_count = len(
                [item for item in payload.get("results", []) if isinstance(item, dict)]
            )
            accepted_count = 0
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
                    or item.get("managed_file_revision_id")
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
                accepted_count += 1
            log_event(
                "retrieval.phrase_strategy.phrase_completed",
                level="WARNING" if payload.get("partial") else "INFO",
                tool_name="hybrid-search",
                status="DEGRADED" if payload.get("partial") else "COMPLETED",
                phrase_fingerprint=phrase_fingerprint,
                phrase_chars=len(phrase),
                raw_result_count=raw_result_count,
                accepted_result_count=accepted_count,
                require_body_evidence=require_body_evidence,
                message="单个受控短语检索和证据门槛处理完成",
            )

        if successful_phrase_count == 0 and last_error is not None:
            raise last_error

        results = list(merged.values())
        results.sort(
            key=lambda item: (
                not bool(item.get("_body_phrase_hit")),
                str(item.get("filename") or ""),
                str(item.get("working_copy_id") or ""),
            )
        )
        for item in results:
            # 完整短语已经通过文件名、摘要或正文的受控连续匹配；它可作为
            # 最终相关结果进入 RelevantFileSet。正文要求仍由调用方单独决定。
            item["relevance_tier"] = "SUPPORTED"
            item.pop("_body_phrase_hit", None)
        log_event(
            "retrieval.phrase_strategy.completed",
            level="WARNING" if partial or not results else "INFO",
            tool_name="hybrid-search",
            status="DEGRADED" if partial else "COMPLETED",
            phrase_count=len(unique_phrases),
            result_count=len(results),
            partial=partial,
            message="受控短语检索合并完成",
        )
        return {
            "ok": True,
            "kind": "workspace_file_search",
            "query": original_query,
            "total_returned": len(results),
            "partial": partial,
            "candidate_limit_reached": candidate_limit_reached,
            "results": results,
            "user_message": _user_message(results, partial),
        }

    def search_with_topic_tiers(
        self,
        *,
        original_query: str,
        parsed_query: Any,
        scope: Any,
        exact_phrase: str,
        required_topic_terms: list[str],
        supporting_topic_terms: list[str],
    ) -> dict[str, Any]:
        """按核心主题和宽泛动作词分组返回文件检索结果。

        LLM 可以理解“劳务费发放”这样的业务表达，但不能自行断言文件相关性。
        本方法只复用受控短语检索的可定位正文命中：同时命中核心主题和动作词的
        文件标为 ``SUPPORTED``；仅命中其中一项的文件标为 ``POSSIBLE``。后者
        只能作为用户可继续查看的候选，不能被 evidence-answer 当成事实证据。
        """

        required = _unique_terms(required_topic_terms)
        supporting = _unique_terms(supporting_topic_terms)
        if not required or not supporting:
            return self.search(
                original_query=original_query,
                parsed_query=parsed_query,
                scope=scope,
                phrases=[exact_phrase],
                require_body_evidence=True,
            )

        # 完整短语命中是最高强度的已验证结果；随后用核心词和动作词的正文
        # 交集补充“词语未连续出现、但同一正文确实同时提及”的文件。
        exact_result = self.search(
            original_query=original_query,
            parsed_query=parsed_query,
            scope=scope,
            phrases=[exact_phrase],
            require_body_evidence=True,
        )
        required_result = self.search(
            original_query=original_query,
            parsed_query=parsed_query,
            scope=scope,
            phrases=required,
            require_body_evidence=True,
        )
        supporting_result = self.search(
            original_query=original_query,
            parsed_query=parsed_query,
            scope=scope,
            phrases=supporting,
            require_body_evidence=True,
        )
        possible_result = self.search(
            original_query=original_query,
            parsed_query=parsed_query,
            scope=scope,
            phrases=[*required, *supporting],
            require_body_evidence=False,
        )

        exact_by_id = _results_by_id(exact_result.get("results", []))
        required_by_id = _results_by_id(required_result.get("results", []))
        supporting_by_id = _results_by_id(supporting_result.get("results", []))
        possible_by_id = _results_by_id(possible_result.get("results", []))
        supported_ids = set(exact_by_id).union(
            set(required_by_id).intersection(supporting_by_id)
        )

        supported: list[dict[str, Any]] = []
        for result_id in supported_ids:
            item = {
                **(
                    exact_by_id.get(result_id)
                    or required_by_id.get(result_id)
                    or supporting_by_id[result_id]
                )
            }
            item["relevance_tier"] = "SUPPORTED"
            item["match_reasons"] = _append_reason(
                item.get("match_reasons"),
                f"已在原文确认核心主题“{'、'.join(required)}”与“{'、'.join(supporting)}”",
            )
            supported.append(item)

        possible: list[dict[str, Any]] = []
        for result_id, source_item in possible_by_id.items():
            if result_id in supported_ids:
                continue
            item = {**source_item, "relevance_tier": "POSSIBLE"}
            item["match_reasons"] = _append_reason(
                item.get("match_reasons"),
                f"仅命中部分主题线索，尚未同时确认“{'、'.join(required)}”与“{'、'.join(supporting)}”",
            )
            possible.append(item)

        supported.sort(key=_stable_result_sort_key)
        possible.sort(key=_stable_result_sort_key)
        results = [*supported, *possible]
        partial = any(
            bool(result.get("partial"))
            for result in (
                exact_result,
                required_result,
                supporting_result,
                possible_result,
            )
        )
        candidate_limit_reached = any(
            bool(result.get("candidate_limit_reached"))
            for result in (
                exact_result,
                required_result,
                supporting_result,
                possible_result,
            )
        )
        log_event(
            "retrieval.phrase_strategy.topic_tiers_completed",
            level="WARNING" if partial else "INFO",
            tool_name="hybrid-search",
            status="DEGRADED" if partial else "COMPLETED",
            required_topic_count=len(required),
            supporting_topic_count=len(supporting),
            supported_result_count=len(supported),
            possible_result_count=len(possible),
            message="核心主题与宽泛动作词检索分级完成",
        )
        return {
            "ok": True,
            "kind": "workspace_file_search",
            "query": original_query,
            "total_returned": len(results),
            "supported_count": len(supported),
            "possible_count": len(possible),
            "partial": partial,
            "candidate_limit_reached": candidate_limit_reached,
            "results": results,
            "user_message": _tiered_user_message(
                supported_count=len(supported),
                possible_count=len(possible),
                partial=partial,
            ),
        }

    def search_fact_anchors(
        self,
        *,
        original_query: str,
        parsed_query: Any,
        scope: Any,
        anchors: list[str] | tuple[str, ...],
        requested_fields: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        """分别验证事实问句锚点，并以文件交集确定可回答范围。

        问句中的“哪个单位、多少费用”属于待回答字段，不能参与召回。多个
        锚点只有在同一文件中都得到连续命中时才是 ``SUPPORTED``；只命中
        部分锚点的文件降为 ``POSSIBLE``，不得授权后续证据回答。
        """

        unique_anchors = _unique_terms(list(anchors))[:6]
        searches = [
            (
                anchor,
                self.search(
                    original_query=original_query,
                    parsed_query=parsed_query,
                    scope=scope,
                    phrases=[anchor],
                    require_body_evidence=False,
                ),
            )
            for anchor in unique_anchors
        ]
        result_maps = [
            _results_by_id(payload.get("results", []))
            for _anchor, payload in searches
        ]
        supported_ids = (
            set.intersection(*(set(items) for items in result_maps))
            if result_maps
            else set()
        )
        hit_counts: dict[str, int] = {}
        source_items: dict[str, dict[str, Any]] = {}
        matched_by_id: dict[str, list[str]] = {}
        for (anchor, _payload), result_map in zip(searches, result_maps):
            for result_id, item in result_map.items():
                source_items.setdefault(result_id, item)
                hit_counts[result_id] = hit_counts.get(result_id, 0) + 1
                matched_by_id.setdefault(result_id, []).append(anchor)

        results: list[dict[str, Any]] = []
        for result_id, source_item in source_items.items():
            matched = matched_by_id.get(result_id, [])
            supported = result_id in supported_ids
            item = {
                **source_item,
                "relevance_tier": "SUPPORTED" if supported else "POSSIBLE",
                "matched_fact_anchors": matched,
            }
            if supported:
                reason = f"已验证同一文件命中事实锚点：{'、'.join(matched)}"
            else:
                missing = [value for value in unique_anchors if value not in matched]
                reason = (
                    f"仅命中事实问句的部分线索：{'、'.join(matched)}；"
                    f"尚未确认：{'、'.join(missing)}"
                )
            item["match_reasons"] = _append_reason(
                item.get("match_reasons"), reason
            )
            results.append(item)
        results.sort(
            key=lambda item: (
                str(item.get("relevance_tier") or "") == "POSSIBLE",
                -hit_counts.get(_result_id(item), 0),
                *_stable_result_sort_key(item),
            )
        )
        supported_count = len(supported_ids)
        possible_count = len(results) - supported_count
        partial = any(bool(payload.get("partial")) for _anchor, payload in searches)
        candidate_limit_reached = any(
            bool(payload.get("candidate_limit_reached"))
            for _anchor, payload in searches
        )
        log_event(
            "retrieval.phrase_strategy.fact_anchors_completed",
            level="WARNING" if partial or not supported_ids else "INFO",
            tool_name="hybrid-search",
            status="DEGRADED" if partial else "COMPLETED",
            anchor_count=len(unique_anchors),
            requested_field_count=len(list(requested_fields)),
            supported_result_count=supported_count,
            possible_result_count=possible_count,
            message="事实问句锚点检索与文件交集校验完成",
        )
        return {
            "ok": True,
            "kind": "workspace_file_search",
            "query": original_query,
            "total_returned": len(results),
            "supported_count": supported_count,
            "possible_count": possible_count,
            "partial": partial,
            "candidate_limit_reached": candidate_limit_reached,
            "results": results,
            "fact_search": {
                "anchors": unique_anchors,
                "requested_fields": list(requested_fields)[:12],
            },
            "user_message": _tiered_user_message(
                supported_count=supported_count,
                possible_count=possible_count,
                partial=partial,
            ),
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


def _unique_terms(values: list[str]) -> list[str]:
    """去重并限制主题词数量，避免自然语言输入放大检索调用次数。"""

    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))[:4]


def _result_id(item: dict[str, Any]) -> str:
    """生成同一次检索内稳定的工作副本优先去重键。"""

    return str(
        item.get("working_copy_id")
        or item.get("managed_file_revision_id")
        or item.get("document_version_id")
        or item.get("document_id")
        or ""
    )


def _results_by_id(items: Any) -> dict[str, dict[str, Any]]:
    """按稳定对象标识建立结果映射，忽略不完整的 Tool 输出。"""

    results: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        result_id = _result_id(item)
        if result_id:
            results.setdefault(result_id, item)
    return results


def _append_reason(reasons: Any, reason: str) -> list[str]:
    """补充普通用户可读的分级原因，不暴露内部检索分数。"""

    result = [str(item) for item in reasons if str(item)] if isinstance(reasons, list) else []
    if reason not in result:
        result.append(reason)
    return result[:6]


def _stable_result_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    """在不同数据库返回顺序下保持同一分组内的展示顺序稳定。"""

    return (str(item.get("filename") or ""), _result_id(item))


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


def _tiered_user_message(
    *, supported_count: int, possible_count: int, partial: bool
) -> str:
    """生成分级检索的用户提示，不把候选误表述为已验证事实。"""

    if supported_count and possible_count:
        message = f"找到 {supported_count} 个已验证相关文件，另有 {possible_count} 个可能相关文件。"
    elif supported_count:
        message = f"找到 {supported_count} 个已验证相关文件。"
    elif possible_count:
        message = f"未找到可确认的相关文件，但有 {possible_count} 个可能相关文件。"
    else:
        message = "未找到相关文件。"
    if partial:
        return f"{message} 部分正文索引当前不可用。"
    return message


def mark_metadata_results_as_possible(
    *, result: dict[str, Any], parsed_query: Any
) -> dict[str, Any]:
    """把摘要级降级结果显式标记为候选，禁止伪装成正文已验证结果。

    当 Chunk 索引不可用时，文件名、分类和本地摘要仍可帮助用户发现文件；但它们
    不能证明“原文涉及某主题”。此函数只针对同时存在核心主题和宽泛动作词的
    原文关系查询生效，并保持其它旧摘要检索结果的兼容展示。
    """

    required = _unique_terms(
        list(getattr(parsed_query, "required_topic_terms", []) or [])
    )
    supporting = _unique_terms(
        list(getattr(parsed_query, "supporting_topic_terms", []) or [])
    )
    relation_mode = str(getattr(parsed_query, "relation_mode", ""))
    if relation_mode != "LITERAL" or not required or not supporting:
        return result

    candidates: list[dict[str, Any]] = []
    for raw_item in list(result.get("results") or []):
        if not isinstance(raw_item, dict):
            continue
        item = {**raw_item, "relevance_tier": "POSSIBLE"}
        item["match_reasons"] = _append_reason(
            item.get("match_reasons"),
            f"当前仅有文件名、分类或摘要线索，尚未在原文确认“{'、'.join(required)}”与“{'、'.join(supporting)}”",
        )
        candidates.append(item)
    message = _tiered_user_message(
        supported_count=0,
        possible_count=len(candidates),
        partial=True,
    )
    return {
        **result,
        "total_returned": len(candidates),
        "supported_count": 0,
        "possible_count": len(candidates),
        # 摘要链路缺少当前正文验证；必须明确标记降级状态，供 Planner 和回执
        # 禁止把候选当成可继续读取的已验证范围。
        "partial": True,
        "results": candidates,
        "user_message": message,
    }
