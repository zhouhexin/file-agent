"""主分类树和待复核文件清单的回归测试。"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentCategory,
    DocumentOrganizationDecision,
    DocumentVersion,
    ManagedFile,
    ManagedRoot,
    User,
    WorkingCopy,
    WorkingCopyRoot,
)
from app.modules.classification.organization_query_service import (
    ClassificationOrganizationQueryService,
    NEEDS_REVIEW_NODE_ID,
)
from app.modules.classification.image_date_policy import image_date_virtual_node_id
from app.modules.classification.organization_schemas import OrganizationTreeNodeResponse
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


def _session():
    """创建包含全部外键表的隔离 SQLite 会话。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _find_node(
    nodes: list[OrganizationTreeNodeResponse],
    category_id: str,
) -> OrganizationTreeNodeResponse:
    """在响应树中按稳定分类 ID 定位节点。"""

    for node in nodes:
        if node.category_id == category_id:
            return node
        try:
            return _find_node(node.children, category_id)
        except LookupError:
            continue
    raise LookupError(category_id)


def _seed_copy(
    db,
    *,
    index: int,
    root: WorkingCopyRoot,
    user: User,
    status: str = "ACTIVE",
) -> WorkingCopy:
    """创建具有当前版本的共享工作副本。"""

    document_id = f"organization-document-{index}"
    version_id = f"organization-version-{index}"
    managed_file_id = f"organization-managed-file-{index}"
    filename = f"{index:02d}-测试文件.docx"
    document = Document(
        id=document_id,
        user_id=user.id,
        workspace_id=root.workspace_id,
        original_filename=filename,
        size_bytes=100 + index,
        sha256=f"{index:064x}",
    )
    managed_file = ManagedFile(
        id=managed_file_id,
        root_id=root.managed_root_id,
        relative_path=filename,
        relative_path_hash=f"{index + 100:064x}",
        filename=filename,
        extension=".docx",
        size_bytes=100 + index,
        content_sha256=f"{index:064x}",
        status="ACTIVE",
    )
    working_copy = WorkingCopy(
        id=f"organization-working-copy-{index}",
        working_copy_root_id=root.id,
        workspace_id=root.workspace_id,
        managed_file_id=managed_file.id,
        document_id=document.id,
        current_version_id=version_id,
        relative_path=filename,
        relative_path_hash=f"{index + 200:064x}",
        filename=filename,
        extension=".docx",
        size_bytes=100 + index,
        content_sha256=document.sha256,
        imported_source_sha256=document.sha256,
        status=status,
    )
    version = DocumentVersion(
        id=version_id,
        document_id=document.id,
        working_copy_id=working_copy.id,
        filename=filename,
        storage_path=f"organization/{filename}",
        size_bytes=document.size_bytes,
        sha256=document.sha256,
    )
    db.add_all([document, managed_file, working_copy, version])
    return working_copy


def _add_primary(
    db,
    working_copy: WorkingCopy,
    *,
    category_id: str,
    category_path: list[str],
    status: str,
    source: str = "test",
) -> None:
    """为当前版本建立一条活动主分类关系。"""

    db.add(
        DocumentCategory(
            working_copy_id=working_copy.id,
            document_id=working_copy.document_id,
            document_version_id=working_copy.current_version_id,
            category_id=category_id,
            category_path_json=category_path,
            relation_role="PRIMARY",
            status=status,
            taxonomy_key="unified_school_file_classification",
            taxonomy_version="2026-08-v6",
            classifier_version="test",
            source=source,
        )
    )


def _add_review_decision(
    db,
    working_copy: WorkingCopy,
    *,
    shadow_only: bool,
    suffix: str,
) -> None:
    """创建实际或 Shadow 的待复核决策。"""

    db.add(
        DocumentOrganizationDecision(
            id=f"organization-decision-{suffix}",
            working_copy_id=working_copy.id,
            document_id=working_copy.document_id,
            document_version_id=working_copy.current_version_id,
            policy_version="auto-placement-v1",
            decision="NEEDS_REVIEW",
            feature_snapshot_json={"shadow_only": shadow_only},
            reason_codes_json=["LOW_CONFIDENCE"],
            idempotency_key=f"organization-decision-key-{suffix}",
        )
    )


