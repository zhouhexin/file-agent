"""文件检索语义计划的受控 schema 与确定性结果整理。

LLM 只能选择完整主题、机构约束和展示偏好。真实文件范围、检索执行、结果分组和
机构层级判断仍由后端完成，模型不能生成 SQL、路径或文件事实。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


OrganizationLevel = Literal[
    "ANY",
    "UNIVERSITY",
    "COLLEGE",
    "DEPARTMENT",
    "SPECIAL_TOPIC",
]


class SearchPhraseConstraint(BaseModel):
    """必须以完整短语执行的主题或机构约束。"""

    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(min_length=2, max_length=30)
    required: bool = True
    match_mode: Literal["EXACT_PHRASE"] = "EXACT_PHRASE"

    @field_validator("phrase")
    @classmethod
    def normalize_phrase(cls, value: str) -> str:
        """规范化空白并拒绝控制字符，避免短语绕过 Tool 边界。"""

        normalized = " ".join(str(value or "").strip().split())
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("search phrase contains control characters")
        return normalized


class SearchOrganizationScope(BaseModel):
    """学校工作区中的机构层级与明确机构名称约束。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["CURRENT_SCHOOL_WORKSPACE"] = "CURRENT_SCHOOL_WORKSPACE"
    organization_level: OrganizationLevel = "ANY"
    organization_terms: list[SearchPhraseConstraint] = Field(
        default_factory=list,
        max_length=4,
    )


class SearchPreferredResult(BaseModel):
    """只影响稳定排序、不排除其它高相关结果的机构层级偏好。"""

    model_config = ConfigDict(extra="forbid")

    organization_level: Literal[
        "UNIVERSITY",
        "COLLEGE",
        "DEPARTMENT",
        "SPECIAL_TOPIC",
    ]
    boost: float = Field(default=1.0, ge=0, le=2)


class FileSearchSemanticPlan(BaseModel):
    """LLM 或确定性降级可以生成的声明式文件检索计划。"""

    model_config = ConfigDict(extra="forbid")

    core_topics: list[SearchPhraseConstraint] = Field(min_length=1, max_length=4)
    scope: SearchOrganizationScope = Field(default_factory=SearchOrganizationScope)
    preferred_results: list[SearchPreferredResult] = Field(
        default_factory=list,
        max_length=4,
    )
    group_by: list[
        Literal["organization_level", "business_topic", "year"]
    ] = Field(
        default_factory=lambda: ["organization_level", "year"],
        max_length=3,
    )
    response_style: Literal["GROUPED_FILE_LIST", "FLAT_FILE_LIST"] = (
        "GROUPED_FILE_LIST"
    )

    @field_validator("group_by")
    @classmethod
    def deduplicate_group_fields(cls, values: list[str]) -> list[str]:
        """保持模型给出的分组顺序，同时移除重复字段。"""

        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def require_exact_core_topic(self) -> "FileSearchSemanticPlan":
        """至少保留一个必需核心短语，禁止把计划降级成宽泛 OR 查询。"""

        if not any(item.required for item in self.core_topics):
            raise ValueError("semantic search requires a required core topic")
        return self

    def protected_phrases(self) -> list[str]:
        """返回所有不能拆分的必需主题和机构短语。"""

        return list(
            dict.fromkeys(
                item.phrase
                for item in [
                    *self.core_topics,
                    *self.scope.organization_terms,
                ]
                if item.required
            )
        )


_PROTECTED_DOCUMENT_TOPICS = (
    "工作总结",
    "述职报告",
    "工作计划",
    "会议纪要",
)


def build_deterministic_semantic_plan(query: str) -> FileSearchSemanticPlan | None:
    """LLM 不可用时为常见复合文种保留完整短语。

    该降级只保护能确定识别的文种，不猜测任意业务同义词。用户说“学校的工作总结”
    时，“学校”表示当前学校业务工作区，而不是每个结果必须出现的字面词；只有
    “只找学校层面”等明确限定才收窄为校级文件。
    """

    text = re.sub(r"\s+", "", str(query or ""))
    topic = next((item for item in _PROTECTED_DOCUMENT_TOPICS if item in text), None)
    if not topic:
        return None

    school_only = bool(
        re.search(r"(?:只|仅)(?:查找|搜索|找|要)?学校(?:层面|级)", text)
        or re.search(r"学校(?:层面|级)(?:的)?", text)
    )
    preferred = []
    if "学校" in text and not school_only:
        preferred = [
            SearchPreferredResult(
                organization_level="UNIVERSITY",
                boost=1.0,
            )
        ]
    organization_terms = [
        SearchPhraseConstraint(phrase=value)
        for value in _extract_explicit_organization_terms(text, topic=topic)
    ]
    return FileSearchSemanticPlan(
        core_topics=[SearchPhraseConstraint(phrase=topic)],
        scope=SearchOrganizationScope(
            organization_level="UNIVERSITY" if school_only else "ANY",
            organization_terms=organization_terms,
        ),
        preferred_results=preferred,
        group_by=["organization_level", "business_topic", "year"],
        response_style="GROUPED_FILE_LIST",
    )


