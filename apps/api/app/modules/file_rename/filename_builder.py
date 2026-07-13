"""根据受控策略构造安全文件名。"""

from __future__ import annotations

import re
from pathlib import Path

from app.modules.file_rename.schemas import FilenameMetadataResult, RenamePolicy


_FORBIDDEN_FILENAME_CHARACTERS = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


class FilenameBuildError(ValueError):
    """命名字段或策略无法生成安全文件名。"""


class FilenameBuilder:
    """只构造 basename，不接受或生成任意目录。"""

    def build(
        self,
        *,
        original_filename: str,
        metadata: FilenameMetadataResult,
        policy: RenamePolicy,
    ) -> tuple[str, str]:
        """返回目标文件名和使用的模板 key。"""

        if not metadata.can_build_filename:
            raise FilenameBuildError("年份或正文标题缺失，不能生成安全文件名。")
        template = next(
            (
                item
                for item in policy.templates
                if metadata.document_number.value and "document_number" in item.required_fields
            ),
            None,
        )
        if template is None:
            template = next(
                (
                    item
                    for item in policy.templates
                    if item.when == "document_number_missing" and "document_number" not in item.required_fields
                ),
                None,
            )
        if template is None:
            raise FilenameBuildError("没有匹配当前字段情况的重命名模板。")

        extension = Path(original_filename).suffix
        if policy.lowercase_extension:
            extension = extension.lower()
        values = {
            "year": _sanitize_component(metadata.year.value or ""),
            "document_number": _sanitize_component(metadata.document_number.value or ""),
            "title": _sanitize_title(metadata.title.value or "", policy.noise_terms),
            "extension": extension if policy.preserve_extension else "",
        }
        filename = template.template.format(**values)
        filename = _normalize_separators(filename=filename, separator=policy.separator, extension=extension)
        filename = _truncate_filename(filename, max_bytes=policy.max_filename_bytes)
        if not filename or filename in {".", ".."} or Path(filename).name != filename:
            raise FilenameBuildError("生成的文件名不符合安全约束。")
        return filename, template.key


def _sanitize_component(value: str) -> str:
    """清理单个模板字段中的非法字符。"""

    cleaned = _FORBIDDEN_FILENAME_CHARACTERS.sub(" ", value)
    return re.sub(r"\s+", " ", cleaned).strip(" ._-")


def _sanitize_title(value: str, noise_terms: list[str]) -> str:
    """清理标题噪声，但保留通知、报告等文种。"""

    cleaned = value
    for term in noise_terms:
        cleaned = cleaned.replace(term, "")
    return _sanitize_component(cleaned)


def _normalize_separators(*, filename: str, separator: str, extension: str) -> str:
    """合并连续分隔符，并保护扩展名前的点。"""

    stem = filename[: -len(extension)] if extension and filename.endswith(extension) else filename
    stem = re.sub(rf"{re.escape(separator)}+", separator, stem).strip(separator)
    return f"{stem}{extension}"


def _truncate_filename(filename: str, *, max_bytes: int) -> str:
    """按 UTF-8 字节限制截断 stem，避免破坏扩展名。"""

    if len(filename.encode("utf-8")) <= max_bytes:
        return filename
    suffix = Path(filename).suffix
    stem = filename[: -len(suffix)] if suffix else filename
    suffix_bytes = len(suffix.encode("utf-8"))
    available = max(1, max_bytes - suffix_bytes)
    encoded = stem.encode("utf-8")[:available]
    while encoded:
        try:
            truncated = encoded.decode("utf-8")
            return f"{truncated.rstrip(' ._-')}{suffix}"
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    raise FilenameBuildError("文件名长度限制过小。")

