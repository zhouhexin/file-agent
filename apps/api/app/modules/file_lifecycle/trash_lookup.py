"""按完整文件名查找当前用户可恢复的回收站文件。

普通文件检索必须继续排除回收站。本模块只在用户消息明确包含完整文件名时提供
回收站恢复候选，并逐条保留同名、同版本或同哈希记录，最终选择只能来自用户。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import DocumentVersion, TrashEntry, WorkingCopy
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


_SUPPORTED_FILENAME_SUFFIXES = {
    ".csv",
    ".doc",
    ".docx",
    ".md",
    ".pdf",
    ".txt",
    ".xls",
    ".xlsx",
}
_DISPLAY_WRAPPERS = re.compile(r"[\s\"'“”‘’《》【】]+")


class ExactTrashFilenameLookupService:
    """把完整文件名查询安全投影为回收站单选候选。"""

    def __init__(self, *, db: Session, user_id: str, workspace_id: str | None = None) -> None:
        """保存请求级数据库会话和用户边界，禁止跨请求复用。"""

        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id or get_shared_workspace_id(db)

    def lookup(self, *, query: str) -> dict[str, Any] | None:
        """仅当消息包含完整文件名且没有同名活动副本时返回候选。

        工作副本 ID、DocumentVersion ID 和内容哈希只用于后端事实校验，绝不能
        用于合并候选或替用户选择。即使多条记录完全一致，也必须逐条返回。
        """

        normalized_query = _normalize_lookup_text(query)
        if not normalized_query:
            return None
        rows = (
            self.db.query(TrashEntry, WorkingCopy, DocumentVersion)
            .join(WorkingCopy, WorkingCopy.id == TrashEntry.working_copy_id)
            .join(DocumentVersion, DocumentVersion.id == TrashEntry.document_version_id)
            .filter(
                TrashEntry.workspace_id == self.workspace_id,
                TrashEntry.status == "ACTIVE",
                WorkingCopy.status == "TRASHED",
            )
            .order_by(TrashEntry.deleted_at.desc(), TrashEntry.id.asc())
            .all()
        )
        matched = [
            (entry, working_copy, version)
            for entry, working_copy, version in rows
            if _is_explicit_full_filename_match(
                normalized_query=normalized_query,
                filename=working_copy.filename,
            )
        ]
        if not matched:
            return None

        matched_names = {
            _normalize_filename_identity(working_copy.filename)
            for _, working_copy, _ in matched
        }
        active_names = {
            _normalize_filename_identity(filename)
            for (filename,) in (
                self.db.query(WorkingCopy.filename)
                .filter(
                    WorkingCopy.workspace_id == self.workspace_id,
                    WorkingCopy.status == "ACTIVE",
                    func.lower(WorkingCopy.filename).in_(list(matched_names)),
                )
                .all()
            )
        }
        matched = [
            item
            for item in matched
            if _normalize_filename_identity(item[1].filename) not in active_names
        ]
        if not matched:
            # 同名活动副本存在时沿用普通检索结果，不把历史删除项混入结果卡。
            return None

        candidates = [
            {
                "trash_entry_id": entry.id,
                "filename": working_copy.filename,
                "size_bytes": working_copy.size_bytes,
                "version_number": version.version_number,
                "deleted_at": entry.deleted_at.isoformat(),
                "created_at": working_copy.created_at.isoformat(),
            }
            for entry, working_copy, version in matched
        ]
        filename = candidates[0]["filename"]
        return {
            "query_type": "EXACT_FILENAME",
            "requires_selection": True,
            "message": f"找到了已删除的文件“{filename}”。请选择是否恢复。",
            "candidates": candidates,
        }


def _normalize_lookup_text(value: str) -> str:
    """统一 Unicode、大小写和展示引号，但保留文件名中的业务符号。"""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _DISPLAY_WRAPPERS.sub("", normalized)


def _normalize_filename_identity(value: str) -> str:
    """归一化文件名身份但保留内部空格，避免把不同合法文件名误判为同名。"""

    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _is_explicit_full_filename_match(*, normalized_query: str, filename: str) -> bool:
    """要求消息包含带受支持扩展名的完整文件名，避免主题词误查回收站。"""

    normalized_filename = _normalize_lookup_text(filename)
    suffix = next(
        (value for value in _SUPPORTED_FILENAME_SUFFIXES if normalized_filename.endswith(value)),
        None,
    )
    return bool(suffix and normalized_filename in normalized_query)
