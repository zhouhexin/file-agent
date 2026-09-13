"""落位前从持久化用途包重新核验成员事实，不信任候选自报的用途标记。"""

from sqlalchemy.orm import Session

from app.db.models import (
    DocumentVersion,
    ManagedFile,
    ManagedFileRevision,
    ManagedRoot,
    WorkingCopy,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.purpose_policy import (
    PurposePolicyResult,
    evaluate_purpose_package,
)
from app.modules.classification.purpose_repository import PurposePackageRepository


def validate_placement_purpose(
    db: Session, *, working_copy: WorkingCopy, candidate: dict | None
) -> PurposePolicyResult | None:
    """校验当前工作版本与冻结源版本的血缘、哈希、组织范围和受控分类。"""

    if not candidate or candidate.get("source") != "verified_purpose_package":
        return None
    invalid = PurposePolicyResult(False, None, ("PURPOSE_PLACEMENT_INVALID",))
    evidence = [
        item
        for item in candidate.get("evidence_items", [])
        if isinstance(item, dict)
        and item.get("type") == "purpose_package_manifest"
        and item.get("source") == "verified_purpose_package"
    ]
    package_ids = {str(item.get("quote") or "") for item in evidence}
    if len(package_ids) != 1 or not all(package_ids):
        return invalid
    repository = PurposePackageRepository(db)
    record = repository.get(next(iter(package_ids)))
    version = db.get(DocumentVersion, working_copy.current_version_id)
    managed_file = db.get(ManagedFile, working_copy.managed_file_id)
    root = db.get(ManagedRoot, managed_file.root_id) if managed_file else None
    if (
        record is None
        or version is None
        or root is None
        or version.document_id != working_copy.document_id
        or record.workspace_id != working_copy.workspace_id
        or record.root_key != root.root_key
        or not record.authorization_source
        or not record.source_request_id
        or not record.manifest_digest
        or record.purpose_category_id != candidate.get("category_id")
        or record.taxonomy_key != candidate.get("taxonomy_key")
        or record.taxonomy_version != candidate.get("taxonomy_version")
        or version.sha256.casefold() != working_copy.content_sha256.casefold()
    ):
        return invalid
    # 源分析版本与物化工作版本 ID 不同，必须沿同一 managed_file 的当前修订验证，
    # 不能按相同目录或相同文件名继承；工作副本已编辑时哈希检查会关闭继承。
    revision = (
        db.query(ManagedFileRevision)
        .filter(
            ManagedFileRevision.managed_file_id == managed_file.id,
            ManagedFileRevision.is_current.is_(True),
            ManagedFileRevision.status == "READY",
        )
        .one_or_none()
    )
    if revision is None or not revision.analysis_document_version_id:
        return invalid
    if (revision.content_sha256 or "").casefold() != version.sha256.casefold():
        return invalid
    if any(
        item.get("manifest_digest", record.manifest_digest) != record.manifest_digest
        for item in evidence
    ):
        return invalid
    return evaluate_purpose_package(
        package=repository.to_snapshot(record),
        taxonomy=load_default_taxonomy(),
        document_version_id=revision.analysis_document_version_id,
        content_sha256=version.sha256,
        managed_file_id=managed_file.id,
    )
