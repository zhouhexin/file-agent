"""Deterministic cell formatting for teacher recruitment workbooks only."""

from __future__ import annotations

import re

from app.modules.reference_catalogs.service import ReferenceCatalogError, catalog_info, query_catalog


_COLUMNS = {"I": "EDUCATION", "J": "PAPER", "K": "PAPER", "L": "PAPER",
            "M": "PROJECT", "N": "PROJECT", "O": "PROJECT", "P": "ACHIEVEMENT"}
_STAGES = ("博士", "硕士", "本科")
_ZONE_RE = re.compile(
    r"(?:SCI|SSCI|A&HCI)(?:\s*/\s*(?:SCI|SSCI|A&HCI))*"
    r"\s*[-：:]?\s*(?:中科院\s*)?([1-4一二三四])\s*区",
    re.I,
)
_CCF_RE = re.compile(r"(?<![A-Za-z0-9])CCF(?![A-Za-z0-9])", re.I)
_CCF_GRADE_RE = re.compile(
    r"(?<![A-Za-z0-9])CCF[ \t]*[-：:]?[ \t]*[ABC](?![A-Za-z0-9+])"
    r"[ \t]*(?:类|级)?[ \t]*(?:会议|期刊)?",
    re.I,
)


def _ccf_grade_first(content: str) -> str:
    """Move an evidenced CCF grade ahead of the other facts in one paper item."""
    match = _CCF_GRADE_RE.search(content)
    if match is None:
        return content
    grade = match.group().strip()
    before = content[:match.start()].strip(" \t,，;；、")
    after = content[match.end():].strip(" \t,，;；、")
    return ",".join(part for part in (grade, before, after) if part)