def _seed_organization_data(db):
    """准备活动分类、未分类、待复核、Shadow 和未发布文件。"""

    user = User(id="organization-user", username="organization-user")
    workspace_id = get_shared_workspace_id(db)
    managed_root = ManagedRoot(
        id="organization-managed-root",
        root_key="organization-root",
        display_name="分类目录测试",
        container_path="/managed/organization",
        enabled=True,
    )
    root = WorkingCopyRoot(
        id="organization-working-root",
        workspace_id=workspace_id,
        managed_root_id=managed_root.id,
        root_key="organization-root",
        relative_storage_path="organization",
        status="ACTIVE",
    )
    db.add_all([user, managed_root, root])
    db.flush()

    annual = _seed_copy(db, index=1, root=root, user=user)
    planning = _seed_copy(db, index=2, root=root, user=user)
    unclassified = _seed_copy(db, index=3, root=root, user=user)
    review = _seed_copy(db, index=4, root=root, user=user)
    shadow = _seed_copy(db, index=5, root=root, user=user)
    organizing = _seed_copy(db, index=6, root=root, user=user, status="ORGANIZING")
    confirmed_review = _seed_copy(db, index=7, root=root, user=user)
    _add_primary(
        db,
        annual,
        category_id="school.admin.annual-plan-summary",
        category_path=["学校", "行政综合管理类", "年度计划、总结"],
        status="AUTO_APPLIED",
    )
    _add_primary(
        db,
        planning,
        category_id="school.admin.development-planning",
        category_path=["学校", "行政综合管理类", "发展规划"],
        status="CONFIRMED",
    )
    _add_review_decision(db, review, shadow_only=False, suffix="review")
    _add_review_decision(db, shadow, shadow_only=True, suffix="shadow")
    _add_review_decision(db, organizing, shadow_only=False, suffix="organizing")
    _add_review_decision(db, confirmed_review, shadow_only=False, suffix="confirmed-review")
    _add_primary(
        db,
        confirmed_review,
        category_id="school.admin.annual-plan-summary",
        category_path=["学校", "行政综合管理类", "年度计划、总结"],
        status="CONFIRMED",
    )
    db.commit()
    return annual, planning, unclassified, review, shadow, organizing, confirmed_review


def test_tree_uses_schema_v2_other_aggregation_without_review_node():
    """分类依据不足的活动文件归 OTHER，不再公开待复核节点或计数。"""

    db = _session()
    _seed_organization_data(db)

    result = ClassificationOrganizationQueryService(db).tree()

    assert result.total_active_files == 6
    assert result.classified_file_count == 3
    assert result.schema_version == 2
    assert result.business_classified_file_count == 3
    assert result.other_file_count == 3
    assert result.needs_review_file_count == 0
    assert all(node.category_id != NEEDS_REVIEW_NODE_ID for node in result.nodes)
    assert _find_node(result.nodes, "school").subtree_file_count == 3
    assert _find_node(result.nodes, "school.admin").subtree_file_count == 3
    assert _find_node(result.nodes, "school.admin.annual-plan-summary").direct_file_count == 2
    assert _find_node(result.nodes, "system.other").direct_file_count == 3


