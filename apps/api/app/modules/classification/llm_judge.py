"""受限 LLM 分类判定器。"""

from __future__ import annotations

from typing import Any


CLASSIFICATION_JUDGE_SYSTEM_PROMPT = """你是文件分类判定器。你只能基于候选分类判定文档类别。
不得创造新分类或自由分类路径；证据无法在原文定位的业务候选不得输出。
必须输出 JSON 对象，格式为 {"labels": [...]}。labels 最多 3 项。
每个 label 必须包含 category_id、confidence、reason、evidence。
evidence 中的 quote 必须来自原文。"""


class LLMClassificationJudge:
    """让 LLM 在候选分类集合内做受限多标签判定。"""

    def __init__(
        self,
        *,
        client: Any,
        allow_free_category_paths: bool = False,
        prompt_version: str = "classification-hybrid-v1",
    ) -> None:
        """保存模型客户端和判定策略。"""

        self.client = client
        self.allow_free_category_paths = allow_free_category_paths
        self.prompt_version = prompt_version

    def judge(
        self,
        *,
        filename: str,
        document_text: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """调用 LLM 判定候选分类，并校验输出不能越权。"""

        candidate_by_id = {
            str(candidate.get("category_id")): candidate
            for candidate in candidates
            if candidate.get("category_id")
        }
        parsed = self.client.complete_json(
            system_prompt=CLASSIFICATION_JUDGE_SYSTEM_PROMPT,
            user_payload={
                "filename": filename,
                "document_excerpt": document_text[:6000],
                "candidates": [_candidate_payload(candidate) for candidate in candidates[:8]],
                "allow_free_category_paths": self.allow_free_category_paths,
                "output_contract": {
                    "labels": [
                        {
                            "category_id": "候选 category_id；自由分类时可填写模型建议 id",
                            "category_path": ["自由分类时必填"],
                            "confidence": 0.0,
                            "reason": "判定理由",
                            "evidence": [{"quote": "必须来自原文", "signals": ["命中信号"]}],
                        }
                    ]
                },
            },
        )

        labels = parsed.get("labels") if isinstance(parsed, dict) else []
        if not isinstance(labels, list):
            return []

        accepted: list[dict[str, Any]] = []
        for label in labels[:3]:
            if not isinstance(label, dict):
                continue
            category_id = str(label.get("category_id") or "")
            if category_id not in candidate_by_id:
                # 保留旧配置字段的读取兼容，但新流程不能把模型自造路径写成
                # 分类复核建议；唯一 PRIMARY 选择器会把无可靠候选归入 OTHER。
                continue
            accepted_label = self._build_candidate_label(
                candidate=candidate_by_id[category_id],
                label=label,
                document_text=document_text,
            )
            if accepted_label is not None:
                accepted.append(accepted_label)
        return accepted

    def _build_candidate_label(
        self,
        *,
        candidate: dict[str, Any],
        label: dict[str, Any],
        document_text: str,
    ) -> dict[str, Any] | None:
        """把 LLM 对候选分类的选择转换为分类建议。"""

        evidence_items = _validated_evidence_items(label=label, document_text=document_text, source="hybrid")
        if not evidence_items:
            # 不生成无可定位证据的业务分类建议。调用方会回退到已验证的规则候选，
            # 或由唯一 PRIMARY 选择器生成 system.other，而不是创建分类复核队列。
            return None
        return {
            **candidate,
            "confidence": _clamp_confidence(label.get("confidence")),
            "status": "SUGGESTED",
            "source": "hybrid",
            "reason": str(label.get("reason") or ""),
            "prompt_version": self.prompt_version,
            "evidence_items": evidence_items,
            "evidence": _signals_from_evidence_items(evidence_items) or list(candidate.get("evidence") or []),
        }

def _candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    """构造传给 LLM 的候选分类信息，避免泄露无关运行状态。"""

    return {
        "category_id": candidate.get("category_id"),
        "name": candidate.get("name"),
        "category_path": candidate.get("category_path"),
        "confidence": candidate.get("confidence"),
        "matched_signals": candidate.get("evidence") or [],
        "candidate_reason": candidate.get("candidate_reason") or "",
    }


def _validated_evidence_items(*, label: dict[str, Any], document_text: str, source: str) -> list[dict[str, Any]]:
    """校验 LLM 返回的 quote 必须能在原文中定位。"""

    evidence = label.get("evidence") or []
    if not isinstance(evidence, list):
        return []
    items: list[dict[str, Any]] = []
    for item in evidence[:3]:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote or quote not in document_text:
            continue
        signals = [str(signal) for signal in item.get("signals", []) if str(signal).strip()]
        items.append(
            {
                "type": "text_quote",
                "page_number": None,
                "sheet_name": None,
                "quote": quote,
                "signals": signals,
                "source": source,
            }
        )
    return items


def _signals_from_evidence_items(evidence_items: list[dict[str, Any]]) -> list[str]:
    """从结构化证据中提取兼容旧 UI 的信号摘要。"""

    signals: list[str] = []
    for item in evidence_items:
        for signal in item.get("signals", []):
            value = str(signal)
            if value and value not in signals:
                signals.append(value)
    return signals


def _clamp_confidence(value: Any) -> float:
    """把模型置信度收敛到 0 到 1 之间。"""

    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, confidence)), 2)
