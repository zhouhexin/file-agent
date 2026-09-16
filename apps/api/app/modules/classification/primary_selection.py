"""从候选、用途和人工事实中确定唯一 PRIMARY。

该模块不写正式关系或移动文件；无法可靠细分业务时优先使用可验证的
学校/学院—部门—发文/其他兜底，组织范围也未知时才返回 ``system.other``。
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
    scoped_fallbacks = _valid_scoped_fallback_candidates(taxonomy, candidates)
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
    evidence_backed = [
        item for item in normalized if _is_evidence_backed_business_candidate(item)
    ]
    if risk_passed and extraction_status in {"COMPLETED", "PARTIAL"} and evidence_backed:
        primary = evidence_backed[0]
        competitor = next(
            (
                item
                for item in evidence_backed[1:]
                if not _same_business_branch(primary, item)
            ),
            None,
        )
        if (
            competitor is None
            or _primary_ranking_score(primary) > _primary_ranking_score(competitor)
            or _has_clear_document_theme_advantage(primary, competitor)
        ):
            return _result(
                primary=primary,
                candidates=normalized,
                outcome=ClassificationOutcome.CLASSIFIED,
                quality=ClassificationQuality.SUFFICIENT,
                basis="EVIDENCE_BACKED_BUSINESS_RULE",
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
    fallback = scoped_fallbacks[0] if scoped_fallbacks else _system_other(taxonomy)
    if scoped_fallbacks:
        reasons.append("SCOPED_ORGANIZATION_FALLBACK")
    return _result(
        primary=fallback,
        candidates=normalized,
        outcome=(
            ClassificationOutcome.CLASSIFIED
            if scoped_fallbacks
            else ClassificationOutcome.OTHER
        ),
        quality=quality,
        basis=("SCOPED_FALLBACK" if scoped_fallbacks else "FALLBACK"),
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
        item
        for item in candidates
        if str(item.get("category_id") or "") != primary_id
        # 业务 PRIMARY 已经确定时，兜底节点不应以次级建议形式混入解释结果。
        # 正常流程会在前置过滤中排除 fallback；此处保留防御性约束，兼容历史调用方。
        and not (
            primary_id != "system.other"
            and str(item.get("category_id") or "") == "system.other"
        )
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
    """对齐组织镜像并排除 fallback、自由路径和历史复核候选。"""

    node_by_id = {
        item.category_id: item
        for item in flatten_category_paths(taxonomy)
        if item.category_id
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_candidate in candidates:
        candidate = _align_candidate_to_organization_scope(
            candidate=raw_candidate,
            node_by_id=node_by_id,
        )
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
    result.sort(key=lambda item: (-_primary_ranking_score(item), -_scope_score(item)))
    return result[:8]


def _align_candidate_to_organization_scope(
    *,
    candidate: dict[str, Any],
    node_by_id: dict[str, Any],
) -> dict[str, Any]:
    """正文范围明确时，把同一业务候选切换到学校/学院镜像节点。

    该步骤不新增业务含义，也不复用来源路径；它只纠正“业务候选属于学院镜像，
    但正文明确面向学校”这一类组织根不一致。没有受控镜像节点时保持原候选，
    后续证据门槛仍会按组织冲突拒绝它。
    """

    organization_scope = str(candidate.get("organization_scope") or "")
    category_id = str(candidate.get("category_id") or "")
    if organization_scope not in {"学校", "学院"}:
        return candidate
    if category_id.startswith("school."):
        current_scope = "学校"
        mirror_id = f"college.{category_id.removeprefix('school.')}"
    elif category_id.startswith("college."):
        current_scope = "学院"
        mirror_id = f"school.{category_id.removeprefix('college.')}"
    else:
        return candidate
    if current_scope == organization_scope:
        return candidate
    mirror = node_by_id.get(mirror_id)
    if (
        mirror is None
        or mirror.node_kind in {CategoryNodeKind.FALLBACK, CategoryNodeKind.GROUP}
        or not mirror.primary_enabled
    ):
        return candidate
    scores = dict(candidate.get("candidate_scores") or {})
    aligned_scope_score = max(float(scores.get("scope") or 0.0), 0.85)
    return {
        **candidate,
        "category_id": mirror.category_id,
        "category_path": list(mirror.path),
        "name": "/".join(mirror.path),
        "candidate_scores": {**scores, "scope": aligned_scope_score},
        "candidate_reason": (
            f"{str(candidate.get('candidate_reason') or '')}；"
            f"正文组织范围为{organization_scope}，切换到同业务镜像节点"
        ).strip("；"),
    }


def _valid_scoped_fallback_candidates(
    taxonomy: Taxonomy, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """读取 matcher 单独生成的组织兜底；FALLBACK 仍不得进入业务竞争。"""

    node_by_id = {
        item.category_id: item
        for item in flatten_category_paths(taxonomy)
        if item.category_id
    }
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        category_id = str(candidate.get("category_id") or "")
        node = node_by_id.get(category_id)
        if (
            node is None
            or node.node_kind != CategoryNodeKind.FALLBACK
            or not node.primary_enabled
            or category_id == "system.other"
            or str(candidate.get("source") or "") != "rule_fallback"
            or list(candidate.get("category_path") or [])[:1]
            not in (["学校"], ["学院"])
        ):
            continue
        result.append(candidate)
    result.sort(
        key=lambda item: (
            -float(dict(item.get("candidate_scores") or {}).get("department") or 0.0),
            -float(dict(item.get("candidate_scores") or {}).get("document_number") or 0.0),
            -float(item.get("confidence") or 0.0),
        )
    )
    return result[:1]


def _primary_ranking_score(candidate: dict[str, Any]) -> float:
    """让标题和正文开头的主旨优势先参与排序，避免高频背景词抢占 Top-1。"""

    scores = dict(candidate.get("candidate_scores") or {})
    title_theme = float(scores.get("title_theme") or 0.0)
    leading_body = float(scores.get("leading_body") or 0.0)
    return (
        _business_score(candidate)
        + min(0.15, title_theme * 0.6)
        + min(0.04, leading_body * 0.15)
    )


def _is_evidence_backed_business_candidate(candidate: dict[str, Any]) -> bool:
    """只让正文证据充分、范围一致且无冲突的业务候选参与 PRIMARY 竞争。"""

    category_path = list(candidate.get("category_path") or [])
    organization_scope = candidate.get("organization_scope")
    if (
        category_path[:1] in (["学校"], ["学院"])
        and organization_scope in {"学校", "学院"}
        and organization_scope != category_path[0]
    ):
        # 正文业务对象不能反向猜测组织层级；学校/学院镜像节点只有在
        # 已检测到相反组织证据时才拒绝；范围未知不等于组织冲突。
        return False
    scores = dict(candidate.get("candidate_scores") or {})
    # 长文中的偶然负向词只用于扣分；只有标题或正文开头形成的明确冲突才阻止落位。
    if bool(scores.get("negative_conflict")):
        return False
    evidence = [item for item in candidate.get("evidence_items", []) if isinstance(item, dict)]
    located = any(
        str(item.get("quote") or "").strip()
        and (item.get("page_number") is not None or bool(item.get("sheet_name")))
        for item in evidence
    )
    minimum_score = _minimum_business_score(candidate)
    return located and _business_score(candidate) >= minimum_score


def _minimum_business_score(candidate: dict[str, Any]) -> float:
    """按主旨位置和独立证据数量设置分层门槛，扩大具体分类覆盖但拒绝单个泛词。"""

    scores = dict(candidate.get("candidate_scores") or {})
    title_theme = float(scores.get("title_theme") or 0.0)
    leading_body = float(scores.get("leading_body") or 0.0)
    evidence_support = float(scores.get("evidence_support") or 0.0)
    if title_theme >= 0.16 and evidence_support >= 0.35:
        return 0.28
    if leading_body >= 0.16 and evidence_support >= 0.35:
        return 0.30
    if evidence_support >= 0.55:
        return 0.35
    return 0.45


def _has_clear_document_theme_advantage(
    primary: dict[str, Any], competitor: dict[str, Any]
) -> bool:
    """主标题明显支持一个分支时，不让长正文中的背景词制造虚假歧义。"""

    primary_scores = dict(primary.get("candidate_scores") or {})
    competitor_scores = dict(competitor.get("candidate_scores") or {})
    primary_title = float(primary_scores.get("title_theme") or 0.0)
    competitor_title = float(competitor_scores.get("title_theme") or 0.0)
    if primary_title < 0.16 or primary_title - competitor_title < 0.08:
        return False
    # 标题优势只能化解接近候选，不能覆盖业务分明显更高的正文证据。
    return _business_score(primary) + 0.12 >= _business_score(competitor)


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