def test_files_support_direct_descendant_other_compatibility_and_stable_pagination():
    """旧复核链接规范到 OTHER，且分页不会重复或泄漏 ORGANIZING 文件。"""

    db = _session()
    annual, planning, unclassified, review, shadow, _, confirmed_review = _seed_organization_data(db)
    service = ClassificationOrganizationQueryService(db)

    descendants = service.files(
        category_id="school.admin",
        scope="descendants",
        review_only=False,
        page=1,
        page_size=20,
    )
    direct = service.files(
        category_id="school.admin",
        scope="direct",
        review_only=False,
        page=1,
        page_size=20,
    )
    review_page = service.files(
        category_id=NEEDS_REVIEW_NODE_ID,
        scope="descendants",
        review_only=False,
        page=1,
        page_size=20,
    )
    first = service.files(
        category_id=None,
        scope="descendants",
        review_only=False,
        page=1,
        page_size=2,
    )
    second = service.files(
        category_id=None,
        scope="descendants",
        review_only=False,
        page=2,
        page_size=2,
    )

    assert descendants.total == 3
    assert {item.working_copy_id for item in descendants.files} == {
        annual.id,
        planning.id,
        confirmed_review.id,
    }
    assert direct.total == 0
    assert review_page.category_id == "system.other"
    assert review_page.review_only is False
    assert review_page.deprecated_compatibility is True
    assert review_page.total == 3
    assert {item.working_copy_id for item in review_page.files} == {
        unclassified.id,
        review.id,
        shadow.id,
    }
    assert all(item.classification_outcome == "OTHER" for item in review_page.files)
    assert all(item.effective_primary is None for item in review_page.files)
    assert first.total == 6
    assert first.total_pages == 3
    assert not ({item.working_copy_id for item in first.files} & {item.working_copy_id for item in second.files})


def test_other_aggregation_keeps_historical_primary_and_location_truthful():
    """历史 fallback 可在 OTHER 找到，但文件项不把它伪造成新的 system.other。"""

    db = _session()
    _seed_organization_data(db)
    root = db.query(WorkingCopyRoot).one()
    user = db.query(User).one()
    legacy = _seed_copy(db, index=9, root=root, user=user)
    current_other = _seed_copy(db, index=10, root=root, user=user)
    _add_primary(
        db,
        legacy,
        category_id="school.admin.other",
        category_path=["学校", "行政综合管理类", "其他"],
        status="CONFIRMED",
    )
    _add_primary(
        db,
        current_other,
        category_id="system.other",
        category_path=["其他"],
        status="AUTO_APPLIED",
    )
    db.commit()

    service = ClassificationOrganizationQueryService(db)
    tree = service.tree()
    page = service.files(
        category_id="system.other",
        scope="descendants",
        review_only=False,
        page=1,
        page_size=20,
    )
    by_copy = {item.working_copy_id: item for item in page.files}

    assert tree.other_file_count == 5
    assert _find_node(tree.nodes, "system.other").direct_file_count == 5
    assert by_copy[legacy.id].classification_outcome == "OTHER"
    assert by_copy[legacy.id].legacy_location is True
    assert by_copy[legacy.id].effective_primary.category_id == "school.admin.other"
    assert by_copy[current_other.id].legacy_location is False
    assert by_copy[current_other.id].effective_primary.category_id == "system.other"


def test_image_upload_years_are_virtual_children_of_college_category():
    """图片上传年份只投影为学院虚拟子节点，不要求把动态年份写入 taxonomy。"""

    db = _session()
    _seed_organization_data(db)
    root = db.query(WorkingCopyRoot).one()
    user = db.query(User).one()
    image = _seed_copy(db, index=8, root=root, user=user)
    image.relative_path = "学院/2026/现场照片.png"
    image.filename = "现场照片.png"
    image.extension = ".png"
    _add_primary(
        db,
        image,
        category_id="college",
        category_path=["学院", "2026"],
        status="AUTO_APPLIED",
        source="image_upload_date_policy",
    )
    db.commit()

    service = ClassificationOrganizationQueryService(db)
    virtual_id = image_date_virtual_node_id("2026")
    tree = service.tree()
    date_node = _find_node(tree.nodes, virtual_id)

    assert date_node.is_virtual is True
    assert date_node.category_path == ["学院", "2026"]
    assert date_node.direct_file_count == 1
    assert _find_node(tree.nodes, "college").subtree_file_count == 1

    page = service.files(
        category_id=virtual_id,
        scope="descendants",
        review_only=False,
        page=1,
        page_size=20,
    )
    assert page.total == 1
    assert page.files[0].working_copy_id == image.id
    assert page.files[0].primary_category_path == ["学院", "2026"]
