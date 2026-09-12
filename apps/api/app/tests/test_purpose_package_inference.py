"""受管目录材料包用途识别的生产接线测试。"""

import hashlib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentExtractionRun,
    DocumentPage,
    DocumentVersion,
    ManagedFile,
    ManagedFileRevision,
    ManagedRoot,
    User,
    Workspace,
)
from app.modules.classification.purpose_inference import (
    ManagedPurposePackageInferenceService,
)


def test_nested_material_package_inherits_unique_structured_purpose():
    """具体材料根存在唯一强表单锚点时，嵌套附件可继承并复用冻结用途。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        user = User(id="purpose-user", username="purpose-user")
        workspace = Workspace(id="purpose-workspace", name="材料包测试")
        root = ManagedRoot(
            id="purpose-root",
            root_key="workdata",
            display_name="测试受管目录",
            container_path="/managed/workdata",
        )
        db.add_all([user, workspace, root])
        db.flush()

        anchor = _add_ready_member(
            db,
            user_id=user.id,
            workspace_id=workspace.id,
            root_id=root.id,
            suffix="anchor",
            relative_path=(
                "人才材料/张三/陕西省高层次人才特殊支持计划"
                "科技创新领军人才申报书.docx"
            ),
            text=(
                "陕西省高层次人才特殊支持计划科技创新领军人才申报书\n"
                "姓名：张三\n申报类别：领军人才\n"
                "所在单位：计算机科学与工程学院"
            ),
        )
        attachment = _add_ready_member(
            db,
            user_id=user.id,
            workspace_id=workspace.id,
            root_id=root.id,
            suffix="attachment",
            relative_path="人才材料/张三/附件/身份证明.pdf",
            # 模拟 OCR 失败但已安全归档的图片/扫描附件；用途只能继承包锚点。
            text="",
        )
        db.flush()

        service = ManagedPurposePackageInferenceService(db)
        resolution = service.resolve(
            managed_file=attachment[0],
            current_revision=attachment[1],
            workspace_id=workspace.id,
            root_key=root.root_key,
            current_document_version_id=attachment[2].id,
            current_sha256=attachment[2].sha256,
        )

        assert resolution.created is True
        assert resolution.snapshot is not None
        assert resolution.snapshot.purpose_category_id == "college.hr.talent-work"
        assert {item.managed_file_id for item in resolution.snapshot.members} == {
            anchor[0].id,
            attachment[0].id,
        }

        reused = service.resolve(
            managed_file=anchor[0],
            current_revision=anchor[1],
            workspace_id=workspace.id,
            root_key=root.root_key,
            current_document_version_id=anchor[2].id,
            current_sha256=anchor[2].sha256,
        )
        assert reused.created is False
        assert reused.snapshot is not None
        assert reused.snapshot.id == resolution.snapshot.id
    finally:
        db.rollback()
        db.close()


def _add_ready_member(
    db,
    *,
    user_id: str,
    workspace_id: str,
    root_id: str,
    suffix: str,
    relative_path: str,
    text: str,
) -> tuple[ManagedFile, ManagedFileRevision, DocumentVersion]:
    """建立一个带持久化正文页的 READY 受管文件成员。"""

    filename = relative_path.rsplit("/", 1)[-1]
    sha256 = hashlib.sha256(f"content-{suffix}".encode("utf-8")).hexdigest()
    managed_file = ManagedFile(
        id=f"managed-{suffix}",
        root_id=root_id,
        relative_path=relative_path,
        relative_path_hash=hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
        filename=filename,
        extension="." + filename.rsplit(".", 1)[-1],
        size_bytes=len(text.encode("utf-8")),
        fingerprint=f"fingerprint-{suffix}",
        content_sha256=sha256,
    )
    document = Document(
        id=f"document-{suffix}",
        user_id=user_id,
        workspace_id=workspace_id,
        original_filename=filename,
        size_bytes=managed_file.size_bytes,
        sha256=sha256,
        status="READY",
        ingest_status="READY",
    )
    version = DocumentVersion(
        id=f"version-{suffix}",
        document_id=document.id,
        storage_path=f"source-analysis/{suffix}",
        filename=filename,
        size_bytes=managed_file.size_bytes,
        sha256=sha256,
        source_type="MANAGED_SOURCE_ANALYSIS",
    )
    extraction = DocumentExtractionRun(
        id=f"extraction-{suffix}",
        document_id=document.id,
        document_version_id=version.id,
        status="COMPLETED",
    )
    page = DocumentPage(
        id=f"page-{suffix}",
        document_id=document.id,
        extraction_run_id=extraction.id,
        page_number=1,
        text_content=text,
    )
    revision = ManagedFileRevision(
        id=f"revision-{suffix}",
        managed_file_id=managed_file.id,
        revision_number=1,
        size_bytes=managed_file.size_bytes,
        quick_fingerprint=managed_file.fingerprint,
        content_sha256=sha256,
        status="READY",
        analysis_status="READY",
        is_current=True,
        analysis_document_id=document.id,
        analysis_document_version_id=version.id,
    )
    db.add_all([managed_file, document, version, extraction, page, revision])
    return managed_file, revision, version
