"""首次自动分类落位的确定性门槛策略。

本模块只判断“能否把某个候选当作已生效主分类”，不读取数据库、不构造物理路径，
也不执行文件操作。相同候选和版本配置必须得到相同结果，便于 Shadow 回放和审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class AutoPlacementPolicyResult:
    """一次门槛判断的结构化结果。"""

    accepted: bool
    primary_category: dict[str, Any] | None
    reason_codes: tuple[str, ...]
    calibrated_confidence: float | None
    required_threshold: float
    top_margin: float | None
    required_margin: float
    feature_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def evaluated_decision(self) -> str:
        """返回 Shadow 和真实执行都可复用的候选决策名称。"""

        return "AUTO_ORGANIZED" if self.accepted else "NEEDS_REVIEW"


class AutoPlacementPolicy:
    """选择最高置信候选，并保留必要的安全与证据硬门槛。"""

    def __init__(self, settings: Settings) -> None:
        """保存版本化策略配置；当前冷启动阶段使用保守全局回退阈值。"""

        self.settings = settings

    def evaluate(
        self,
        *,
        categories: list[dict[str, Any]],
        extraction_status: str,
        risk_passed: bool = True,
    ) -> AutoPlacementPolicyResult:
        """从按置信度排序的多标签候选中选择唯一主分类，硬门槛失败时拒识。

        当前测试阶段暂不使用分数、候选间隔、正文信号数量、负向信号和摘要/全文
        一致性作为拒绝条件。相关计算和原条件保留，便于后续恢复校准门控。
        """

        ordered = [item for item in categories if isinstance(item, dict)]
        primary = ordered[0] if ordered else None
        top_score = _score(primary)
        second_score = _score(ordered[1] if len(ordered) > 1 else None)
        top_ranking_score = _ranking_score(primary)
        second_ranking_score = _ranking_score(ordered[1] if len(ordered) > 1 else None)
        margin = (
            top_ranking_score - second_ranking_score if primary is not None else None
        )
        content_signals = _content_signals(primary)
        evidence_items = _located_evidence(primary)
        negative_signals = _string_list(primary, "negative_signals")
        reasons: list[str] = []

        if extraction_status != "COMPLETED":
            reasons.append("PARSE_FAILED")
        if not risk_passed:
            reasons.append("RISK_CHECK_FAILED")
        if primary is None:
            reasons.append("NO_TAXONOMY_CANDIDATE")
        else:
            if str(primary.get("name") or "") == "其他":
                reasons.append("OTHER_CATEGORY")
            if str(primary.get("source") or "") == "llm_free_path":
                reasons.append("FREE_PATH_NOT_ALLOWED")
            if str(primary.get("status") or "") == "NEEDS_REVIEW":
                reasons.append("EVIDENCE_MISSING")
            if not str(primary.get("category_id") or ""):
                reasons.append("NO_TAXONOMY_CANDIDATE")
            if not str(primary.get("taxonomy_key") or "") or not str(
                primary.get("taxonomy_version") or ""
            ):
                reasons.append("POLICY_VERSION_UNAVAILABLE")
            if not evidence_items:
                reasons.append("EVIDENCE_MISSING")
            # Top-1 直接落位测试期间暂时停用以下软拒绝条件。保留原判断，后续完成
            # 人工标注评估和阈值校准后可按版本化策略恢复。
            # if len(content_signals) < 2:
            #     reasons.append("FILENAME_ONLY_SIGNAL")
            # if negative_signals:
            #     reasons.append("NEGATIVE_SIGNAL_CONFLICT")
            # if top_score < self.settings.auto_classification_fallback_threshold:
            #     reasons.append("TOP_SCORE_BELOW_THRESHOLD")
            # if margin is None or margin < self.settings.auto_classification_fallback_margin:
            #     reasons.append("TOP_MARGIN_TOO_SMALL")
            # if primary.get("summary_fulltext_agreement") is False:
            #     reasons.append("SUMMARY_FULLTEXT_CONFLICT")

        # 原因码是稳定审计接口，必须顺序去重，不能把相同失败重复展示给用户。
        reason_codes = tuple(dict.fromkeys(reasons))
        return AutoPlacementPolicyResult(
            accepted=not reason_codes,
            primary_category=primary if not reason_codes else None,
            reason_codes=reason_codes,
            calibrated_confidence=top_score if primary is not None else None,
            required_threshold=self.settings.auto_classification_fallback_threshold,
            top_margin=margin,
            required_margin=self.settings.auto_classification_fallback_margin,
            feature_snapshot={
                "candidate_count": len(ordered),
                "content_signal_count": len(content_signals),
                "located_evidence_count": len(evidence_items),
                "negative_signal_count": len(negative_signals),
                "top_score": top_score if primary is not None else None,
                "second_score": second_score if len(ordered) > 1 else None,
                "top_ranking_score": top_ranking_score if primary is not None else None,
                "second_ranking_score": (
                    second_ranking_score if len(ordered) > 1 else None
                ),
                "summary_fulltext_agreement": (
                    primary.get("summary_fulltext_agreement") if primary is not None else None
                ),
                "soft_gate_mode": "top1_test_disabled",
                "calibration_mode": (
                    "global_conservative_fallback"
                    if self.settings.auto_classification_calibration_version == "unpublished"
                    else "published_calibration"
                ),
                "global_fallback_policy": (
                    self.settings.auto_classification_global_fallback_policy
                ),
            },
        )


def _score(category: dict[str, Any] | None) -> float:
    """读取有限范围分数，拒绝异常值污染门槛判断。"""

    if category is None:
        return 0.0
    try:
        return max(0.0, min(1.0, float(category.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _ranking_score(category: dict[str, Any] | None) -> float:
    """读取未饱和候选排序分，用于冷启动阶段区分两个高分候选。

    现有 rule-only 分类会把展示置信度封顶为 0.95，若直接计算间隔会把不同的
    高分候选错误压成同分。发布校准器后可在 ``candidate_scores`` 中提供明确的
    ``ranking_score`` 替换该兼容回退。
    """

    if category is None:
        return 0.0
    candidate_scores = dict(category.get("candidate_scores") or {})
    raw = candidate_scores.get(
        "ranking_score",
        candidate_scores.get("rule", category.get("rule_score", category.get("confidence", 0))),
    )
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _string_list(category: dict[str, Any] | None, key: str) -> list[str]:
    """读取候选中的非空字符串列表并保持顺序去重。"""

    if category is None:
        return []
    return list(
        dict.fromkeys(str(item).strip() for item in category.get(key, []) if str(item).strip())
    )


def _content_signals(category: dict[str, Any] | None) -> list[str]:
    """只统计正文实际命中的独立信号，不把文件名命中当作内容证据。"""

    return _string_list(category, "matched_content_signals")


def _located_evidence(category: dict[str, Any] | None) -> list[dict[str, Any]]:
    """筛选具有真实定位信息和原文引用的证据项。"""

    if category is None:
        return []
    result: list[dict[str, Any]] = []
    for item in category.get("evidence_items", []):
        if not isinstance(item, dict) or not str(item.get("quote") or "").strip():
            continue
        if item.get("page_number") is None and not item.get("sheet_name"):
            continue
        result.append(item)
    return result