def render_teacher_cell(field, *, column: str, db) -> tuple[str, list[dict]]:
    """Derive display text exclusively from verified fragments and frozen catalog facts."""
    spec = field.teacher_render
    if not spec or _COLUMNS.get(column) != spec.kind or not field.fragments:
        raise ValueError("TEACHER_RENDER_INVALID:列、格式和正式原文片段不匹配")
    audit = []
    lines = []
    ordinals = []
    stages = {}
    catalog_version = None
    for item in spec.items:
        if spec.kind == "EDUCATION":
            if item.stage not in _STAGES or item.ordinal is not None or item.stage in stages:
                raise ValueError("TEACHER_RENDER_INVALID:学历阶段重复或次序字段无效")
            stages[item.stage] = item
        else:
            if item.stage is not None or item.ordinal is None or item.ordinal in ordinals:
                raise ValueError("TEACHER_RENDER_INVALID:事项序号重复或缺失")
            ordinals.append(item.ordinal)
        used_roles = set()
        for part in item.parts:
            if part.fragment_index >= len(field.fragments):
                raise ValueError("TEACHER_RENDER_EVIDENCE_INVALID:展示片段索引越界")
            if part.text not in field.fragments[part.fragment_index].quote:
                raise ValueError("TEACHER_RENDER_EVIDENCE_INVALID:展示文字不在原文引文中")
            if spec.kind == "EDUCATION":
                if part.role not in {"GRADUATION", "SCHOOL", "MAJOR"} or part.role in used_roles:
                    raise ValueError("TEACHER_RENDER_INVALID:学历字段重复或角色不适用")
            elif part.role in {"GRADUATION", "SCHOOL", "MAJOR"}:
                raise ValueError("TEACHER_RENDER_INVALID:学历字段不能用于论文或项目")
            used_roles.add(part.role)
    if spec.kind == "EDUCATION":
        if not stages.get("博士") and not stages.get("本科") and not stages.get("硕士"):
            raise ValueError("TEACHER_RENDER_INVALID:学历阶段为空")
        for stage in _STAGES:
            item = stages.get(stage)
            if not item:
                if stage == "硕士" and "博士" in stages:
                    lines.append("硕士：硕博连读")
                    audit.append({"generated": "硕士：硕博连读", "reason": "博士资料存在且硕士栏未填写"})
                continue
            parts = {part.role: part.text for part in item.parts}
            line = f"{stage}：{parts.get('GRADUATION', '')}".rstrip("：")
            lines.append("\n".join(filter(None, (line, parts.get("SCHOOL"), parts.get("MAJOR")))))
        return "\n\n".join(lines), audit

    if ordinals != sorted(ordinals):
        raise ValueError("TEACHER_RENDER_INVALID:事项必须按原顺序升序提交")
    for item in spec.items:
        parts = item.parts
        if column == "J" and any(part.role != "TEXT" for part in parts):
            raise ValueError("TEACHER_RENDER_INVALID:论文题目只能使用原文题目")
        if column == "K" and any(part.role not in {"TEXT", "ISSN", "JOURNAL_NAME"} for part in parts):
            raise ValueError("TEACHER_RENDER_INVALID:刊物列字段类型不匹配")
        if column in {"M", "N", "O", "P"} and any(part.role != "TEXT" for part in parts):
            raise ValueError("TEACHER_RENDER_INVALID:项目或成果只能使用原文内容")
        if column == "L" and any(part.role not in {"TEXT", "ISSN", "JOURNAL_NAME", "ZONE"} for part in parts):
            raise ValueError("TEACHER_RENDER_INVALID:收录列字段类型不匹配")
        content = ",".join(part.text for part in parts if part.role not in {"ISSN", "JOURNAL_NAME"} or column == "K")
        if column == "L":
            if any(_CCF_RE.search(part.text) for part in parts if part.role == "TEXT"):
                audit.append({"ordinal": item.ordinal, "school_grade_status": "CCF_SOURCE"})
                lines.append(f"{item.ordinal}.{_ccf_grade_first(content)}")
                continue
            zones = [part.text for part in parts if part.role == "ZONE"]
            if not zones and any(_ZONE_RE.search(part.text) for part in parts if part.role == "TEXT"):
                raise ValueError("TEACHER_RENDER_ZONE_REQUIRED:原文包含明确分区，须用 ZONE 标记并冻结目录版本")
            zone_grade = None
            if zones:
                if len(zones) != 1:
                    raise ValueError("TEACHER_RENDER_INVALID:同一论文有多个分区")
                match = _ZONE_RE.search(zones[0])
                if match:
                    zone_grade = {"1": "A++", "一": "A++", "2": "A+", "二": "A+",
                                  "3": "A", "三": "A", "4": "A", "四": "A"}[match.group(1)]
            issn = next((part.text for part in parts if part.role == "ISSN"), None)
            journal = next((part.text for part in parts if part.role == "JOURNAL_NAME"), None)
            catalog_grade = None
            catalog_conflict = False
            if zones or issn or journal:
                if spec.catalog_version_id is None:
                    raise ValueError("TEACHER_RENDER_CATALOG_REQUIRED:学校判级必须冻结共享目录版本")
                try:
                    if catalog_version is None:
                        catalog_version = catalog_info(db)["catalog_version_id"]
                except ReferenceCatalogError as exc:
                    raise ValueError(f"TEACHER_RENDER_CATALOG_UNAVAILABLE:{exc}") from exc
                if str(spec.catalog_version_id) != catalog_version:
                    raise ValueError("TEACHER_RENDER_CATALOG_CHANGED:共享目录版本已变化")
            if issn or journal:
                try:
                    match = query_catalog(db, issn=issn, journal_name=journal)
                except ReferenceCatalogError as exc:
                    raise ValueError(f"TEACHER_RENDER_CATALOG_UNAVAILABLE:{exc}") from exc
                if match["match_status"] == "FOUND":
                    row = match["matches"][0]
                    catalog_grade = row["school_grade"]
                    audit.append({"ordinal": item.ordinal, "catalog_version_id": match["catalog_version_id"],
                                  "sheet": row["sheet"], "row": row["row"], "category": row["category"]})
                elif match["match_status"] in {"CONFLICT", "AMBIGUOUS"}:
                    catalog_conflict = True
                    audit.append({"ordinal": item.ordinal, "school_grade_status": match["match_status"]})
            if catalog_conflict or (zone_grade and catalog_grade and zone_grade != catalog_grade):
                if not catalog_conflict:
                    audit.append({"ordinal": item.ordinal, "school_grade_status": "CONFLICT"})
                school_grade = None
            else:
                school_grade = zone_grade or catalog_grade
            if zone_grade:
                audit.append({"ordinal": item.ordinal, "catalog_version_id": catalog_version,
                              "rules_sheet": "Sheet1", "source_zone": zones[0],
                              "zone_grade": zone_grade})
            if school_grade:
                content = f"{school_grade}类,{content}" if content else f"{school_grade}类"
        lines.append(f"{item.ordinal}.{content}")
    return ("\n\n" if spec.kind == "PROJECT" else "\n").join(lines), audit
