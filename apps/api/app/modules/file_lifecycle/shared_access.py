"""共享工作副本的规范身份解析与统一访问策略。

上传暂存 Document、共享工作副本 Document 和不可变原件属于不同身份。本模块集中完成
三者映射，并把“共享活动文件可读、私有会话与上传来源仍隔离”的规则收敛到一个边界。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Query, Session

from app.db.models import (
    DocumentCategorySuggestion,
    DocumentVersion,
    UploadArchiveRecord,
    WorkingCopy,
)
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


class CanonicalWorkingFileError(ValueError):
    """规范工作副本不存在、未就绪、版本失效或存在歧义。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码，供 API、Tool 和选择卡使用同一降级语义。"""

        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CanonicalWorkingFile:
    """共享文件的规范身份快照。"""

    working_copy: WorkingCopy
    document_version: DocumentVersion
    source_document_id: str
    source_document_version_id: str
    mapped_from_upload: bool


class CanonicalWorkingFileResolver:
    """把附件、建议或共享文档解析到唯一当前工作副本。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话，禁止跨请求缓存可变工作副本。"""

        self.db = db

    def resolve_document(
        self,
        *,
        document_id: str,
        document_version_id: str | None = None,
        allow_trashed: bool = False,
    ) -> CanonicalWorkingFile:
        """解析一个 Document，并严格校验当前版本内容身份。

        SHA-256 只验证映射完整性，不参与多个候选之间的自动合并或选择。
        """

        shared_workspace_id = get_shared_workspace_id(self.db)
        statuses = {"ACTIVE", "TRASHED"} if allow_trashed else {"ACTIVE"}
        direct_query = self.db.query(WorkingCopy).filter(
            WorkingCopy.workspace_id == shared_workspace_id,
            WorkingCopy.document_id == document_id,
            WorkingCopy.status.in_(statuses),
        )
        direct = direct_query.all()
        if direct:
            working_copy = self._require_unique(direct)
            version = self._current_version(working_copy)
            if document_version_id and version.id != document_version_id:
                raise CanonicalWorkingFileError(
                    "DOCUMENT_VERSION_CHANGED",
                    "文件内容版本已经变化，请重新读取并确认分类。",
                )
            self._validate_content(working_copy=working_copy, version=version)
            return CanonicalWorkingFile(
                working_copy=working_copy,
                document_version=version,
                source_document_id=document_id,
                source_document_version_id=version.id,
                mapped_from_upload=False,
            )

        source_versions_query = self.db.query(DocumentVersion).filter(
            DocumentVersion.document_id == document_id
        )
        if document_version_id:
            source_versions_query = source_versions_query.filter(
                DocumentVersion.id == document_version_id
            )
        source_versions = source_versions_query.order_by(
            DocumentVersion.version_number.desc()
        ).all()
        if not source_versions:
            raise CanonicalWorkingFileError(
                "DOCUMENT_NOT_FOUND", "没有找到对应的文件版本。"
            )

        archive_rows = (
            self.db.query(UploadArchiveRecord, DocumentVersion)
            .join(
                DocumentVersion,
                DocumentVersion.id == UploadArchiveRecord.upload_document_version_id,
            )
            .filter(
                UploadArchiveRecord.upload_document_version_id.in_(
                    [item.id for item in source_versions]
                )
            )
            .all()
        )
        if not archive_rows:
            raise CanonicalWorkingFileError(
                "WORKING_COPY_NOT_FOUND",
                "没有找到对应的共享工作副本，请重新附加文件。",
            )

        managed_file_ids = [
            row.managed_file_id for row, _ in archive_rows if row.managed_file_id
        ]
        candidates = (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.workspace_id == shared_workspace_id,
                WorkingCopy.managed_file_id.in_(managed_file_ids),
                WorkingCopy.is_primary_import.is_(True),
                WorkingCopy.status.in_(statuses),
            )
            .all()
            if managed_file_ids
            else []
        )
        if not candidates:
            pending_statuses = {
                "DUPLICATE_CHECK_PENDING",
                "ARCHIVE_PENDING",
                "ARCHIVED",
                "IMPORT_PENDING",
                "IMPORTING",
            }
            if any(record.status in pending_statuses for record, _ in archive_rows):
                raise CanonicalWorkingFileError(
                    "WORKING_COPY_NOT_READY",
                    "文件仍在后台归档或创建共享工作副本，请稍后重试。",
                )
            raise CanonicalWorkingFileError(
                "WORKING_COPY_NOT_FOUND",
                "没有找到对应的共享工作副本，请重新附加文件。",
            )

        working_copy = self._require_unique(candidates)
        current_version = self._current_version(working_copy)
        source_by_id = {version.id: version for _, version in archive_rows}
        matched_source = next(
            (
                source_by_id[record.upload_document_version_id]
                for record, _ in archive_rows
                if record.managed_file_id == working_copy.managed_file_id
                and record.upload_document_version_id in source_by_id
            ),
            None,
        )
        if matched_source is None:
            raise CanonicalWorkingFileError(
                "WORKING_COPY_MAPPING_INVALID",
                "文件归档记录与共享工作副本不一致，请重新处理该文件。",
            )
        self._validate_content(working_copy=working_copy, version=current_version)
        if (
            matched_source.sha256 != current_version.sha256
            or matched_source.sha256 != working_copy.content_sha256
        ):
            raise CanonicalWorkingFileError(
                "DOCUMENT_VERSION_CHANGED",
                "分类建议对应的内容已经变化，请重新读取并分类。",
            )
        return CanonicalWorkingFile(
            working_copy=working_copy,
            document_version=current_version,
            source_document_id=document_id,
            source_document_version_id=matched_source.id,
            mapped_from_upload=True,
        )

    def resolve_suggestion(
        self,
        suggestion: DocumentCategorySuggestion,
        *,
        allow_trashed: bool = False,
    ) -> CanonicalWorkingFile:
        """把分类建议映射到规范共享工作副本，拒绝旧版本证据。"""

        return self.resolve_document(
            document_id=suggestion.document_id,
            document_version_id=suggestion.document_version_id or None,
            allow_trashed=allow_trashed,
        )

    def _require_unique(self, candidates: list[WorkingCopy]) -> WorkingCopy:
        """只接受唯一工作副本，不按文件名、哈希或版本替用户挑选。"""

        if len(candidates) != 1:
            raise CanonicalWorkingFileError(
                "WORKING_COPY_AMBIGUOUS",
                "找到多个可能的共享文件，请先选择具体文件。",
            )
        return candidates[0]

    def _current_version(self, working_copy: WorkingCopy) -> DocumentVersion:
        """读取工作副本当前版本并验证反向身份。"""

        version = (
            self.db.get(DocumentVersion, working_copy.current_version_id)
            if working_copy.current_version_id
            else None
        )
        if version is None or version.document_id != working_copy.document_id:
            raise CanonicalWorkingFileError(
                "WORKING_COPY_VERSION_INVALID",
                "共享工作副本缺少有效内容版本。",
            )
        return version

    @staticmethod
    def _validate_content(
        *, working_copy: WorkingCopy, version: DocumentVersion
    ) -> None:
        """使用哈希验证当前快照，哈希不能替代业务身份。"""

        if not version.sha256 or version.sha256 != working_copy.content_sha256:
            raise CanonicalWorkingFileError(
                "WORKING_COPY_HASH_MISMATCH",
                "共享工作副本内容校验失败，请先重新同步文件。",
            )


class SharedWorkingCopyAccessPolicy:
    """统一共享活动文件读取和高风险操作范围。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def scope_readable(self, query: Query) -> Query:
        """为已经包含 WorkingCopy 的查询附加共享活动范围。"""

        return query.filter(
            WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
            WorkingCopy.status == "ACTIVE",
        )

    def get_readable(self, *, working_copy_id: str) -> WorkingCopy | None:
        """返回任意登录用户都可读取的共享活动工作副本。"""

        return (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.id == working_copy_id,
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                WorkingCopy.status == "ACTIVE",
            )
            .one_or_none()
        )

    def get_status_visible(self, *, working_copy_id: str) -> WorkingCopy | None:
        """读取活动或已删除状态，仅供恢复提示，不授权读取回收站正文。"""

        return (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.id == working_copy_id,
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                WorkingCopy.status.in_({"ACTIVE", "TRASHED"}),
            )
            .one_or_none()
        )

    def require_mutable(self, *, working_copy_id: str) -> WorkingCopy:
        """返回可生成 OperationPlan 的共享活动工作副本。"""

        working_copy = self.get_readable(working_copy_id=working_copy_id)
        if working_copy is None:
            raise CanonicalWorkingFileError(
                "WORKING_COPY_NOT_ACTIVE",
                "文件不存在、仍在后台处理或已经删除，请重新选择。",
            )
        return working_copy
