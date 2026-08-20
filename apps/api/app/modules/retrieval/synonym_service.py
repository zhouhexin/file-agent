"""文件检索受控同义短语与范围实体服务。

正式同义词只来自项目内版本化配置。服务不调用 LLM，也不把单个复合短语自动拆成 OR；
任何宽泛主题都只能作为待用户选择的候选范围。学校、学院等机构词是可验证的
文件范围条件，绝不能被静默降级为“当前工作区”的无条件检索。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


MAX_SYNONYM_PHRASES = 8
MAX_PHRASE_CHARS = 30
# “学校/本校/全校”是同一个业务范围的不同说法。它们必须用于匹配文件的
# 文件名、摘要、分类或正文证据；不能因为当前用户已在学校工作区就直接丢弃。
_SCOPE_ENTITY_VARIANTS = {
    "学校": ("学校", "本校", "全校"),
    "本校": ("学校", "本校", "全校"),
    "全校": ("学校", "本校", "全校"),
}
ENTITY_TOPIC_SUFFIXES = (
    "学校",
    "学院",
    "研究院",
    "实验室",
    "委员会",
    "办公室",
    "中心",
    "部门",
    "处",
    "科",
    "系",
)


@dataclass(frozen=True)
class SearchSynonymGroup:
    """一个经过结构校验的文件检索同义词组。"""

    group_id: str
    version: str
    canonical: str
    aliases: tuple[str, ...]
    broad_topics: tuple[str, ...]
    equivalent_in_text: bool = False

    @property
    def phrases(self) -> tuple[str, ...]:
        """返回去重后的完整短语，canonical 始终位于第一项。"""

        return tuple(dict.fromkeys((self.canonical, *self.aliases)))[:MAX_SYNONYM_PHRASES]


class FileSearchSynonymService:
    """读取并查询正式文件检索同义词组。"""

    def __init__(self, groups: tuple[SearchSynonymGroup, ...] | None = None) -> None:
        """允许测试注入固定词组，生产默认读取版本化 JSON。"""

        self.groups = groups if groups is not None else load_default_search_synonyms()
        self._phrase_index = {
            _normalize_phrase(phrase): group
            for group in self.groups
            for phrase in group.phrases
        }

    def find_group(self, phrase: str) -> SearchSynonymGroup | None:
        """按 canonical 或 alias 查找同义词组，不做自由语义猜测。"""

        return self._phrase_index.get(_normalize_phrase(phrase))

    def expand(self, phrase: str) -> tuple[str, ...]:
        """返回受控完整短语；没有词组时只返回原短语。"""

        normalized = _clean_public_phrase(phrase)
        if not normalized:
            return ()
        group = self.find_group(normalized)
        return group.phrases if group is not None else (normalized,)

    def expand_equivalent_mentions(self, phrase: str) -> tuple[str, ...]:
        """在完整查询短语中替换正式实体别名，不拆词、不扩大业务主题。

        例如“计算机学院的工作总结”会同时生成
        “计算机科学与工程学院的工作总结”。这类配置表示同一实体的正式简称，
        因此不需要再让用户选择语义范围。
        """

        cleaned = _clean_public_phrase(phrase)
        if not cleaned:
            return ()
        variants = [cleaned]
        for group in self.groups:
            if not group.equivalent_in_text:
                continue
            current_variants = list(variants)
            for value in current_variants:
                for source in group.phrases:
                    if source not in value:
                        continue
                    for target in group.phrases:
                        candidate = value.replace(source, target)
                        if candidate not in variants:
                            variants.append(candidate)
                            if len(variants) >= MAX_SYNONYM_PHRASES:
                                return tuple(variants)
        return tuple(variants)

    def find_equivalent_mention(
        self,
        phrase: str,
    ) -> tuple[SearchSynonymGroup, str] | None:
        """查找完整查询中出现的正式实体名称或简称。

        只返回显式标记为 ``equivalent_in_text`` 的词组，避免把普通业务同义词
        当作同一机构实体而静默扩大检索范围。
        """

        cleaned = _clean_public_phrase(phrase)
        if not cleaned:
            return None
        for group in self.groups:
            if not group.equivalent_in_text:
                continue
            for value in sorted(group.phrases, key=len, reverse=True):
                if value in cleaned:
                    return group, value
        return None


def split_entity_topic_phrase(phrase: str) -> tuple[str, str] | None:
    """把确定的“范围实体 + 文件主题”拆成两个必须同时满足的条件。

    该规则只接受具有明确学校机构后缀的左侧实体，避免把任意含“的”的自然语言
    都扩大成两个独立检索条件。无“的”的常见紧凑表达（如“学校工作总结”）也
    支持解析；范围实体与主题始终保留为独立条件，不能把范围实体默默当作工作区。
    """

    cleaned = _clean_public_phrase(phrase)
    if not cleaned:
        return None
    if cleaned.count("的") == 1:
        entity, topic = (value.strip() for value in cleaned.split("的", 1))
        if _is_scope_entity(entity) and len(topic) >= 2:
            return entity, topic
        return None

    # “学校工作总结”“计算机学院工作总结”等紧凑表达没有连接词。只接受受控
    # 范围词或明确机构后缀，避免把普通复合业务词错误拆开。
    for entity in sorted(_SCOPE_ENTITY_VARIANTS, key=len, reverse=True):
        if cleaned.startswith(entity) and len(cleaned[len(entity) :].strip()) >= 2:
            return entity, cleaned[len(entity) :].strip()
    suffix_pattern = "|".join(
        re.escape(value) for value in sorted(ENTITY_TOPIC_SUFFIXES, key=len, reverse=True)
    )
    match = re.match(rf"^(.+?(?:{suffix_pattern}))(.{{2,}})$", cleaned)
    if match and _is_scope_entity(match.group(1)):
        return match.group(1), match.group(2)
    return None


def expand_scope_entity_phrases(entity: str) -> tuple[str, ...]:
    """返回范围实体可用于证据匹配的受控等价说法。

    这不是自由同义扩展：目前仅覆盖“学校/本校/全校”这组产品定义的范围词；
    其他学院、部门等实体保持用户原词，后续可通过版本化实体词典扩展。
    """

    cleaned = _clean_public_phrase(entity)
    if not cleaned:
        return ()
    return _SCOPE_ENTITY_VARIANTS.get(cleaned, (cleaned,))


def _is_scope_entity(value: str) -> bool:
    """判断词语是否具有可验证的机构范围语义。"""

    return value in _SCOPE_ENTITY_VARIANTS or value.endswith(ENTITY_TOPIC_SUFFIXES)


@lru_cache(maxsize=1)
def load_default_search_synonyms() -> tuple[SearchSynonymGroup, ...]:
    """加载默认同义词配置并拒绝重复、空值和超长词项。"""

    path = (
        Path(__file__).resolve().parent
        / "synonyms"
        / "school_file_search_synonyms.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = str(payload.get("version") or "").strip()
    if not version:
        raise ValueError("文件检索同义词配置缺少 version")

    groups: list[SearchSynonymGroup] = []
    seen_ids: set[str] = set()
    seen_phrases: set[str] = set()
    for raw in payload.get("groups", []):
        group_id = str(raw.get("id") or "").strip()
        canonical = _clean_public_phrase(raw.get("canonical"))
        aliases = tuple(
            value
            for value in (
                _clean_public_phrase(item) for item in raw.get("aliases", [])
            )
            if value
        )
        broad_topics = tuple(
            value
            for value in (
                _clean_public_phrase(item) for item in raw.get("broad_topics", [])
            )
            if value
        )
        if not group_id or group_id in seen_ids:
            raise ValueError("文件检索同义词组 id 为空或重复")
        if not canonical:
            raise ValueError(f"同义词组 {group_id} 缺少 canonical")
        phrases = tuple(dict.fromkeys((canonical, *aliases)))
        if len(phrases) > MAX_SYNONYM_PHRASES:
            raise ValueError(f"同义词组 {group_id} 超过短语数量上限")
        normalized_phrases = {_normalize_phrase(item) for item in phrases}
        if seen_phrases.intersection(normalized_phrases):
            raise ValueError(f"同义词组 {group_id} 与其他组存在重复短语")
        seen_ids.add(group_id)
        seen_phrases.update(normalized_phrases)
        groups.append(
            SearchSynonymGroup(
                group_id=group_id,
                version=version,
                canonical=canonical,
                aliases=aliases,
                broad_topics=tuple(dict.fromkeys(broad_topics))[:4],
                equivalent_in_text=bool(raw.get("equivalent_in_text", False)),
            )
        )
    return tuple(groups)


def validate_custom_search_phrase(value: str) -> str:
    """校验用户自定义短语，禁止控制字符和超长自由查询进入 Tool。"""

    phrase = _clean_public_phrase(value)
    if len(phrase) < 2:
        raise ValueError("自定义查找词至少需要 2 个字符")
    return phrase


def _clean_public_phrase(value: object) -> str:
    """规范化可公开展示的短语，同时保留中文业务空格。"""

    phrase = " ".join(str(value or "").strip().split())
    if not phrase or len(phrase) > MAX_PHRASE_CHARS:
        return ""
    if re.search(r"[\x00-\x1f\x7f]", phrase):
        return ""
    return phrase


def _normalize_phrase(value: str) -> str:
    """生成同义词索引键，大小写和空白不影响查找。"""

    return re.sub(r"\s+", "", str(value or "").strip().lower())
