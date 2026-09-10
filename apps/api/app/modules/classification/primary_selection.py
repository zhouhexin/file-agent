"""从候选、用途和人工事实中确定唯一 PRIMARY。

该模块不写正式关系或移动文件；无法可靠细分时返回 ``system.other`` 完成结果，不生成分类复核任务。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.modules.classification.matcher import flatten_category_paths
from app.modules.classification.schemas import CategoryNodeKind, Taxonomy


class ClassificationOutcome(StrEnum):
    """公开分类结果只允许业务分类或 OTHER。"""

    CLASSIFIED = "CLASSIFIED"
    OTHER = "OTHER"


class ClassificationQuality(StrEnum):
    """内部质量诊断，不映射成用户复核队列。"""

    SUFFICIENT = "SUFFICIENT"
    AMBIGUOUS = "AMBIGUOUS"
    INSUFFICIENT = "INSUFFICIENT"


class PrimarySelectionResult(BaseModel):
    """一次唯一主类选择的可持久化结构。"""

    model_config = ConfigDict(extra="forbid")

    primary_candidate: dict[str, Any]
    secondary_candidates: list[dict[str, Any]] = Field(default_factory=list)
    classification_outcome: ClassificationOutcome
    classification_quality: ClassificationQuality
    selection_basis: str
    reason_codes: list[str] = Field(default_factory=list)
    policy_version: str
    input_fingerprint: str


def select_primary_category(
    *,
    taxonomy: Taxonomy,
    candidates: list[dict[str, Any]],
    input_fingerprint: str,
    policy_version: str = "workdata-v1",
    extraction_status: str = "COMPLETED",
    risk_passed: bool = True,
    explicit_target: dict[str, Any] | None = None,
    existing_human_primary: dict[str, Any] | None = None,
    purpose_candidate: dict[str, Any] | None = None,
) -> PrimarySelectionResult:
    """按明确目标、人工主类、用途包、强业务、父类、OTHER 的固定顺序选择。"""

    normalized = _valid_business_candidates(taxonomy, candidates)
    if explicit_target is not None and _is_primary_enabled(taxonomy, explicit_target):
        return _result(
            primary=explicit_target,
            candidates=normalized,
            outcome=ClassificationOutcome.CLASSIFIED,
            quality=ClassificationQuality.SUFFICIENT,
            basis="EXPLICIT_TARGET",
            reasons=[],
            policy_version=policy_version,
            input_fingerprint=input_fingerprint,
        )
    if existing_human_primary is not None and _is_primary_enabled(
        taxonomy, existing_human_primary
    ):
        return _result(
            primary=existing_human_primary,
            candidates=normalized,
            outcome=(
                ClassificationOutcome.OTHER
                if existing_human_primary.get("category_id") == "system.other"
                else ClassificationOutcome.CLASSIFIED
            ),
            quality=ClassificationQuality.SUFFICIENT,
            basis="EXISTING_HUMAN_PRIMARY",
            reasons=[],
            policy_version=policy_version,
            input_fingerprint=input_fingerprint,
        )
    if purpose_candidate is not None and _is_primary_enabled(taxonomy, purpose_candidate):
        return _result(
            primary=purpose_candidate,
            candidates=normalized,
            outcome=ClassificationOutcome.CLASSIFIED,
            quality=ClassificationQuality.SUFFICIENT,
            basis="VERIFIED_PACKAGE",
            reasons=[],
            policy_version=policy_version,
            input_fingerprint=input_fingerprint,
        )

    reasons: list[str] = []
    quality = ClassificationQuality.INSUFFICIENT
    if not risk_passed:
        reasons.append("RISK_CHECK_FAILED")
    if extraction_status not in {"COMPLETED", "PARTIAL"}:
        reasons.append("PARSE_UNAVAILABLE")
    strong = [item for item in normalized if _is_strong_business_candidate(item)]
    if risk_passed and extraction_status in {"COMPLETED", "PARTIAL"} and strong:
        primary = strong[0]
        competitor = next(
            (
                item
                for item in strong[1:]
                if not _same_business_branch(primary, item)
            ),
            None,
        )
        if competitor is None or _business_score(primary) - _business_score(competitor) >= 0.15:
            return _result(
                primary=primary,
                candidates=normalized,
                outcome=ClassificationOutcome.CLASSIFIED,
                quality=ClassificationQuality.SUFFICIENT,
                basis="STRONG_BUSINESS_RULE",
                reasons=[],
                policy_version=policy_version,
                input_fingerprint=input_fingerprint,
            )
        quality = ClassificationQuality.AMBIGUOUS
        reasons.append("AMBIGUOUS_PRIMARY")
    elif not normalized:
        reasons.append("NO_BUSINESS_EVIDENCE")
    else:
        reasons.append("LOW_QUALITY")
    if normalized and not any(item.get("organization_scope") for item in normalized):
        reasons.append("UNKNOWN_SCOPE")
    return _result(
        primary=_system_other(taxonomy),
        candidates=normalized,
        outcome=ClassificationOutcome.OTHER,
        quality=quality,
        basis="FALLBACK",
        reasons=list(dict.fromkeys(reasons)),
        policy_version=policy_version,
        input_fingerprint=input_fingerprint,
    )


def _result(
    *,
    primary: dict[str, Any],
    candidates: list[dict[str, Any]],
    outcome: ClassificationOutcome,
    quality: ClassificationQuality,
    basis: str,
    reasons: list[str],
    policy_version: str,
    input_fingerprint: str,
) -> PrimarySelectionResult:
    """构造结果并确保 PRIMARY 不会在辅助候选中重复。"""

    primary_id = str(primary.get("category_id") or "")
    secondaries = [
        item for item in candidates if str(item.get("category_id") or "") != primary_id
    ]
    return PrimarySelectionResult(
        primary_candidate=primary,
        secondary_candidates=secondaries[:7],
        classification_outcome=outcome,
        classification_quality=quality,
        selection_basis=basis,
        reason_codes=reasons,
        policy_version=policy_version,
        input_fingerprint=input_fingerprint,
    )


def _valid_business_candidates(
    taxonomy: Taxonomy, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """排除 fallback、自由路径和历史复核候选，保留最多八个稳定候选。"""

    node_by_id = {
        item.category_id: item
        for item in flatten_category_paths(taxonomy)
        if item.category_id
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        category_id = str(candidate.get("category_id") or "")
        node = node_by_id.get(category_id)
        if (
            not category_id
            or category_id in seen
            or node is None
            or node.node_kind in {CategoryNodeKind.FALLBACK, CategoryNodeKind.GROUP}
            or not node.primary_enabled
            or candidate.get("source") == "llm_free_path"
            or candidate.get("status") == "NEEDS_REVIEW"
        ):
            continue
        seen.add(category_id)
        result.append(candidate)
    result.sort(key=lambda item: (-_business_score(item), -_scope_score(item)))
    return result[:8]


def _is_strong_business_candidate(candidate: dict[str, Any]) -> bool:
    """冷启动只接受版本化强规则、明确表单或局部完整简历。"""

    basis = str(candidate.get("purpose_basis") or "")
    scores = dict(candidate.get("candidate_scores") or {})
    category_path = list(candidate.get("category_path") or [])
    if (
        category_path[:1] in (["学校"], ["学院"])
        and candidate.get("organization_scope") != category_path[0]
    ):
        # 正文业务对象不能反向猜测组织层级；学校/学院镜像节点只有在
        # 独立组织证据与目标分支一致时才可自动生效。
        return False
    if basis not in {"VERSIONED_RULE", "TITLE_FORM", "LOCAL_RESUME_WINDOW"}:
        if not (
            basis == "CONTENT_RULE"
            and _business_score(candidate) >= 0.75
            and float(scores.get("evidence_support") or 0.0) >= 0.5
            and _scope_score(candidate) >= 0.1
        ):
            return False
    evidence = [item for item in candidate.get("evidence_items", []) if isinstance(item, dict)]
    located = any(
        str(item.get("quote") or "").strip()
        and (item.get("page_number") is not None or bool(item.get("sheet_name")))
        for item in evidence
    )
    return located and _business_score(candidate) >= 0.45


def _is_primary_enabled(taxonomy: Taxonomy, candidate: dict[str, Any]) -> bool:
    """明确目标也只能指向当前 taxonomy 中可落位节点。"""

    category_id = str(candidate.get("category_id") or "")
    return any(
        item.category_id == category_id and item.primary_enabled
        for item in flatten_category_paths(taxonomy)
    )


def _system_other(taxonomy: Taxonomy) -> dict[str, Any]:
    """读取唯一新版 fallback；配置缺失时立即失败，禁止另造节点。"""

    node = next(
        (item for item in flatten_category_paths(taxonomy) if item.category_id == "system.other"),
        None,
    )
    if node is None or not node.primary_enabled:
        raise ValueError("当前 taxonomy 缺少可落位的 system.other")
    return {
        "name": "/".join(node.path),
        "category_id": node.category_id,
        "category_path": node.path,
        "confidence": 0.0,
        "status": "SUGGESTED",
        "source": "system_fallback",
        "purpose_basis": "FALLBACK",
        "evidence": [],
        "evidence_items": [],
        "candidate_scores": {"business": 0.0, "scope": 0.0, "evidence_support": 0.0},
        "taxonomy_key": taxonomy.key,
        "taxonomy_version": taxonomy.version,
    }


def _business_score(candidate: dict[str, Any]) -> float:
    """读取候选业务分，禁止把 scope 分混入。"""

    scores = dict(candidate.get("candidate_scores") or {})
    return float(scores.get("business", candidate.get("rule_score", 0.0)) or 0.0)


def _scope_score(candidate: dict[str, Any]) -> float:
    """组织范围只用于同业务候选的次级排序。"""

    return float(dict(candidate.get("candidate_scores") or {}).get("scope", 0.0) or 0.0)


def _same_business_branch(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """父子候选或学校/学院镜像不互相制造假歧义。"""

    left_id = str(left.get("category_id") or "")
    right_id = str(right.get("category_id") or "")
    if left_id.startswith(f"{right_id}.") or right_id.startswith(f"{left_id}."):
        return True
    return left_id.removeprefix("school.") == right_id.removeprefix("college.")
