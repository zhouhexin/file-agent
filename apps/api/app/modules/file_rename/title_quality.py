"""文件重命名标题候选的共享质量规则。"""

from __future__ import annotations

import re


def looks_like_body_sentence(value: str) -> bool:
    """识别正文段落、条款组合或课程要求被误选为标题的情况。"""

    text = value.strip()
    if not text:
        return False
    sentence_marks = len(re.findall(r"[。；！？]", text))
    section_marks = len(
        re.findall(r"(?:^|[\s。；])[一二三四五六七八九十百\d]+[、.]", text)
    )
    numeric_conditions = len(
        re.findall(r"(?:≥|≤|>=|<=|>|<|学分|年限|百分比|%)", text, flags=re.I)
    )
    return (
        sentence_marks >= 2
        or section_marks >= 1
        or (sentence_marks >= 1 and numeric_conditions >= 2)
    )
