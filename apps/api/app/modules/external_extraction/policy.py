"""WorkBuddy 外部内容提取的来源与 Provider 安全策略。

WorkBuddy 图片不得回退到 File Agent 服务器 OCR。这里只集中维护入口与图片识别；
Provider 由 WorkBuddy 实际选择并在提交中记录，服务端不固定其名称。
"""

from __future__ import annotations

from pathlib import Path

from app.db.models import DocumentVersion, IngestBatch


WORKBUDDY_INGEST_CLIENT_IDS = frozenset({"workbuddy-attachment", "workbuddy-local"})
WORKBUDDY_IMAGE_SUFFIXES = frozenset({".bmp", ".jpeg", ".jpg", ".png", ".webp"})


def is_workbuddy_ingest(batch: IngestBatch | None) -> bool:
    """判断批次是否来自受控 WorkBuddy 附件或本地目录入口。"""

    return bool(batch is not None and batch.client_id in WORKBUDDY_INGEST_CLIENT_IDS)


def is_image_version(version: DocumentVersion | None) -> bool:
    """只依据受控版本元数据判断是否为图片，不读取图片正文。"""

    if version is None:
        return False
    suffix = Path(version.filename).suffix.lower()
    return suffix in WORKBUDDY_IMAGE_SUFFIXES or str(version.content_type or "").startswith(
        "image/"
    )


def requires_workbuddy_image_ocr(
    *,
    batch: IngestBatch | None,
    version: DocumentVersion | None,
) -> bool:
    """WorkBuddy 入口的图片必须由宿主 OCR，不能进入服务器解析器。"""

    return is_workbuddy_ingest(batch) and is_image_version(version)
