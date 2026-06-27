"""对话阶段文件正文的轻量确定性分类器。"""

from __future__ import annotations

from typing import Any


CATEGORY_RULES: list[dict[str, Any]] = [
    {"name": "奖学金", "keywords": ["奖学金", "一等奖", "二等奖", "三等奖", "助学金"]},
    {"name": "学生活动", "keywords": ["志愿", "社团", "活动", "竞赛", "实践"]},
    {"name": "成绩", "keywords": ["成绩", "绩点", "分数", "课程"]},
    {"name": "处分", "keywords": ["处分", "违纪", "警告"]},
]


def classify_document_text(text: str) -> list[dict[str, Any]]:
    """基于正文关键词返回可审计的基础分类建议。"""

    normalized_text = text or ""
    matched_categories: list[dict[str, Any]] = []
    for rule in CATEGORY_RULES:
        evidence = [keyword for keyword in rule["keywords"] if keyword in normalized_text]
        if not evidence:
            continue
        matched_categories.append(
            {
                "name": rule["name"],
                "confidence": min(0.95, 0.65 + len(evidence) * 0.1),
                "status": "SUGGESTED",
                "evidence": evidence[:3],
            }
        )

    if matched_categories:
        return sorted(matched_categories, key=lambda item: item["confidence"], reverse=True)

    return [
        {
            "name": "其他",
            "confidence": 0.2,
            "status": "SUGGESTED",
            "evidence": [],
        }
    ]
