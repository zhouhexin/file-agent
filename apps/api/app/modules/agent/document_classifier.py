"""对话阶段文件正文的轻量确定性分类器。"""

from __future__ import annotations

from typing import Any

from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.matcher import match_document_text


def classify_document_text(text: str) -> list[dict[str, Any]]:
    """基于预置分类体系返回可审计的基础分类建议。"""

    taxonomy = load_default_taxonomy()
    return match_document_text(text=text, taxonomy=taxonomy)
