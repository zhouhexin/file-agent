"""拒收越界引用和敏感输出，校验不等于证明模型语义判断正确。"""

from __future__ import annotations

import re

from app.modules.evidence_answer.schemas import EvidencePackage
from app.modules.evidence_evaluation.schemas import EvidenceIssue, SemanticResponse


class EvaluationValidationError(ValueError):
    """仅暴露稳定错误码，避免异常文本携带模型响应或文件正文。"""


_SENSITIVE = re.compile(
    r"(?:(?<![\w])/(?:[^\s/]+/)*[^\s/]+|[A-Za-z]:[\\/]|"
    r"\\\\[^\\\s]+\\|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\b(?:api[_-]?key|password|secret|access_token)\s*[:=])",
    re.IGNORECASE,
)


def validate_safe_text(value: object) -> None:
    """只校验结果自由文本，原文只作为运行时输入，绝不写普通日志。"""

    if isinstance(value, str) and _SENSITIVE.search(value):
        raise EvaluationValidationError("UNSAFE_OUTPUT")
    if isinstance(value, dict):
        for item in value.values():
            validate_safe_text(item)
    if isinstance(value, list):
        for item in value:
            validate_safe_text(item)


def validate_semantics(response: SemanticResponse, package: EvidencePackage) -> None:
    """要求 ID 唯一，所有支持和冲突必须引用本批真实证据及原值。"""

    validate_safe_text(response.model_dump())
    evidence = {item.evidence_id: item for item in package.evidence_items}
    ids = [item.requirement_id for item in response.requirements]
    if len(set(ids)) != len(ids):
        raise EvaluationValidationError("DUPLICATE_REQUIREMENT_ID")
    for requirement in response.requirements:
        if not set(requirement.evidence_ids) <= evidence.keys():
            raise EvaluationValidationError("UNKNOWN_EVIDENCE_ID")
        if requirement.status == "SUPPORTED" and not requirement.evidence_ids:
            raise EvaluationValidationError("SUPPORT_WITHOUT_EVIDENCE")
    for conflict in response.conflicts:
        for fact in (conflict.left, conflict.right):
            if fact.evidence_id not in evidence:
                raise EvaluationValidationError("UNKNOWN_EVIDENCE_ID")
            if fact.value_quote not in evidence[fact.evidence_id].quote:
                raise EvaluationValidationError("UNGROUNDED_CONFLICT_VALUE")


def validate_issues(issues: list[EvidenceIssue], package: EvidencePackage) -> None:
    """最终双重检查引用；MISSING 可无引用，冲突必须至少两条不同证据。"""

    allowed = {item.evidence_id for item in package.evidence_items}
    if len(allowed) != len(package.evidence_items):
        raise EvaluationValidationError("DUPLICATE_EVIDENCE_ID")
    for issue in issues:
        validate_safe_text(issue.model_dump())
        if not set(issue.evidence_ids) <= allowed:
            raise EvaluationValidationError("UNKNOWN_EVIDENCE_ID")
        if issue.type == "CONFLICT" and len(set(issue.evidence_ids)) < 2:
            raise EvaluationValidationError("CONFLICT_NEEDS_TWO_EVIDENCES")
        if issue.type == "OUTDATED" and not issue.evidence_ids:
            raise EvaluationValidationError("VERSION_WITHOUT_EVIDENCE")
        if issue.type == "MISSING" and not issue.missing_requirements:
            raise EvaluationValidationError("MISSING_WITHOUT_REQUIREMENT")