def _extract_explicit_organization_terms(
    text: str, *, topic: str
) -> list[str]:
    """提取紧邻文种的明确机构名称，忽略泛指“学校”。"""

    pattern = re.compile(
        rf"(?:^|(?:19|20)\d{{2}}年|查找|搜索|检索|找)"
        rf"([\u4e00-\u9fff]{{2,16}}(?:学院|书院|大学|中心|办公室|处|部))"
        rf"(?:的)?{re.escape(topic)}"
    )
    values: list[str] = []
    for match in pattern.finditer(text):
        value = str(match.group(1) or "").strip()
        if value and value not in {"学校"} and value not in values:
            values.append(value)
    return values[:4]


def apply_semantic_result_plan(
    *,
    result: dict[str, Any],
    plan: FileSearchSemanticPlan,
) -> dict[str, Any]:
    """按后端可见元数据执行机构过滤、偏好排序和稳定分组。"""

    rows: list[dict[str, Any]] = []
    for raw_item in result.get("results", []):
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        item["organization_level"] = infer_organization_level(item)
        item["business_topic"] = _business_topic(item, plan=plan)
        rows.append(item)

    required_level = plan.scope.organization_level
    if required_level != "ANY":
        rows = [
            item
            for item in rows
            if item.get("organization_level") == required_level
        ]

    boosts = {
        item.organization_level: item.boost for item in plan.preferred_results
    }
    rows.sort(
        key=lambda item: (
            -float(boosts.get(item.get("organization_level"), 0.0)),
            -_safe_year(item.get("year")),
            str(item.get("filename") or ""),
            str(item.get("working_copy_id") or item.get("document_id") or ""),
        )
    )
    supported_count = sum(
        1 for item in rows if item.get("relevance_tier") != "POSSIBLE"
    )
    possible_count = len(rows) - supported_count
    return {
        **result,
        "total_returned": len(rows),
        "supported_count": supported_count,
        "possible_count": possible_count,
        "results": rows,
        "semantic_plan": plan.model_dump(),
        "result_groups": _group_results(rows, plan=plan),
    }


def infer_organization_level(item: dict[str, Any]) -> str:
    """从可展示文件名、逻辑相对路径和分类中确定性推断机构层级。"""

    filename = str(item.get("filename") or "")
    relative_path = str(item.get("relative_path") or "")
    category_text = "/".join(
        str(value) for value in item.get("category_path", []) if value
    )
    text = " ".join([filename, relative_path, category_text])
    # 正式校名或学校级标题优先于存放目录。校级文件可能位于“学院向学校提交”
    # 等业务目录下，不能因路径里出现“学院”而误判为院级总结。
    if re.search(
        r"(?:大学(?:委员会)?.{0,16}工作总结|学校(?:年度)?工作总结)",
        filename,
    ):
        return "UNIVERSITY"
    if re.search(r"(?:学院|书院)", filename):
        return "COLLEGE"
    if re.search(r"(?:综合治理|安全稳定|工会|校庆|教学工作)", text):
        return "SPECIAL_TOPIC"
    if re.search(r"(?:学校文件|大学(?:委员会)?)", text):
        return "UNIVERSITY"
    if re.search(r"(?:学院|书院)", relative_path):
        return "COLLEGE"
    if re.search(r"(?:党委|组织部|宣传部|处|中心|办公室)", text):
        return "DEPARTMENT"
    return "SPECIAL_TOPIC"


def _business_topic(
    item: dict[str, Any], *, plan: FileSearchSemanticPlan
) -> str:
    """优先使用已持久化分类，否则回退到核心检索主题。"""

    category_path = [
        str(value) for value in item.get("category_path", []) if value
    ]
    if category_path:
        return category_path[-1]
    return plan.core_topics[0].phrase


def _group_results(
    rows: list[dict[str, Any]], *, plan: FileSearchSemanticPlan
) -> list[dict[str, Any]]:
    """生成前端和回执可直接消费的稳定分组，不让 LLM重排文件事实。"""

    if plan.response_style != "GROUPED_FILE_LIST" or not plan.group_by:
        return []
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for item in rows:
        key = tuple(_group_value(item, field) for field in plan.group_by)
        groups.setdefault(key, []).append(item)
    return [
        {
            "group_by": list(plan.group_by),
            "group_values": list(key),
            "count": len(items),
            "results": items,
        }
        for key, items in groups.items()
    ]


def _group_value(item: dict[str, Any], field: str) -> str:
    """把允许的分组字段转换为可展示文本。"""

    if field == "year":
        return str(item.get("year") or "未识别年份")
    return str(item.get(field) or "未分类")


def _safe_year(value: Any) -> int:
    """把不可信元数据安全转换为排序年份。"""

    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
