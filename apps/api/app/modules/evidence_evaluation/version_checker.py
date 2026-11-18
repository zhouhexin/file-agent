"""只读核对技术版本；不推断法规、通知或业务事实是否失效。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Conversation, DocumentIndexRun, DocumentVersion, ManagedFile,
    ManagedFileRevision, ManagedRoot, Workspace, WorkingCopy,
)
from app.modules.chunks.service import INDEX_VERSION
from app.modules.evidence_answer.schemas import EvidenceItem, EvidencePackage
from app.modules.evidence_evaluation.schemas import CheckResult, EvidenceIssue, TemporalFocus
from app.modules.evidence_evaluation.validator import EvaluationValidationError
from app.modules.file_lifecycle.shared_workspace import SHARED_WORKSPACE_SYSTEM_KEY


@dataclass(frozen=True)
class VersionSnapshot:
    """后端授权查询或离线冻结快照给出的版本事实，不接受 LLM 生成。"""

    status: Literal["VERIFIED", "UNVERIFIABLE"]
    current_version_id: str | None = None


def infer_temporal_focus(question: str) -> TemporalFocus:
    """仅在问题明确时识别当前/历史；无时间线索绝不默认判过期。"""

    current = bool(re.search(r"当前|现在|最新|现行|目前|\b(?:current|latest|now)\b", question, re.I))
    historical = bool(re.search(
        r"历史|当时|旧版|原版|第\s*\d+\s*版|截至|当年|去年|前年|(?<!\d)(?:19|20)\d{2}(?!\d)|"
        r"historical|previous|as of", question, re.I,
    ))
    if current and historical:
        return "UNKNOWN"
    if current:
        return "CURRENT"
    if historical:
        return "HISTORICAL"
    return "UNKNOWN"


class AuthorizedVersionReader:
    """使用服务端冻结的允许对象集合核对版本，不按客户端 ID 扩大查询。"""

    def __init__(
        self, *, db: Session, user_id: str, conversation_id: str, workspace_id: str,
        working_copy_ids: frozenset[str], revision_ids: frozenset[str],
    ) -> None:
        """授权集合必须来自既有检索服务已经通过校验的数据库记录。"""

        self.db = db
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.workspace_id = workspace_id
        self.working_copy_ids = working_copy_ids
        self.revision_ids = revision_ids

    def __call__(self, package: EvidencePackage) -> dict[str, VersionSnapshot]:
        """Core SELECT + 连接级保存点隔离错误，不 flush/commit 主回答会话。"""

        # Session.begin_nested 会自动 flush；这里特意使用连接级保存点，避免旁观
        # 检查把原回答未提交的业务变更提前写入，SQL 失败也不会污染 PostgreSQL 事务。
        with self.db.no_autoflush:
            connection = self.db.connection()
            with connection.begin_nested():
                conversation = connection.execute(select(Conversation.id).where(
                    Conversation.id == self.conversation_id,
                    Conversation.user_id == self.user_id,
                )).first()
                shared = connection.execute(select(Workspace.id).where(
                    Workspace.id == self.workspace_id,
                    Workspace.system_key == SHARED_WORKSPACE_SYSTEM_KEY,
                )).first()
                if conversation is None or shared is None:
                    raise EvaluationValidationError("VERSION_ACCESS_DENIED")
                # 同一内容版本通常有多个 Chunk，共用版本快照避免逐 Chunk 重复 SQL。
                cache: dict[tuple, VersionSnapshot] = {}
                snapshots = {}
                for item in package.evidence_items:
                    key = (item.working_copy_id, item.managed_file_revision_id,
                           item.document_id, item.document_version_id)
                    if key not in cache:
                        cache[key] = self._read(connection, item)
                    snapshots[item.evidence_id] = cache[key]
                return snapshots

    def _read(self, connection, item: EvidenceItem) -> VersionSnapshot:
        if item.working_copy_id:
            if item.working_copy_id not in self.working_copy_ids:
                raise EvaluationValidationError("VERSION_ACCESS_DENIED")
            row = connection.execute(select(
                WorkingCopy.current_version_id, WorkingCopy.status,
            ).where(
                WorkingCopy.id == item.working_copy_id,
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.document_id == item.document_id,
            )).mappings().first()
            if row is None or row["status"] != "ACTIVE":
                return VersionSnapshot("UNVERIFIABLE")
            current_version_id = row["current_version_id"]
        elif item.managed_file_revision_id:
            if item.managed_file_revision_id not in self.revision_ids:
                raise EvaluationValidationError("VERSION_ACCESS_DENIED")
            source = connection.execute(select(
                ManagedFileRevision.managed_file_id,
            ).join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
                .join(ManagedRoot, ManagedRoot.id == ManagedFile.root_id).where(
                    ManagedFileRevision.id == item.managed_file_revision_id,
                    ManagedFileRevision.analysis_document_id == item.document_id,
                    ManagedFileRevision.analysis_document_version_id == item.document_version_id,
                    ManagedFile.status == "ACTIVE", ManagedRoot.enabled.is_(True),
                )).first()
            if source is None:
                return VersionSnapshot("UNVERIFIABLE")
            revisions = connection.execute(select(
                ManagedFileRevision.analysis_document_version_id, ManagedFileRevision.status,
                ManagedFileRevision.analysis_document_id,
            ).where(
                ManagedFileRevision.managed_file_id == source[0],
                ManagedFileRevision.is_current.is_(True),
            )).all()
            if len(revisions) != 1 or revisions[0].status != "READY":
                return VersionSnapshot("UNVERIFIABLE")
            # 源修订 ID 与内容版本 ID 是两种对象，绝不互相比大小或直接比较。
            current_version_id = revisions[0].analysis_document_version_id
            current_binding = connection.execute(select(DocumentVersion.id).where(
                DocumentVersion.id == current_version_id,
                DocumentVersion.document_id == revisions[0].analysis_document_id,
            )).first()
            if current_binding is None:
                return VersionSnapshot("UNVERIFIABLE")
        else:
            return VersionSnapshot("UNVERIFIABLE")

        original = connection.execute(select(DocumentVersion.document_id).where(
            DocumentVersion.id == item.document_version_id,
        )).first()
        if original is None or original[0] != item.document_id:
            raise EvaluationValidationError("VERSION_BINDING_INVALID")
        if item.working_copy_id:
            current = connection.execute(select(DocumentVersion.id).where(
                DocumentVersion.id == current_version_id,
                DocumentVersion.document_id == item.document_id,
            )).first()
            if current is None:
                return VersionSnapshot("UNVERIFIABLE")
        ready = connection.execute(select(DocumentIndexRun.id).where(
            DocumentIndexRun.document_version_id == current_version_id,
            DocumentIndexRun.status == "COMPLETED",
            DocumentIndexRun.index_version == INDEX_VERSION,
        )).first()
        return (
            VersionSnapshot("VERIFIED", current_version_id)
            if ready is not None else VersionSnapshot("UNVERIFIABLE")
        )


class VersionChecker:
    """确定性版本检查器，可注入冻结快照开展无数据库离线评测。"""

    def __init__(self, reader: Callable[[EvidencePackage], dict[str, VersionSnapshot]]) -> None:
        """reader 只允许返回版本事实，不能执行物理文件操作。"""

        self.reader = reader

    def check(self, package: EvidencePackage, *, temporal_focus: TemporalFocus) -> CheckResult:
        """即使是历史问题也校验授权，只跳过过期判定，不跳过权限。"""

        try:
            snapshots = self.reader(package)
            if set(snapshots) != {item.evidence_id for item in package.evidence_items}:
                raise EvaluationValidationError("INCOMPLETE_VERSION_SNAPSHOT")
            issues = []
            limitations = ["BUSINESS_VALIDITY_NOT_VERIFIED"]
            unknown = False
            for item in package.evidence_items:
                snapshot = snapshots[item.evidence_id]
                if snapshot.status != "VERIFIED" or not snapshot.current_version_id:
                    unknown = True
                elif temporal_focus == "CURRENT" and snapshot.current_version_id != item.document_version_id:
                    issues.append(EvidenceIssue(
                        type="OUTDATED", reason="当前状态问题引用了已被替换的内容版本。",
                        reason_code="CONTENT_VERSION_REPLACED", evidence_ids=[item.evidence_id],
                    ))
            if unknown:
                limitations.append("VERSION_UNVERIFIABLE")
            if temporal_focus == "UNKNOWN":
                limitations.append("TEMPORAL_FOCUS_UNKNOWN")
            return CheckResult(
                status="PARTIAL" if unknown or temporal_focus == "UNKNOWN" else "COMPLETED",
                issues=issues, limitations=limitations,
            )
        except Exception as exc:
            # 不保存异常消息，其中可能带 SQL、文件路径或其他用户的对象信息。
            return CheckResult(status="EVALUATION_FAILED", error_codes=[
                str(exc) if isinstance(exc, EvaluationValidationError) else "VERSION_CHECK_FAILED",
            ])
