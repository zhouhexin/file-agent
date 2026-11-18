"""识别用户逐份列出的正式文档身份；仅提取范围，匹配必须经过后端索引。"""

import re
import unicodedata
from dataclasses import dataclass


def _compact(value: str) -> str:
    """统一排版差异，不省略题名、年份或文号中的身份信息。"""
    value = unicodedata.normalize("NFKC", value)
    return (
        re.sub(r"\s+", "", value)
        .casefold()
        .translate(str.maketrans("〔〕【】", "[][]"))
    )


@dataclass(frozen=True)
class ExplicitSource:
    """用户明确列出的来源候选；必须匹配真实索引才能成为读取范围。"""

    title: str
    issuer: str
    year: str
    number: str

    def matches(self, filenames: list[str]) -> bool:
        """要求同一真实名称同时包含完整题名与正式文号，不以主题相似替代。"""
        identity = _compact(f"{self.issuer}〔{self.year}〕{self.number}号")
        # 标题与正式文号必须同时有真实文件名依据，不能只凭一个编号认定对象。
        return any(
            identity in _compact(name) and _compact(self.title) in _compact(name)
            for name in filenames
        )


def extract_explicit_sources(message: str) -> list[ExplicitSource]:
    """窄范围支持：根据《题名》+文号逐份列举，再提出只读内容要求。"""
    text = unicodedata.normalize("NFKC", str(message))
    prefix = re.match(
        r"^\s*(?:以下仅核对文件记载的当时信息。\s*)?(?:请\s*)?(?:只|仅)?(?:根据|依据)",
        text,
    )
    if not prefix:
        return []
    spans, depth, start = [], 0, None
    mask = list(text)
    for pos, char in enumerate(text):
        if char == "《":
            if depth == 0:
                start = pos
            depth += 1
        if depth:
            mask[pos] = " "
        if char == "》" and depth:
            depth -= 1
            if depth == 0:
                spans.append((start, pos + 1))
    if depth:
        return []
    # 屏蔽书名号内的题名，防止文件标题中的“说明”等词被当作用户操作。
    action = re.search(
        r"(?:分别)?(?:说明|列出|回答|解释|比较|对照|总结|概括|核对|告诉我)",
        "".join(mask)[prefix.end():],
    )
    if not action:
        return []
    end = prefix.end() + action.start()
    if re.search(r"删除|回收站|重命名|改名|覆盖|移动|复制|导出|外发|发送给", text[end:]):
        return []
    spans = [(a, b) for a, b in spans if a >= prefix.end() and b <= end]
    if not 2 <= len(spans) <= 8:
        return []
    results = []
    pattern = re.compile(
        r"(?P<issuer>[\u4e00-\u9fffA-Za-z]{2,30})\s*[〔\[【]"
        r"(?P<year>(?:19|20)\d{2})[〕\]】]\s*(?P<number>\d{1,4})\s*号"
    )
    for index, (a, b) in enumerate(spans):
        title = text[a + 1:b - 1].strip()
        tail = text[b:spans[index + 1][0] if index + 1 < len(spans) else end]
        numbers = list(pattern.finditer(tail))
        if len(title) < 4 or len(numbers) != 1:
            return []
        match = numbers[0]
        results.append(
            ExplicitSource(title, match["issuer"], match["year"], match["number"])
        )
    return results
