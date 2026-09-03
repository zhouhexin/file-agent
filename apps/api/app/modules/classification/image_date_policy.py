"""上传图片按学院根目录和本地上传日期组织的确定性规则。

该规则只表达用户明确指定的目录投影，不尝试从图片内容推断具体学院或业务主题；
动态日期节点是分类树的虚拟组织节点，不会被写回版本化 taxonomy。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


IMAGE_DATE_CATEGORY_ROOT_ID = "college"
IMAGE_DATE_CATEGORY_ROOT_NAME = "学院"
IMAGE_DATE_RELATION_SOURCE = "image_upload_date_policy"
IMAGE_DATE_CLASSIFIER_VERSION = "image-upload-date-v1"
MANAGED_SOURCE_MODIFIED_DATE_RELATION_SOURCE = "managed_source_modified_date_policy"
MANAGED_SOURCE_MODIFIED_DATE_CLASSIFIER_VERSION = "managed-source-modified-date-v1"
IMAGE_DATE_RELATION_SOURCES = frozenset(
    {
        IMAGE_DATE_RELATION_SOURCE,
        MANAGED_SOURCE_MODIFIED_DATE_RELATION_SOURCE,
    }
)
IMAGE_DATE_VIRTUAL_NODE_PREFIX = "__image_upload_date__:"
IMAGE_DATE_TIMEZONE = ZoneInfo("Asia/Shanghai")

_DATE_LABEL_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_date_label(value: str) -> bool:
    """校验规范日历日期，拒绝仅格式相似但不存在的月份或日期。"""

    if not _DATE_LABEL_PATTERN.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def image_upload_date_label(uploaded_at: datetime) -> str:
    """把上传时间转换为中国本地自然日，避免 UTC 跨日造成目录错误。"""

    normalized = uploaded_at
    if normalized.tzinfo is None:
        # 数据库存量中的无时区时间按项目统一的 UTC 持久化约定解释。
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(IMAGE_DATE_TIMEZONE).date().isoformat()


def image_date_category_path(date_label: str) -> list[str]:
    """生成稳定的“学院/日期”显示与工作副本目录路径。"""

    if not _is_date_label(str(date_label or "")):
        raise ValueError("图片上传日期必须使用 YYYY-MM-DD 格式")
    return [IMAGE_DATE_CATEGORY_ROOT_NAME, date_label]


def image_date_virtual_node_id(date_label: str) -> str:
    """为动态日期目录生成不会与 taxonomy 稳定 ID 冲突的查询标识。"""

    image_date_category_path(date_label)
    return f"{IMAGE_DATE_VIRTUAL_NODE_PREFIX}{date_label}"


def parse_image_date_virtual_node_id(category_id: str | None) -> str | None:
    """解析后端签发的图片日期虚拟节点 ID，拒绝任意路径片段。"""

    value = str(category_id or "")
    if not value.startswith(IMAGE_DATE_VIRTUAL_NODE_PREFIX):
        return None
    date_label = value.removeprefix(IMAGE_DATE_VIRTUAL_NODE_PREFIX)
    return date_label if _is_date_label(date_label) else None


def image_date_from_category_path(category_path: list[object] | None) -> str | None:
    """从正式关系投影中读取受控日期，用于构造分类树虚拟节点。"""

    normalized = [str(item) for item in list(category_path or [])]
    if (
        len(normalized) == 2
        and normalized[0] == IMAGE_DATE_CATEGORY_ROOT_NAME
        and _is_date_label(normalized[1])
    ):
        return normalized[1]
    return None
