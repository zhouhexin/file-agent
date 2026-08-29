"""三层文件生命周期持久化仓库。

仓库只处理确定业务 ID 的数据库读写，不接收 LLM 生成路径，也不执行文件系统操作。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentVersion,
    ManagedFile,
    ManagedRoot,
    UploadArchiveRecord,
    UploadDuplicateCandidate,
    UploadDuplicateReview,
    WorkingCopy,
    WorkingCopyPathRecord,
    WorkingCopyRoot,
    Workspace,
    utcnow,
)
from app.modules.file_lifecycle.shared_workspace import (
    SHARED_WORKSPACE_STORAGE_KEY,
    SHARED_WORKSPACE_TYPE,
    get_shared_workspace_id,
)


class FileLifecycleRepository:
    """封装上传归档、重复确认和工作副本关系的数据库操作。"""

    def __init__(self, db: Session) -> None:
        """保存请求级或 worker 级数据库会话。"""

        self.db = db

    def create_upload_version(self, *, document: Document, storage_path: str, created_by: str) -> DocumentVersion:
        """为新上传暂存创建不可变 UPLOAD 版本。"""

        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            storage_tier="UPLOAD",
            storage_path=storage_path,
            filename=document.original_filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            source_type="UPLOAD",
            created_by=created_by,
        )
        self.db.add(version)
        self.db.flush()
        return version

    def create_upload_lifecycle(
        self,
        *,
        version: DocumentVersion,
        document: Document,
        conversation_id: str | None,
        ttl_hours: int,
    ) -> tuple[UploadArchiveRecord, UploadDuplicateReview]:
        """在同一事务中创建归档状态和重复确认记录。"""

        if not document.workspace_id:
            raise ValueError("上传 Document 缺少 workspace_id")
        archive = UploadArchiveRecord(
            upload_document_version_id=version.id,
            content_sha256=version.sha256,
            status="DUPLICATE_CHECK_PENDING",
        )
        review = UploadDuplicateReview(
            upload_document_version_id=version.id,
            conversation_id=conversation_id,
            workspace_id=document.workspace_id,
            user_id=document.user_id,
            status="CHECKING",
            expires_at=utcnow() + timedelta(hours=ttl_hours),
        )
        self.db.add_all([archive, review])
        self.db.flush()
        return archive, review

    def get_upload_version(self, version_id: str) -> DocumentVersion | None:
        """按 ID 查询上传版本。"""

        return self.db.get(DocumentVersion, version_id)

    def get_archive_by_version(self, version_id: str) -> UploadArchiveRecord | None:
        """查询上传版本唯一归档记录。"""

        return (
            self.db.query(UploadArchiveRecord)
            .filter(UploadArchiveRecord.upload_document_version_id == version_id)
            .one_or_none()
        )

    def get_review_by_version(self, version_id: str) -> UploadDuplicateReview | None:
        """查询上传版本唯一重复确认。"""

        return (
            self.db.query(UploadDuplicateReview)
            .filter(UploadDuplicateReview.upload_document_version_id == version_id)
            .one_or_none()
        )

    def get_owned_review(self, *, review_id: str, user_id: str) -> UploadDuplicateReview | None:
        """按用户边界查询重复确认，跨用户一律表现为不存在。"""

        return (
            self.db.query(UploadDuplicateReview)
            .filter(UploadDuplicateReview.id == review_id, UploadDuplicateReview.user_id == user_id)
            .one_or_none()
        )

    def list_candidates(self, review_id: str) -> list[UploadDuplicateCandidate]:
        """按排名读取候选。"""

        return (
            self.db.query(UploadDuplicateCandidate)
            .filter(UploadDuplicateCandidate.duplicate_review_id == review_id)
            .order_by(UploadDuplicateCandidate.rank.asc())
            .all()
        )

    def replace_exact_candidates(
        self,
        *,
        review: UploadDuplicateReview,
        upload_document_id: str,
        sha256: str,
        max_candidates: int,
        filename: str | None = None,
    ) -> list[UploadDuplicateCandidate]:
        """生成精确内容和同名上传候选。

        同步阶段允许所有同名文件进入共享工作目录；用户再次上传同名文件时才在这里
        提示。精确 SHA-256 仍优先，同一既有文件不会同时返回两张候选卡。
        """

        self.db.query(UploadDuplicateCandidate).filter(
            UploadDuplicateCandidate.duplicate_review_id == review.id
        ).delete(synchronize_session=False)
        rows = (
            self.db.query(ManagedFile, WorkingCopy, Document)
            .outerjoin(WorkingCopy, WorkingCopy.managed_file_id == ManagedFile.id)
            .outerjoin(Document, WorkingCopy.document_id == Document.id)
            .filter(ManagedFile.content_sha256 == sha256, ManagedFile.status == "ACTIVE")
            .filter(
                or_(
                    WorkingCopy.id.is_(None),
                    and_(
                        WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                        WorkingCopy.status == "ACTIVE",
                    ),
                )
            )
            .filter(or_(Document.id.is_(None), Document.id != upload_document_id))
            .order_by(ManagedFile.archived_at.asc(), ManagedFile.created_at.asc())
            .limit(max_candidates)
            .all()
        )
        candidates: list[UploadDuplicateCandidate] = []
        for rank, (managed_file, working_copy, candidate_document) in enumerate(rows, start=1):
            scope = self._candidate_scope(
                review=review,
                working_copy=working_copy,
                candidate_document=candidate_document,
            )
            # 工作副本进入唯一共享工作区后不再按上传用户隔离；上传来源本身仍保持私有。
            can_use_existing = bool(
                working_copy
                and candidate_document
                and working_copy.workspace_id == get_shared_workspace_id(self.db)
                and working_copy.status == "ACTIVE"
            )
            if working_copy is None:
                managed_root = self.db.get(ManagedRoot, managed_file.root_id)
                summary = {
                    "message": "检测到尚未同步到工作副本的相同文件",
                    "filename": managed_file.filename,
                    "managed_root_key": managed_root.root_key if managed_root else None,
                    "managed_relative_path": managed_file.relative_path,
                    "similarity_bucket": "100%",
                    "file_status": "ACTIVE",
                }
            elif scope == "CROSS_USER":
                summary = {
                    "message": "系统检测到相同内容",
                    "similarity_bucket": "100%",
                }
            else:
                summary = {
                    "message": "检测到共享工作目录中可直接使用的相同文件",
                    "filename": working_copy.filename if working_copy else managed_file.filename,
                    "relative_path": working_copy.relative_path if can_use_existing else None,
                    "updated_at": working_copy.updated_at.isoformat() if can_use_existing else None,
                    "file_status": "ACTIVE",
                }
            candidate = UploadDuplicateCandidate(
                duplicate_review_id=review.id,
                candidate_managed_file_id=managed_file.id,
                candidate_working_copy_id=working_copy.id if working_copy else None,
                match_type="EXACT_SHA256",
                match_scope=scope,
                similarity_score=1.0,
                match_evidence_json={"sha256_equal": True},
                user_visible_summary_json=summary,
                rank=rank,
            )
            self.db.add(candidate)
            candidates.append(candidate)
        remaining = max(0, max_candidates - len(candidates))
        exact_managed_ids = {item.candidate_managed_file_id for item in candidates}
        normalized_filename = str(filename or "").strip().lower()
        if remaining and normalized_filename:
            same_name_rows = (
                self.db.query(ManagedFile, WorkingCopy, Document)
                .outerjoin(WorkingCopy, WorkingCopy.managed_file_id == ManagedFile.id)
                .outerjoin(Document, WorkingCopy.document_id == Document.id)
                .filter(
                    ManagedFile.status == "ACTIVE",
                    or_(
                        WorkingCopy.id.is_(None),
                        and_(
                            WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                            WorkingCopy.status == "ACTIVE",
                        ),
                    ),
                    func.lower(func.coalesce(WorkingCopy.filename, ManagedFile.filename))
                    == normalized_filename,
                    or_(Document.id.is_(None), Document.id != upload_document_id),
                )
                .filter(
                    ~ManagedFile.id.in_(exact_managed_ids)
                    if exact_managed_ids
                    else ManagedFile.id != ""
                )
                .order_by(WorkingCopy.updated_at.desc())
                .limit(remaining)
                .all()
            )
            for offset, (managed_file, working_copy, candidate_document) in enumerate(
                same_name_rows,
                start=1,
            ):
                scope = self._candidate_scope(
                    review=review,
                    working_copy=working_copy,
                    candidate_document=candidate_document,
                )
                # 同名活动工作副本属于共享业务事实，所有登录用户都可以直接选择。
                can_use_existing = bool(
                    working_copy
                    and working_copy.workspace_id == get_shared_workspace_id(self.db)
                    and working_copy.status == "ACTIVE"
                )
                if working_copy is None:
                    managed_root = self.db.get(ManagedRoot, managed_file.root_id)
                    summary = {
                        "message": "检测到尚未同步到工作副本的同名文件",
                        "filename": managed_file.filename,
                        "managed_root_key": managed_root.root_key if managed_root else None,
                        "managed_relative_path": managed_file.relative_path,
                        "file_status": "ACTIVE",
                    }
                elif scope == "CROSS_USER":
                    summary = {"message": "系统检测到同名文件"}
                else:
                    summary = {
                        "message": "检测到共享工作目录中可直接使用的同名文件",
                        "filename": working_copy.filename,
                        "relative_path": working_copy.relative_path if can_use_existing else None,
                        "updated_at": working_copy.updated_at.isoformat(),
                        "file_status": "ACTIVE",
                    }
                candidate = UploadDuplicateCandidate(
                    duplicate_review_id=review.id,
                    candidate_managed_file_id=managed_file.id,
                    candidate_working_copy_id=working_copy.id if working_copy else None,
                    match_type="SAME_FILENAME",
                    match_scope=scope,
                    similarity_score=1.0,
                    match_evidence_json={"filename_equal": True},
                    user_visible_summary_json=summary,
                    rank=len(candidates) + offset,
                )
                self.db.add(candidate)
                candidates.append(candidate)
        self.db.flush()
        return candidates

    def _candidate_scope(
        self,
        *,
        review: UploadDuplicateReview,
        working_copy: WorkingCopy | None,
        candidate_document: Document | None,
    ) -> str:
        """根据共享工作区关系确定候选展示范围，上传来源用户不参与授权。"""

        if (
            working_copy
            and candidate_document
            and working_copy.workspace_id == get_shared_workspace_id(self.db)
        ):
            return "SAME_WORKSPACE"
        return "CROSS_USER"

    def get_or_create_archive_root(self, *, container_path: str) -> ManagedRoot:
        """创建上传归档专用受管原始目录索引。"""

        root = self.db.query(ManagedRoot).filter(ManagedRoot.root_key == "upload_archive").one_or_none()
        if root is None:
            root = ManagedRoot(
                root_key="upload_archive",
                display_name="上传附件原始文件",
                container_path=container_path,
                classification_mode="NONE",
                enabled=True,
                read_only=True,
                archive_write_enabled=True,
                allowed_operations_json=["scan", "list", "search", "read"],
            )
            self.db.add(root)
        else:
            root.container_path = container_path
            root.enabled = True
            root.read_only = True
            root.archive_write_enabled = True
            root.allowed_operations_json = ["scan", "list", "search", "read"]
        self.db.flush()
        return root

    def create_archived_managed_file(
        self,
        *,
        root: ManagedRoot,
        version: DocumentVersion,
        relative_path: str,
        relative_path_hash: str,
        file_identity: str | None,
    ) -> ManagedFile:
        """为已完成原子归档的文件创建不可变原始文件索引。"""

        existing = (
            self.db.query(ManagedFile)
            .filter(ManagedFile.source_upload_version_id == version.id)
            .one_or_none()
        )
        if existing is not None:
            return existing
        managed_file = ManagedFile(
            root_id=root.id,
            relative_path=relative_path,
            relative_path_hash=relative_path_hash,
            filename=version.filename,
            extension=Path(version.filename).suffix.lower(),
            size_bytes=version.size_bytes,
            fingerprint=version.sha256,
            content_sha256=version.sha256,
            file_identity=file_identity,
            source_type="UPLOAD_ARCHIVE",
            source_upload_version_id=version.id,
            archived_at=utcnow(),
            status="ACTIVE",
        )
        self.db.add(managed_file)
        self.db.flush()
        return managed_file

    def get_or_create_working_root(
        self,
        *,
        workspace_id: str,
        managed_root: ManagedRoot,
    ) -> WorkingCopyRoot:
        """创建工作区到受管原始目录的唯一工作副本根映射。

        系统共享工作区固定使用 ``shared/<root_key>``，绝不能把 UUID 工作区 ID
        写入物理目录，从而避免同一原始文件按用户重复复制。
        """

        root = (
            self.db.query(WorkingCopyRoot)
            .filter(
                WorkingCopyRoot.workspace_id == workspace_id,
                WorkingCopyRoot.managed_root_id == managed_root.id,
            )
            .one_or_none()
        )
        workspace = self.db.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError("工作副本根缺少有效工作区")
        storage_scope = (
            SHARED_WORKSPACE_STORAGE_KEY
            if workspace.workspace_type == SHARED_WORKSPACE_TYPE
            else workspace.id
        )
        relative_storage_path = f"{storage_scope}/{managed_root.root_key}"
        if root is None:
            root = WorkingCopyRoot(
                workspace_id=workspace_id,
                managed_root_id=managed_root.id,
                root_key=managed_root.root_key,
                relative_storage_path=relative_storage_path,
                status="INITIALIZING",
            )
            self.db.add(root)
            self.db.flush()
        elif root.relative_storage_path != relative_storage_path:
            has_copies = (
                self.db.query(WorkingCopy.id)
                .filter(WorkingCopy.working_copy_root_id == root.id)
                .first()
                is not None
            )
            if has_copies:
                # 不能只改数据库前缀而让既有物理文件失联；启动布局修复任务完成前，
                # 新导入应重试而不是继续写入旧目录。
                raise RuntimeError("工作副本根等待异步布局修复")
            root.relative_storage_path = relative_storage_path
            root.root_key = managed_root.root_key
            root.status = "INITIALIZING"
            self.db.flush()
        return root

    def find_primary_working_copy(self, *, working_root_id: str, managed_file_id: str) -> WorkingCopy | None:
        """查询原始文件在工作副本根中的主导入映射，包括已进入回收站的副本。"""

        return (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.working_copy_root_id == working_root_id,
                WorkingCopy.managed_file_id == managed_file_id,
                WorkingCopy.is_primary_import.is_(True),
            )
            .one_or_none()
        )

    def list_owned_working_copies(self, *, workspace_id: str) -> list[tuple[WorkingCopy, WorkingCopyRoot]]:
        """列出当前工作区工作副本。"""

        return (
            self.db.query(WorkingCopy, WorkingCopyRoot)
            .join(WorkingCopyRoot, WorkingCopy.working_copy_root_id == WorkingCopyRoot.id)
            .filter(
                WorkingCopy.workspace_id == workspace_id,
                WorkingCopy.status.in_(["ACTIVE", "TRASHED"]),
            )
            .order_by(WorkingCopy.relative_path.asc())
            .all()
        )

    def list_user_working_copies(self, *, workspace_id: str, user_id: str) -> list[tuple[WorkingCopy, WorkingCopyRoot]]:
        """列出共享工作区中活动及回收站工作副本。

        ``user_id`` 仅保留兼容旧调用签名；共享文件读取由工作区和活动状态授权，
        上传来源、会话和反馈仍在各自服务中按用户隔离。这个状态列表还承担
        回收站页面与恢复结果展示，因此不能在仓储层提前丢弃 ``TRASHED``。
        """

        return (
            self.db.query(WorkingCopy, WorkingCopyRoot)
            .join(WorkingCopyRoot, WorkingCopy.working_copy_root_id == WorkingCopyRoot.id)
            .filter(
                WorkingCopy.workspace_id == workspace_id,
                WorkingCopy.status.in_(["ACTIVE", "TRASHED"]),
            )
            .order_by(WorkingCopy.relative_path.asc())
            .all()
        )

    def get_owned_working_copy(self, *, working_copy_id: str, workspace_id: str) -> WorkingCopy | None:
        """查询当前工作区的工作副本。"""

        return (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.id == working_copy_id,
                WorkingCopy.workspace_id == workspace_id,
                WorkingCopy.status.in_(["ACTIVE", "TRASHED"]),
            )
            .one_or_none()
        )

    def get_user_working_copy(self, *, working_copy_id: str, workspace_id: str, user_id: str) -> WorkingCopy | None:
        """读取共享工作副本。

        ``user_id`` 不再决定共享文件归属；高风险操作的发起人和确认人仍由
        OperationPlan 服务独立校验，不能据此读取其他用户私有审计。
        """

        return (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.id == working_copy_id,
                WorkingCopy.workspace_id == workspace_id,
                WorkingCopy.status.in_(["ACTIVE", "TRASHED"]),
            )
            .one_or_none()
        )

    def list_versions(self, document_id: str) -> list[DocumentVersion]:
        """按版本号读取文档版本。"""

        return (
            self.db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_number.asc())
            .all()
        )

    def list_path_records(self, working_copy_id: str) -> list[WorkingCopyPathRecord]:
        """按更新时间和序号读取不可变路径历史。"""

        return (
            self.db.query(WorkingCopyPathRecord)
            .filter(WorkingCopyPathRecord.working_copy_id == working_copy_id)
            .order_by(WorkingCopyPathRecord.updated_at.desc(), WorkingCopyPathRecord.sequence_number.desc())
            .all()
        )
