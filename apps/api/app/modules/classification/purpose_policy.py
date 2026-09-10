"""校验不可变材料包快照并生成独立用途候选。

材料包用途只能来自服务端持久化的成员清单和哈希，不能根据当前目录名或任意子目录动态继承。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.classification.matcher import flatten_category_paths
from app.modules.classification.schemas import Taxonomy


class PurposePackageMember(BaseModel):
    """用途包创建时冻结的单个成员事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    managed_file_id: str | None = None
    document_version_id: str
    sha256: str = Field(min_length=32, max_length=128)


class PurposePackageSnapshot(BaseModel):
    """不可变用途包；更新用途必须创建新快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    workspace_id: str
    root_key: str
    source_container_id: str
    purpose_category_id: str
    taxonomy_key: str
    taxonomy_version: str
    policy_id: str
    policy_version: str
    manifest_digest: str
    members: tuple[PurposePackageMember, ...]
    authorization_source: str
    source_request_id: str

    @model_validator(mode="after")
    def validate_unique_members(self) -> "PurposePackageSnapshot":
        """一个内容版本在同一快照中只能出现一次。"""

        version_ids = [member.document_version_id for member in self.members]
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("用途包成员 document_version_id 重复")
        return self


@dataclass(frozen=True)
class PurposePolicyResult:
    """用途包验证结果；失败不回退到目录猜测。"""

    valid: bool
    candidate: dict | None
    reason_codes: tuple[str, ...]


def evaluate_purpose_package(
    *,
    package: PurposePackageSnapshot,
    taxonomy: Taxonomy,
    document_version_id: str,
    content_sha256: str,
    managed_file_id: str | None = None,
) -> PurposePolicyResult:
    """按冻结版本、成员 ID 和哈希校验用途，不接受路径前缀匹配。"""

    reasons: list[str] = []
    if (
        package.taxonomy_key != taxonomy.key
        or package.taxonomy_version != taxonomy.version
    ):
        reasons.append("PURPOSE_TAXONOMY_STALE")
    member = next(
        (
            item
            for item in package.members
            if item.document_version_id == document_version_id
            and (managed_file_id is None or item.managed_file_id == managed_file_id)
        ),
        None,
    )
    if member is None:
        reasons.append("PURPOSE_MEMBER_NOT_FROZEN")
    elif member.sha256.casefold() != content_sha256.casefold():
        reasons.append("PURPOSE_MEMBER_HASH_MISMATCH")
    category = next(
        (
            item
            for item in flatten_category_paths(taxonomy)
            if item.category_id == package.purpose_category_id
        ),
        None,
    )
    if category is None or not category.primary_enabled:
        reasons.append("PURPOSE_CATEGORY_INVALID")
    if reasons:
        return PurposePolicyResult(False, None, tuple(dict.fromkeys(reasons)))
    assert member is not None and category is not None
    candidate = {
        "name": "/".join(category.path),
        "category_id": category.category_id,
        "category_path": category.path,
        # 用途授权是确定性选择依据，不伪造成内容语义概率 1.0。
        "confidence": 0.98,
        "status": "SUGGESTED",
        "source": "verified_purpose_package",
        "purpose_basis": "VERIFIED_PACKAGE",
        "evidence": [],
        "evidence_items": [
            {
                "type": "purpose_package_manifest",
                "page_number": None,
                "sheet_name": None,
                "quote": package.id,
                "signals": [package.policy_id, package.policy_version],
                "source": "verified_purpose_package",
            }
        ],
        "candidate_scores": {
            "business": 0.0,
            "scope": 0.0,
            "evidence_support": 1.0,
            "purpose_package": True,
        },
        "taxonomy_key": taxonomy.key,
        "taxonomy_version": taxonomy.version,
        "purpose_package_id": package.id,
        "purpose_manifest_digest": package.manifest_digest,
    }
    return PurposePolicyResult(True, candidate, ())
