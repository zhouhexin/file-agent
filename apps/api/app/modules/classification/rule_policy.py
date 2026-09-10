"""加载并执行版本化的 workdata 确定性分类规则。

规则文件只允许固定字段与组合操作符，不执行表达式、脚本或动态 Python；真实正文只在调用期间读取，
不会写入规则对象或 AgentGraphState。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_RULE_POLICY_PATH = (
    Path(__file__).resolve().parents[5]
    / "rules"
    / "classification-policies"
    / "workdata-v1.json"
)


class SignalGroups(BaseModel):
    """受控信号组合；每个内层数组表示可互换的一组表达。"""

    model_config = ConfigDict(extra="forbid")

    all_groups: list[list[str]] = Field(default_factory=list)
    any_group: list[list[str]] = Field(default_factory=list)
    same_section: list[list[str]] = Field(default_factory=list)


class ScopePolicy(BaseModel):
    """组织范围只作为独立信号，不得单独成立业务分类。"""

    model_config = ConfigDict(extra="forbid")

    preferred_roots: list[str] = Field(default_factory=list)
    required: bool = False


class EvidenceRequirements(BaseModel):
    """规则成立所需的最少可定位业务证据。"""

    model_config = ConfigDict(extra="forbid")

    minimum_groups: int = Field(default=1, ge=1, le=8)
    require_body_signal: bool = True


class ClassificationRule(BaseModel):
    """单条可回放规则，回归样本 ID 不参与在线计算。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(pattern=r"^[a-z][a-z0-9._-]+$")
    version: str = Field(min_length=1)
    candidate_category_ids: list[str] = Field(min_length=1)
    subject_signals: SignalGroups
    action_signals: SignalGroups
    title_signals: SignalGroups
    negative_contexts: list[str] = Field(default_factory=list)
    scope_policy: ScopePolicy
    evidence_requirements: EvidenceRequirements
    regression_sample_ids: list[str] = Field(default_factory=list)


class ClassificationRulePolicy(BaseModel):
    """生产规则集及其所有版本化门槛。"""

    model_config = ConfigDict(extra="forbid")

    policy_id: str
    version: str
    policy_mode: str = "conservative_rules"
    candidate_limit: int = Field(default=8, ge=1, le=8)
    resume_window_paragraphs: int = Field(default=12, ge=1, le=12)
    resume_window_characters: int = Field(default=3000, ge=200, le=3000)
    rules: list[ClassificationRule]

    @model_validator(mode="after")
    def validate_unique_rule_ids(self) -> "ClassificationRulePolicy":
        """规则 ID 必须唯一，避免不同含义静默覆盖。"""

        rule_ids = [rule.rule_id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("分类规则 ID 重复")
        return self


@dataclass(frozen=True)
class RulePolicyMatch:
    """规则层输出的业务候选摘要。"""

    category_id: str
    rule_id: str
    business_score: float
    evidence_support: float
    matched_signals: tuple[str, ...]
    negative_signals: tuple[str, ...]
    reason: str


def load_rule_policy(path: Path = DEFAULT_RULE_POLICY_PATH) -> ClassificationRulePolicy:
    """从固定 JSON 文件加载并严格校验规则策略。"""

    with path.open("r", encoding="utf-8") as file:
        return ClassificationRulePolicy.model_validate(json.load(file))


def evaluate_rule_policy(
    *,
    filename: str,
    title: str,
    body_text: str,
    organization_root: str | None,
    policy: ClassificationRulePolicy | None = None,
) -> list[RulePolicyMatch]:
    """执行白名单组合规则；组织范围只作加分或必需条件，不单独命中业务。"""

    active_policy = policy or load_rule_policy()
    title_text = "\n".join(value for value in (filename, title) if value)
    full_text = "\n".join(value for value in (title_text, body_text) if value)
    sections = [part.strip() for part in body_text.splitlines() if part.strip()]
    matches: list[RulePolicyMatch] = []
    for rule in active_policy.rules:
        if (
            rule.scope_policy.required
            and rule.scope_policy.preferred_roots
            and organization_root not in rule.scope_policy.preferred_roots
        ):
            continue
        subject = _match_signal_groups(rule.subject_signals, full_text, sections)
        action = _match_signal_groups(rule.action_signals, full_text, sections)
        title_hits = _match_signal_groups(rule.title_signals, title_text, [title_text])
        positive_groups = int(bool(subject)) + int(bool(action)) + int(bool(title_hits))
        if positive_groups < rule.evidence_requirements.minimum_groups:
            continue
        body_hits = [signal for signal in [*subject, *action] if signal in body_text]
        if rule.evidence_requirements.require_body_signal and not body_hits:
            continue
        negative = _prefer_specific(
            [signal for signal in rule.negative_contexts if signal and signal in full_text]
        )
        positive = _prefer_specific([*subject, *action, *title_hits])
        if not positive:
            continue
        score = min(1.0, 0.22 * positive_groups + 0.08 * len(positive))
        score -= min(0.6, 0.15 * len(negative))
        if score <= 0:
            continue
        evidence_support = min(1.0, 0.25 * len(_prefer_specific(body_hits)))
        for category_id in rule.candidate_category_ids:
            matches.append(
                RulePolicyMatch(
                    category_id=category_id,
                    rule_id=rule.rule_id,
                    business_score=round(score, 4),
                    evidence_support=round(evidence_support, 4),
                    matched_signals=tuple(positive),
                    negative_signals=tuple(negative),
                    reason=f"版本化规则 {rule.rule_id} 命中：{'、'.join(positive[:5])}",
                )
            )
    matches.sort(key=lambda item: (-item.business_score, -item.evidence_support, item.rule_id))
    return matches[: active_policy.candidate_limit]


def _match_signal_groups(
    groups: SignalGroups,
    text: str,
    sections: list[str],
) -> list[str]:
    """按 all_groups/any_group/same_section 三种固定操作符匹配。"""

    matched: list[str] = []
    if groups.all_groups:
        all_hits = [_first_hit(group, text) for group in groups.all_groups]
        if any(hit is None for hit in all_hits):
            return []
        matched.extend(hit for hit in all_hits if hit)
    if groups.any_group:
        any_hits = [_first_hit(group, text) for group in groups.any_group]
        if not any(any_hits):
            return []
        matched.extend(hit for hit in any_hits if hit)
    if groups.same_section:
        section_hits: list[str] | None = None
        for section in sections or [text]:
            current = [_first_hit(group, section) for group in groups.same_section]
            if all(current):
                section_hits = [hit for hit in current if hit]
                break
        if section_hits is None:
            return []
        matched.extend(section_hits)
    return _prefer_specific(matched)


def _first_hit(signals: list[str], text: str) -> str | None:
    """一组同义信号只返回最长命中，避免短词重复放大。"""

    hits = [signal for signal in signals if signal and signal in text]
    return max(hits, key=len) if hits else None


def _prefer_specific(signals: list[str]) -> list[str]:
    """按原顺序去重，并移除被更长表达包含的短信号。"""

    unique = list(dict.fromkeys(signals))
    return [
        signal
        for signal in unique
        if not any(signal != other and signal in other for other in unique)
    ]
