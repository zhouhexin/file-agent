"""Agent 能力路由器。

本模块把 LLM 或确定性 Planner 给出的 intent、required_capabilities 和
tool_plan_hint 标准化为受控 Tool 路由。它只负责选择能力入口，不执行 Tool。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.modules.agent.capabilities.service import load_agent_capabilities


SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".tsv"}
INTENT_ALIASES = {
    "SPREADSHEET_ANALYSIS": "ANALYZE_SPREADSHEET",
}


class CapabilityRoute(BaseModel):
    """Planner 能力路由结果。"""

    intent: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    selected_skill: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    target_scope: str = "unspecified"
    matched_file_types: list[str] = Field(default_factory=list)


def route_user_intent(
    *,
    intent: str,
    required_capabilities: list[str],
    tool_plan_hint: list[str],
    target_scope: str = "unspecified",
    attachments: list[dict] | None = None,
) -> CapabilityRoute | None:
    """根据标准 intent 和能力 hint 返回受控 Tool 路由。"""

    normalized_intent = _normalize_intent_alias(intent)
    file_types = _attachment_file_types(attachments or [])
    requested_keys = {normalized_intent, intent, *required_capabilities, *tool_plan_hint}
    if not requested_keys:
        return None

    catalog = load_agent_capabilities(detail_level="full")
    for capability in catalog.get("capabilities", []):
        capability_keys = {
            str(item)
            for item in [
                capability.get("id"),
                *capability.get("intents", []),
                *capability.get("capability_keys", []),
                *capability.get("tool_names", []),
            ]
            if item
        }
        if not requested_keys.intersection(capability_keys):
            continue
        tool_names = [str(item) for item in capability.get("tool_names", []) if item]
        if not tool_names:
            continue
        return CapabilityRoute(
            intent=_normalized_intent(intent=normalized_intent, capability=capability),
            tool_name=_select_tool_name(tool_names=tool_names, requested_keys=requested_keys),
            selected_skill=str(capability.get("id") or "llm-understanding"),
            capability_id=str(capability.get("id") or "unknown"),
            target_scope=target_scope,
            matched_file_types=sorted(file_types),
        )
    return None


def _normalize_intent_alias(intent: str) -> str:
    """把 LLM 可能输出的同义 intent 归一为应用内部枚举。"""

    return INTENT_ALIASES.get(intent, intent)


def _normalized_intent(*, intent: str, capability: dict) -> str:
    """优先保留传入 intent；缺失时使用能力目录中的第一个标准 intent。"""

    if intent:
        return intent
    intents = [str(item) for item in capability.get("intents", []) if item]
    return intents[0] if intents else "UNKNOWN"


def _select_tool_name(*, tool_names: list[str], requested_keys: set[str]) -> str:
    """优先选择 LLM hint 中明确给出的 Tool，否则使用能力默认 Tool。"""

    for tool_name in tool_names:
        if tool_name in requested_keys:
            return tool_name
    return tool_names[0]


def _attachment_file_types(attachments: list[dict]) -> set[str]:
    """从附件文件名中提取后缀，供能力路由判断文件类型。"""

    file_types: set[str] = set()
    for attachment in attachments:
        filename = str(attachment.get("filename") or "")
        suffix = Path(filename).suffix.lower()
        if suffix:
            file_types.add(suffix)
    return file_types
