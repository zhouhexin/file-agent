"""结构化字段的确定性类型归一化。"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.modules.agent.tool_schemas import StructuredFieldSpec


BOOLEAN_VALUES = {
    "true": True,
    "yes": True,
    "是": True,
    "通过": True,
    "同意": True,
    "false": False,
    "no": False,
    "否": False,
    "不通过": False,
    "不同意": False,
}


def normalize_field_value(
    *,
    field: StructuredFieldSpec,
    raw_text: str | None,
    candidate_value: Any,
) -> tuple[Any, str, list[str]]:
    """按字段类型返回规范化值、状态和警告码，不修补不确定字符。"""

    raw = str(raw_text if raw_text is not None else candidate_value or "").strip()
    if not raw:
        return None, "MISSING", ["FIELD_MISSING"]
    try:
        value = _normalize_by_type(field=field, raw=raw, candidate_value=candidate_value)
    except (ValueError, InvalidOperation):
        return None, "NEEDS_REVIEW", ["NORMALIZATION_FAILED"]
    return value, "NORMALIZED", []


def _normalize_by_type(
    *,
    field: StructuredFieldSpec,
    raw: str,
    candidate_value: Any,
) -> Any:
    """实现单字段的严格类型转换。"""

    field_type = field.field_type
    if field.multiple:
        values = candidate_value if isinstance(candidate_value, list) else re.split(r"[、,，;；\n]", raw)
        return [
            _normalize_scalar(field=field.model_copy(update={"multiple": False}), raw=str(value).strip())
            for value in values
            if str(value).strip()
        ]
    return _normalize_scalar(field=field, raw=raw)


def _normalize_scalar(*, field: StructuredFieldSpec, raw: str) -> Any:
    """规范化一个非数组值。"""

    if field.field_type in {"string", "person_name", "organization"}:
        return raw
    if field.field_type == "integer":
        normalized = _numeric_text(raw)
        if not re.fullmatch(r"[-+]?\d+", normalized):
            raise ValueError("not an integer")
        return int(normalized)
    if field.field_type in {"decimal", "money"}:
        amount = Decimal(_numeric_text(raw))
        if field.field_type == "money":
            currency = "CNY" if any(marker in raw for marker in ("￥", "¥", "元", "人民币")) else None
            return {"amount": format(amount, "f"), "currency": currency}
        return format(amount, "f")
    if field.field_type in {"date", "datetime"}:
        return _normalize_datetime(raw, include_time=field.field_type == "datetime")
    if field.field_type == "phone":
        normalized = re.sub(r"[\s()（）-]", "", raw)
        if not re.fullmatch(r"\+?\d{6,20}", normalized):
            raise ValueError("not a phone number")
        return normalized
    if field.field_type == "id_number":
        normalized = re.sub(r"\s", "", raw).upper()
        if not re.fullmatch(r"[0-9A-Z-]{6,40}", normalized):
            raise ValueError("not an id number")
        return normalized
    if field.field_type == "boolean":
        normalized = raw.lower()
        if normalized not in BOOLEAN_VALUES:
            raise ValueError("not a boolean")
        return BOOLEAN_VALUES[normalized]
    if field.field_type == "enum":
        exact = next((value for value in field.enum_values if raw == value), None)
        if exact is None:
            raise ValueError("not an allowed enum value")
        return exact
    raise ValueError("unsupported field type")


def _numeric_text(value: str) -> str:
    """移除确定性的金额装饰符；模糊字符仍导致解析失败。

    手写报销、资助登记表常用 ``10000.-``、``3000-`` 或 ``1000.`` 表示整数金额。
    这些尾缀位于完整数字之后，不承载小数值，可以确定性移除；数字主体中的点、横线
    或其他字符仍然拒绝，避免把看不清的金额修补成业务事实。
    """

    normalized = value.strip()
    normalized = normalized.replace(",", "").replace("，", "")
    normalized = re.sub(r"(?:人民币|RMB|CNY|￥|¥|元)", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.strip()
    normalized = re.sub(r"(?<=\d)[.。．]?[-—–]\s*$", "", normalized)
    normalized = re.sub(r"(?<=\d)[.。．]\s*$", "", normalized)
    if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)", normalized):
        raise ValueError("ambiguous numeric value")
    return normalized


def _normalize_datetime(value: str, *, include_time: bool) -> str:
    """只解析具有完整年月日的常见确定性格式。"""

    normalized = value.strip()
    normalized = re.sub(r"年|/|\.", "-", normalized)
    normalized = normalized.replace("月", "-").replace("日", "")
    normalized = re.sub(r"\s+", " ", normalized)
    formats = (
        ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"]
        if include_time
        else []
    ) + ["%Y-%m-%d"]
    for format_string in formats:
        try:
            parsed = datetime.strptime(normalized, format_string)
        except ValueError:
            continue
        return parsed.isoformat(timespec="seconds") if include_time else parsed.date().isoformat()
    raise ValueError("ambiguous date")


def mask_sensitive_value(*, field_type: str, value: Any) -> Any:
    """普通用户回执默认遮蔽手机号和证件号中段。"""

    if value is None or field_type not in {"phone", "id_number"}:
        return value
    text = str(value)
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:3]}{'*' * (len(text) - 7)}{text[-4:]}"
