"""生成分类内容层与 PRIMARY 选择层的稳定指纹。

内容指纹不包含工作副本当前路径或系统生成文件名，因此纯移动不会触发 OCR/解析；原始导入名称作为
独立不可变特征参与，避免系统重命名结果反过来强化下一轮分类。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ClassificationContentFingerprintInput(BaseModel):
    """所有会改变自动候选事实的版本化输入。"""

    model_config = ConfigDict(extra="forbid")

    document_version_id: str
    content_sha256: str
    extracted_content_digest: str
    structure_digest: str = ""
    parser_version: str
    taxonomy_key: str
    taxonomy_version: str
    taxonomy_content_digest: str
    rule_policy_id: str
    rule_policy_version: str
    summary_config_version: str
    semantic_model_version: str = "disabled"
    graph_policy_version: str = "disabled"
    ingest_original_filename: str = ""
    original_naming_features: dict[str, Any] = Field(default_factory=dict)


class PrimarySelectionFingerprintInput(BaseModel):
    """在内容指纹之上冻结用途、人工主类和本轮明确目标。"""

    model_config = ConfigDict(extra="forbid")

    content_fingerprint: str
    purpose_package_digest: str = ""
    existing_human_primary_id: str = ""
    existing_human_primary_revision: int | None = Field(default=None, ge=1)
    explicit_target_category_id: str = ""
    explicit_target_revision: int | None = Field(default=None, ge=1)


def build_content_fingerprint(value: ClassificationContentFingerprintInput) -> str:
    """对规范 JSON 计算完整 SHA-256，任一事实或策略变化都会失效。"""

    return _digest(value.model_dump(mode="json"))


def build_primary_selection_fingerprint(
    value: PrimarySelectionFingerprintInput,
) -> str:
    """生成包含授权上下文的 PRIMARY 决策指纹。"""

    return _digest(value.model_dump(mode="json"))


def digest_text(value: str) -> str:
    """计算解析正文或结构快照摘要，不把原文写入状态。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(payload: dict[str, Any]) -> str:
    """使用稳定键序和 UTF-8 编码生成跨进程一致摘要。"""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
