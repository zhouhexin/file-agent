"""不可变材料包用途策略测试。"""

from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.purpose_policy import (
    PurposePackageMember,
    PurposePackageSnapshot,
    evaluate_purpose_package,
)


def _package() -> PurposePackageSnapshot:
    """构造只包含单一冻结成员的招聘用途包。"""

    return PurposePackageSnapshot(
        id="package-1",
        workspace_id="workspace-1",
        root_key="managed",
        source_container_id="container-1",
        purpose_category_id="college.hr.faculty-recruitment",
        taxonomy_key="unified_school_file_classification",
        taxonomy_version="2026-09-v10",
        policy_id="recruitment-package",
        policy_version="1",
        manifest_digest="manifest-1",
        members=(
            PurposePackageMember(
                managed_file_id="managed-1",
                document_version_id="version-1",
                sha256="a" * 64,
            ),
        ),
        authorization_source="INITIAL_INGEST_TASK",
        source_request_id="request-1",
    )


def test_verified_package_returns_purpose_candidate_without_fake_probability():
    """T08：合法包生成独立主用途候选，置信度不得伪造为 1.0。"""

    result = evaluate_purpose_package(
        package=_package(),
        taxonomy=load_default_taxonomy(),
        document_version_id="version-1",
        content_sha256="a" * 64,
        managed_file_id="managed-1",
    )

    assert result.valid is True
    assert result.candidate["category_id"] == "college.hr.faculty-recruitment"
    assert result.candidate["confidence"] < 1.0
    assert result.candidate["source"] == "verified_purpose_package"


def test_directory_neighbor_and_hash_mismatch_do_not_inherit_package():
    """T09：同目录散件和哈希变化均不能继承冻结用途。"""

    not_member = evaluate_purpose_package(
        package=_package(),
        taxonomy=load_default_taxonomy(),
        document_version_id="version-neighbor",
        content_sha256="a" * 64,
        managed_file_id="managed-neighbor",
    )
    forged = evaluate_purpose_package(
        package=_package(),
        taxonomy=load_default_taxonomy(),
        document_version_id="version-1",
        content_sha256="b" * 64,
        managed_file_id="managed-1",
    )

    assert not_member.reason_codes == ("PURPOSE_MEMBER_NOT_FROZEN",)
    assert forged.reason_codes == ("PURPOSE_MEMBER_HASH_MISMATCH",)
    assert not_member.candidate is None
    assert forged.candidate is None
