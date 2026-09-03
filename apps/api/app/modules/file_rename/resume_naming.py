"""个人简历专用文件名识别与构造。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CHINESE_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平"
    "黄和穆萧尹姚邵汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董"
    "梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林钟徐邱骆高夏蔡田樊胡凌"
    "霍虞万支柯管卢莫经房裘缪干解应宗丁宣邓郁单杭洪包诸左石崔吉龚程"
    "邢裴陆荣翁荀羊惠甄曲封芮储靳段巫乌焦巴弓牧隗山谷车侯宓蓬全班仰"
    "秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白"
    "怀蒲台从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶"
    "堵冉宰郦雍郤璩桑桂濮牛寿通边扈燕冀浦尚农温庄晏柴瞿阎充慕连茹习"
    "艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧利师巩"
    "聂晁勾敖冷辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯"
    "盖益桓公"
)
_RESUME_TERMS = ("个人简历", "个人履历", "履历表", "简历", "curriculum vitae")
_GENERIC_PATH_TERMS = (
    "应聘",
    "招聘",
    "简历",
    "履历",
    "材料",
    "附件",
    "照片",
    "证书",
    "论文",
    "其他",
    "个人",
)


@dataclass(frozen=True, slots=True)
class ResumeFilenameSuggestion:
    """姓名可信时生成的个人简历标准名。"""

    filename: str
    person_name: str
    evidence_quote: str
    evidence_source: str


def suggest_resume_filename(
    *,
    original_filename: str,
    pages: list[Any],
    source_relative_path: str = "",
) -> ResumeFilenameSuggestion | None:
    """仅对明确简历且姓名唯一的文件生成 ``姓名_个人简历``。"""

    text = "\n".join(_page_text(page) for page in pages if _page_text(page))[:100_000]
    if not _looks_like_resume(filename=original_filename, text=text):
        return None
    labeled_names = _labeled_name_candidates(text)
    if len(labeled_names) > 1:
        return None
    resolved = (
        labeled_names[0]
        if labeled_names
        else _name_from_source_path(source_relative_path)
        or _name_from_filename(original_filename)
    )
    if resolved is None:
        return None
    person_name, evidence_quote, evidence_source = resolved
    extension = Path(original_filename).suffix.lower()
    return ResumeFilenameSuggestion(
        filename=f"{person_name}_个人简历{extension}",
        person_name=person_name,
        evidence_quote=evidence_quote,
        evidence_source=evidence_source,
    )


def _looks_like_resume(*, filename: str, text: str) -> bool:
    """显式简历词或稳定履历结构命中时才启用专用规则。"""

    filename_stem = Path(filename).stem
    filename_compact = re.sub(r"\s+", "", filename_stem).lower()
    if any(term.replace(" ", "") in filename_compact for term in _RESUME_TERMS):
        return True
    if re.search(r"(?i)(?:^|[^a-z])c\.?v\.?(?:[^a-z]|$)", filename_stem):
        return True
    if re.search(
        r"(?im)^\s*(?:个\s*人\s*(?:简\s*历|履\s*历)|履\s*历\s*表|简\s*历|curriculum\s+vitae|c\.?v\.?)\s*$",
        text,
    ):
        return True
    personal = bool(re.search(r"姓\s*名|出生年月|性\s*别|联系方式|联系电话|电子邮箱", text))
    groups = sum(
        bool(re.search(pattern, text, flags=re.I))
        for pattern in (
            r"教育经历|学习经历|教育背景|毕业院校|学历|学位",
            r"工作经历|任职经历|工作履历|现任职级|任教学校",
            r"科研经历|研究方向|代表著作|发表论文|学术成果",
            r"联系电话|联系方式|电子邮箱|\bemail\b|\bphone\b",
        )
    )
    return personal and groups >= 2


def _name_from_labeled_text(text: str) -> tuple[str, str, str] | None:
    """优先读取正文中明确的姓名字段。"""

    candidates = _labeled_name_candidates(text)
    return candidates[0] if len(candidates) == 1 else None


def _labeled_name_candidates(text: str) -> list[tuple[str, str, str]]:
    """读取并去重姓名字段；多个不同姓名时交由上层拒绝自动命名。"""

    candidates: dict[str, tuple[str, str, str]] = {}
    for match in re.finditer(
        r"姓\s*名\s*[:：]?\s*([\u3400-\u9fff·]{2,6}?)(?=\s|出生|性别|民族|籍贯|学历|学位|$)",
        text,
        flags=re.M,
    ):
        name = _chinese_name(match.group(1), require_common_surname=False)
        if name:
            candidates.setdefault(
                name,
                (name, match.group(0).strip(), "resume_body_name"),
            )
    for match in re.finditer(
        r"(?im)^\s*(?:full\s+name|name)\s*[:：]\s*"
        r"([a-z][a-z'’-]*(?:\s+[a-z][a-z'’-]*){1,4})\s*$",
        text,
    ):
        name = match.group(1).strip()
        candidates.setdefault(name.casefold(), (name, match.group(0).strip(), "resume_body_name"))
    return list(candidates.values())


def _name_from_source_path(relative_path: str) -> tuple[str, str, str] | None:
    """正文未标姓名时，从应聘材料包的人员目录反向提取。"""

    normalized = str(relative_path or "").replace("\\", "/")
    for part in reversed(normalized.rsplit("/", 1)[0].split("/")):
        if not part or any(term in part for term in _GENERIC_PATH_TERMS):
            continue
        name = _chinese_name(part, require_common_surname=True)
        if name:
            return name, part, "managed_source_container"
    return None


def _name_from_filename(filename: str) -> tuple[str, str, str] | None:
    """最后从简历原文件名中的明确人名片段提取。"""

    stem = Path(filename).stem.strip()
    labeled = _name_from_labeled_text(stem)
    if labeled is not None:
        return labeled[0], stem, "resume_filename"
    cleaned = re.sub(r"[\[（(]\s*\d+\s*[\]）)]", "", stem)
    cleaned = re.sub(r"(?:个人)?(?:简历|履历)(?:表)?", "-", cleaned, flags=re.I)
    cleaned = re.sub(r"(?i)curriculum\s+vitae|(?:^|[^a-z])c\.?v\.?(?:[^a-z]|$)", "-", cleaned)
    cleaned = re.sub(r"^\s*\d{4}[_\-\s]*", "", cleaned)
    candidates = [part.strip(" _-.的") for part in re.split(r"[-_—]+", cleaned)]
    for candidate in candidates:
        candidate = re.sub(r"\d+$", "", candidate).strip()
        name = _chinese_name(candidate, require_common_surname=True)
        if name:
            return name, stem, "resume_filename"
    english = re.fullmatch(
        r"[A-Za-z][A-Za-z'’-]*(?:\s+[A-Za-z][A-Za-z'’-]*){1,4}",
        cleaned.strip(" _-."),
    )
    if english:
        return english.group(0), stem, "resume_filename"
    return None


def _chinese_name(value: str, *, require_common_surname: bool) -> str | None:
    """校验短中文姓名，目录和文件名候选必须具有常见姓氏。"""

    normalized = re.sub(r"\s+", "", str(value or "")).strip("·")
    if not re.fullmatch(r"[\u3400-\u9fff]{2,4}", normalized):
        return None
    if require_common_surname and normalized[0] not in _CHINESE_SURNAMES:
        return None
    return normalized


def _page_text(page: Any) -> str:
    """兼容 ORM 页面和测试字典。"""

    if isinstance(page, dict):
        return str(page.get("text_content") or page.get("text") or "")
    return str(getattr(page, "text_content", "") or "")
