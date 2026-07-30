"""ToolResultBinding 的安全解析器。

解析器只读取当前 AgentRun 已完成步骤的结构化输出，不执行模板语言、Python 表达式、JSONPath、Shell
或数据库查询。绑定完成后仍必须由 ToolRegistry 再次执行输入 schema 校验。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.modules.agent.tool_contracts import ToolResultBinding


TRUSTED_INPUT_ROOTS = {
    "user_id",
    "conversation_id",
    "agent_run_id",
    "workspace_id",
    "confirmation_text",
    "operation_plan_id",
    "local_path",
    "file_path",
}
MAX_BOUND_ARRAY_ITEMS = 100


class ToolBindingError(ValueError):
    """Tool 结果绑定不存在、越权或类型校验失败。"""


class ToolResultBindingResolver:
    """把已完成步骤输出绑定到目标 Tool 的字面量输入。"""

    def resolve(
        self,
        *,
        literal_input: dict[str, Any],
        bindings: list[dict[str, Any] | ToolResultBinding],
        step_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """解析全部绑定；任何一项失败都拒绝目标 Tool 调用。"""

        resolved = deepcopy(literal_input)
        for raw_binding in bindings:
            binding = (
                raw_binding
                if isinstance(raw_binding, ToolResultBinding)
                else ToolResultBinding.model_validate(raw_binding)
            )
            target_root = binding.target_field.split(".", 1)[0]
            if target_root in TRUSTED_INPUT_ROOTS:
                raise ToolBindingError(
                    f"绑定不能覆盖受信任运行字段: {binding.target_field}"
                )
            source = step_results.get(binding.source_step_id)
            if source is None:
                raise ToolBindingError(
                    f"绑定来源步骤不存在或尚未完成: {binding.source_step_id}"
                )
            if str(source.get("status") or "").upper() not in {
                "COMPLETED",
                "PARTIAL",
            }:
                raise ToolBindingError(
                    f"绑定来源步骤未成功: {binding.source_step_id}"
                )
            value = _read_field(source.get("output", {}), binding.source_field)
            if isinstance(value, list) and len(value) > MAX_BOUND_ARRAY_ITEMS:
                raise ToolBindingError(
                    f"绑定数组超过 {MAX_BOUND_ARRAY_ITEMS} 项上限: {binding.source_field}"
                )
            _write_field(resolved, binding.target_field, value)
        return resolved


def _read_field(payload: Any, field_path: str) -> Any:
    """从字典结果读取点分字段；不支持数组索引或通配符。"""

    current = payload
    for part in field_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ToolBindingError(f"绑定来源字段不存在: {field_path}")
        current = current[part]
    return deepcopy(current)


def _write_field(payload: dict[str, Any], field_path: str, value: Any) -> None:
    """向目标输入写入点分字段，禁止覆盖已存在的字面量值。"""

    parts = field_path.split(".")
    current = payload
    for part in parts[:-1]:
        existing = current.get(part)
        if existing is None:
            current[part] = {}
        elif not isinstance(existing, dict):
            raise ToolBindingError(f"绑定目标父字段不是对象: {field_path}")
        current = current[part]
    leaf = parts[-1]
    if leaf in current:
        raise ToolBindingError(f"绑定不能覆盖字面量输入: {field_path}")
    current[leaf] = deepcopy(value)
