"""从受控具体材料容器生成不可变用途包。

目录只用于确定候选成员边界，不能决定业务用途。系统从叶目录向上寻找最近的
具体材料容器且不越过宽泛集合根；只有至少一个成员的原始正文同时满足“精确
题名＋字段结构”，且包内不存在不同业务锚点时，才冻结成员版本和 SHA-256 并
允许其他附件继承用途。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.models import (
    DocumentExtractionRun,
    DocumentPage,
    ManagedFile,
    ManagedFileRevision,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.matcher import detect_structured_document_purpose
from app.modules.classification.purpose_policy import (
    PurposePackageMember,
    PurposePackageSnapshot,
)
from app.modules.classification.purpose_repository import PurposePackageRepository


_MAX_PACKAGE_MEMBERS = 50
_BROAD_CONTAINER_NAMES = {
    "",
    ".",
    "workdata",
    "外来应聘",
    "人事处",
    "职称评定",
    "专业认证",
    "办公",
    "学校文件",
}


@dataclass(frozen=True)
class PurposePackageResolution:
    """一次材料包解析结果，created 用于触发既有成员分类刷新。"""

    snapshot: PurposePackageSnapshot | None
    created: bool = False


class ManagedPurposePackageInferenceService:
    """为受管源修订查找或创建可验证的用途包。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话，不持有文件系统访问能力。"""

        self.db = db
        self.repository = PurposePackageRepository(db)

    def resolve(
        self,
        *,
        managed_file: ManagedFile,
        current_revision: ManagedFileRevision,
        workspace_id: str,
        root_key: str,
        current_document_version_id: str,
        current_sha256: str,
    ) -> PurposePackageResolution:
        """优先读取现有快照；不存在时按完整叶级容器尝试冻结新快照。"""

        taxonomy = load_default_taxonomy()
        existing = self.repository.find_for_member(
            document_version_id=current_document_version_id,
            sha256=current_sha256,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
        )
        if existing is not None:
            return PurposePackageResolution(existing, False)

        selected: tuple[
            str,
            list[tuple[ManagedFile, ManagedFileRevision]],
            str,
        ] | None = None
        # 先尝试最近叶目录，再逐级向上；这既能覆盖“人员/照片/附件”嵌套包，
        # 又会在年份或宽泛集合根前停止，避免把整棵来源目录当作一个用途包。
        for container in _candidate_containers(managed_file.relative_path):
            members = self._load_complete_members(
                managed_file=managed_file,
                current_revision=current_revision,
                current_document_version_id=current_document_version_id,
                current_sha256=current_sha256,
                container=container,
            )
            if not 2 <= len(members) <= _MAX_PACKAGE_MEMBERS:
                continue
            anchors = self._detect_anchor_categories(members)
            if len(anchors) == 1:
                selected = (container, members, next(iter(anchors)))
                break
            if len(anchors) > 1:
                # 最近容器已经出现用途冲突时不能继续扩大边界碰碰运气。
                return PurposePackageResolution(None, False)
        if selected is None:
            return PurposePackageResolution(None, False)
        container, members, purpose_category_id = selected
        package_members = tuple(
            PurposePackageMember(
                managed_file_id=file.id,
                document_version_id=revision.analysis_document_version_id or "",
                sha256=revision.content_sha256 or "",
            )
            for file, revision in members
        )
        manifest_digest = _manifest_digest(
            source_container_id=f"{managed_file.root_id}:{container}",
            purpose_category_id=purpose_category_id,
            members=package_members,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
        )
        if record := self.repository.find_by_manifest_digest(manifest_digest):
            return PurposePackageResolution(self.repository.to_snapshot(record), False)
        anchor_revision_id = next(
            revision.id
            for file, revision in members
            if self._document_anchor(file, revision) == purpose_category_id
        )
        snapshot = PurposePackageSnapshot(
            id=str(uuid4()),
            workspace_id=workspace_id,
            root_key=root_key,
            source_container_id=hashlib.sha256(
                f"{managed_file.root_id}:{container}".encode("utf-8")
            ).hexdigest(),
            purpose_category_id=purpose_category_id,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            policy_id="structured-material-package",
            policy_version="2",
            manifest_digest=manifest_digest,
            members=package_members,
            authorization_source="VERIFIED_STRUCTURE_MANIFEST",
            source_request_id=anchor_revision_id,
        )
        self.repository.create(snapshot)
        return PurposePackageResolution(snapshot, True)

    def _load_complete_members(
        self,
        *,
        managed_file: ManagedFile,
        current_revision: ManagedFileRevision,
        current_document_version_id: str,
        current_sha256: str,
        container: str,
    ) -> list[tuple[ManagedFile, ManagedFileRevision]]:
        """读取同一叶级容器的完整当前成员；任何成员未分析时关闭式返回空。"""

        files = (
            self.db.query(ManagedFile)
            .filter(
                ManagedFile.root_id == managed_file.root_id,
                ManagedFile.status == "ACTIVE",
            )
            .all()
        )
        files = [item for item in files if _is_container_member(item.relative_path, container)]
        if not 2 <= len(files) <= _MAX_PACKAGE_MEMBERS:
            return []
        result: list[tuple[ManagedFile, ManagedFileRevision]] = []
        for item in files:
            if item.id == managed_file.id:
                current_revision.analysis_document_version_id = current_document_version_id
                current_revision.content_sha256 = current_sha256
                result.append((item, current_revision))
                continue
            revision = (
                self.db.query(ManagedFileRevision)
                .filter(
                    ManagedFileRevision.managed_file_id == item.id,
                    ManagedFileRevision.is_current.is_(True),
                    ManagedFileRevision.status == "READY",
                )
                .first()
            )
            if (
                revision is None
                or not revision.analysis_document_version_id
                or not revision.content_sha256
            ):
                return []
            result.append((item, revision))
        result.sort(key=lambda pair: pair[0].relative_path.casefold())
        return result

    def _detect_anchor_categories(
        self, members: list[tuple[ManagedFile, ManagedFileRevision]]
    ) -> set[str]:
        """收集包内强结构锚点，多个业务用途并存时拒绝继承。"""

        return {
            category_id
            for file, revision in members
            if (category_id := self._document_anchor(file, revision))
        }

    def _document_anchor(
        self, managed_file: ManagedFile, revision: ManagedFileRevision
    ) -> str | None:
        """从当前成员已经持久化的原始页面文本识别结构锚点。"""

        pages = (
            self.db.query(DocumentPage)
            .join(
                DocumentExtractionRun,
                DocumentPage.extraction_run_id == DocumentExtractionRun.id,
            )
            .filter(
                DocumentExtractionRun.document_version_id
                == revision.analysis_document_version_id
            )
            .order_by(DocumentPage.page_number.asc().nullslast())
            .limit(20)
            .all()
        )
        return detect_structured_document_purpose(
            filename=managed_file.filename,
            full_text="\n".join(page.text_content or "" for page in pages),
        )


def _candidate_containers(relative_path: str) -> tuple[str, ...]:
    """从最近叶目录向上返回具体候选容器，遇到宽泛或年份层即停止。"""

    parent = _normalized_parent(relative_path)
    if not parent or parent == ".":
        return ()
    broad_names = {item.casefold() for item in _BROAD_CONTAINER_NAMES}
    candidates: list[str] = []
    current = PurePosixPath(parent)
    while current.as_posix() not in {"", "."}:
        name = current.name.strip()
        if name.casefold() in broad_names or (name.isdigit() and len(name) in {2, 4}):
            break
        candidates.append(current.as_posix())
        current = current.parent
    return tuple(candidates)


def _is_container_member(relative_path: str, container: str) -> bool:
    """判断文件是否位于冻结容器内，按完整路径段比较而非字符串前缀。"""

    normalized = str(relative_path or "").replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    container_path = PurePosixPath(container)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return False
    try:
        path.relative_to(container_path)
    except ValueError:
        return False
    return path != container_path


def _normalized_parent(relative_path: str) -> str:
    """规范化受管相对路径的父目录，不接受越级片段。"""

    normalized = str(relative_path or "").replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return ""
    return path.parent.as_posix()


def _manifest_digest(
    *,
    source_container_id: str,
    purpose_category_id: str,
    members: tuple[PurposePackageMember, ...],
    taxonomy_key: str,
    taxonomy_version: str,
) -> str:
    """对规范化成员事实计算稳定摘要，路径变化会产生新快照。"""

    payload = {
        "source_container_id": source_container_id,
        "purpose_category_id": purpose_category_id,
        "taxonomy_key": taxonomy_key,
        "taxonomy_version": taxonomy_version,
        "members": sorted(
            (member.model_dump(mode="json") for member in members),
            key=lambda item: (
                str(item.get("managed_file_id") or ""),
                str(item.get("document_version_id") or ""),
            ),
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
