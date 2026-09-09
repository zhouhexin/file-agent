"""把 WorkBuddy 固定清单来源转换为可审计、不可写的整理规则上下文。

来源目录只能影响已显式选择的规则集，不能直接成为目标存储路径，也不能替代正文证据。
默认 ``content_based`` 规则因此返回空上下文；只有 ``legacy_school_materials`` 会把已经由
批次 Schema 和清单范围校验过的逻辑根、相对路径提供给既有职称/应聘材料包规则。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models import IngestBatch, IngestItem


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """一次导入条目的冻结来源及其规则适用结果。"""

    source_root_ref: str
    source_relative_path: str
    rule_profile: str
    source_context: str


class SourceProvenanceService:
    """从同批条目和冻结策略构造只读来源上下文。"""

    @staticmethod
    def resolve(*, batch: IngestBatch, item: IngestItem) -> SourceProvenance:
        """验证条目属于批次，并仅为显式旧材料规则开放来源信号。"""

        if item.batch_id != batch.id:
            raise RuntimeError("来源条目不属于当前导入批次")
        policy = dict(batch.policy_json or {})
        configured_root = str(policy.get("source_root_ref") or "")
        if not configured_root or configured_root != item.source_root_ref:
            raise RuntimeError("来源逻辑根与冻结批次策略不一致")
        rule_profile = str(policy.get("rule_profile") or "content_based")
        source_context = ""
        if rule_profile == "legacy_school_materials":
            # 使用 POSIX 逻辑文本供既有规则识别；它不是服务器路径，不能传给 StorageService。
            source_context = f"{item.source_root_ref}/{item.source_relative_path}"
        return SourceProvenance(
            source_root_ref=item.source_root_ref,
            source_relative_path=item.source_relative_path,
            rule_profile=rule_profile,
            source_context=source_context,
        )
