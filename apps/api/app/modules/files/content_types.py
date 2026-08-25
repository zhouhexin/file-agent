"""统一文件 MIME 推断与图片/PDF 内容识别边界。

文件名和客户端上报的 MIME 只能用于生成元数据候选；真正进入图片结构化抽取前，必须读取
受控文件内容确认格式，既兼容历史误标记录，也不能放行仅伪装扩展名的文件。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError


DEFAULT_BINARY_CONTENT_TYPE = "application/octet-stream"

SUPPORTED_STRUCTURED_IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/bmp",
        "image/tiff",
    }
)

SUPPORTED_STRUCTURED_CONTENT_TYPES = SUPPORTED_STRUCTURED_IMAGE_CONTENT_TYPES | {
    "application/pdf"
}

_CONTENT_TYPE_BY_SUFFIX = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".jfif": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".dib": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".gif": "image/gif",
}

_EXPECTED_CONTENT_TYPES_BY_SUFFIX = {
    suffix: {content_type} for suffix, content_type in _CONTENT_TYPE_BY_SUFFIX.items()
}
_EXPECTED_CONTENT_TYPES_BY_SUFFIX.update(
    {
        ".md": {"text/markdown", "text/plain"},
        ".csv": {"text/csv", "application/csv", "text/plain"},
        ".xlsx": {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        },
        ".xlsm": {
            "application/vnd.ms-excel.sheet.macroenabled.12",
            "application/vnd.ms-excel",
        },
    }
)

_CONTENT_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "image/x-ms-bmp": "image/bmp",
    "image/x-bmp": "image/bmp",
    "image/tif": "image/tiff",
}

_CONTENT_TYPE_BY_PIL_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "DIB": "image/bmp",
    "TIFF": "image/tiff",
    "GIF": "image/gif",
}


def normalize_content_type(content_type: str | None) -> str:
    """规范化 MIME 大小写、参数和常见历史别名。"""

    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_ALIASES.get(normalized, normalized)


def infer_content_type(*, filename: str, declared_content_type: str | None = None) -> str:
    """生成稳定 MIME 元数据；客户端给出明确值时保留，通用值按扩展名补全。

    该函数不证明文件内容安全，只用于消除不同操作系统和浏览器对同一扩展名的元数据差异。
    需要执行解析或结构化抽取时，调用方仍必须做内容级校验。
    """

    suffix = Path(filename).suffix.lower()
    inferred = _CONTENT_TYPE_BY_SUFFIX.get(suffix, DEFAULT_BINARY_CONTENT_TYPE)
    declared = normalize_content_type(declared_content_type)
    if not declared or declared == DEFAULT_BINARY_CONTENT_TYPE:
        return inferred
    # 同一格式的浏览器兼容 MIME 统一收敛为规范值；不匹配值继续保留，供风险检查拒绝或告警。
    if declared in _EXPECTED_CONTENT_TYPES_BY_SUFFIX.get(suffix, set()):
        return inferred
    return declared


def expected_content_types_for_filename(filename: str) -> set[str]:
    """返回文件扩展名对应的规范 MIME 集合，供基础风险检查复用。"""

    return set(_EXPECTED_CONTENT_TYPES_BY_SUFFIX.get(Path(filename).suffix.lower(), set()))


def detect_image_content_type(file_path: Path) -> str | None:
    """用 Pillow 校验真实图片容器并返回规范 MIME，不信任扩展名。"""

    try:
        with Image.open(file_path) as image:
            image_format = str(image.format or "").upper()
            image.verify()
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
    return _CONTENT_TYPE_BY_PIL_FORMAT.get(image_format)


def detect_structured_source_content_type(file_path: Path) -> str | None:
    """根据受控文件内容识别结构化抽取来源类型。

    PDF 只在头部存在规范签名时作为候选，随后仍由 PyMuPDF 完整打开校验；图片必须由 Pillow
    成功识别并校验容器。这里故意不信任数据库 MIME 或扩展名，避免历史误标和格式伪装。
    """

    try:
        with file_path.open("rb") as handle:
            header = handle.read(1024)
    except OSError:
        return None
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    return detect_image_content_type(file_path)
