"""CPU-only 中文检索分词器。

生产依赖使用 Jieba；依赖暂不可用时采用确定性字符词项降级，保证解析和索引任务不会因可选组件中断。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*|[\u3400-\u9fff]+")
_STOP_WORDS = {"的", "了", "和", "与", "及", "或", "在", "是", "为", "对", "等"}


class ChineseLexicalTokenizer:
    """使用独立 Jieba 实例生成 PostgreSQL ``simple`` FTS 可消费的词项。"""

    def __init__(self, business_terms: Iterable[str] = ()) -> None:
        """创建请求外可复用的只读词典，避免修改 Jieba 全局状态。"""

        self.name = "jieba"
        self.version = "fallback-deterministic-v1"
        self._jieba = None
        try:
            import jieba

            self._jieba = jieba.Tokenizer()
            self.version = str(getattr(jieba, "__version__", "unknown"))
            for term in sorted({str(item).strip() for item in business_terms if str(item).strip()}):
                if 1 < len(term) <= 40:
                    self._jieba.add_word(term, freq=100_000)
        except ImportError:
            # 部署依赖缺失时仍保留 CPU 词法可用性，但索引运行会记录实际 fallback 版本。
            self._jieba = None

    def tokenize(self, text: str) -> list[str]:
        """把正文转换为稳定小写词项；空白和常见停用词不会进入索引。"""

        normalized = str(text or "").strip()
        if not normalized:
            return []
        if self._jieba is not None:
            raw_tokens = self._jieba.cut(normalized, cut_all=False)
        else:
            raw_tokens = _fallback_tokens(normalized)
        tokens: list[str] = []
        for raw in raw_tokens:
            token = str(raw).strip().lower()
            if not token or token in _STOP_WORDS or not _TOKEN_PATTERN.fullmatch(token):
                continue
            tokens.append(token)
        return tokens

    def search_text(self, text: str) -> str:
        """生成以空格分隔的 FTS 输入，不把该派生文本写入日志或普通用户响应。"""

        return " ".join(self.tokenize(text))


def load_default_business_terms() -> set[str]:
    """从统一 taxonomy 提取短业务词，用于提高学校场景复合词的分词稳定性。"""

    taxonomy_path = (
        Path(__file__).resolve().parents[1]
        / "classification"
        / "taxonomies"
        / "unified_school_file_classification.json"
    )
    try:
        payload = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    terms: set[str] = set()

    def visit(node: object) -> None:
        """递归读取 taxonomy v2 节点，不把描述长句加入词典。"""

        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        for key in ("name", "aliases", "positive_signals"):
            value = node.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                term = str(item or "").strip()
                if 1 < len(term) <= 20 and not re.search(r"[。；，,\n]", term):
                    terms.add(term)
        visit(node.get("children") or [])

    visit(payload.get("categories") or [])
    return terms


def _fallback_tokens(text: str) -> list[str]:
    """在 Jieba 不可用时生成确定性中英文词项，并补充中文二元词。"""

    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text):
        value = match.group(0)
        if re.fullmatch(r"[\u3400-\u9fff]+", value):
            tokens.append(value)
            if len(value) > 1:
                tokens.extend(value[index : index + 2] for index in range(len(value) - 1))
        else:
            tokens.append(value)
    return tokens

