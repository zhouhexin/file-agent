"""文档版本的内容提取责任策略。

WorkBuddy 文档通道把正文读取责任固定在宿主。这里集中识别该不可回退策略，
使后续索引、分类和人工重处理都不会因缓存指纹不同而重新调用服务器解析器。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import DocumentExtractionRun, DocumentVersion


WORKBUDDY_DOCUMENT_MODE = "WORKBUDDY_DOCUMENT"
WORKBUDDY_DOCUMENT_EXTRACTOR = "workbuddy-external-document"


class WorkBuddyContentRequiredError(RuntimeError):
    """受保护版本缺少宿主正文时阻止服务器读取原件。"""

    code = "WORKBUDDY_CONTENT_REQUIRED"


def requires_workbuddy_document_extraction(version: DocumentVersion | None) -> bool:
    """判断版本是否只能使用已验收的 WorkBuddy 全文结果。"""

    return bool(
        version is not None
        and str((version.extraction_policy_json or {}).get("mode") or "")
        == WORKBUDDY_DOCUMENT_MODE
    )


def latest_workbuddy_document_run(
    *,
    db: Session,
    document_id: str,
    document_version_id: str,
) -> DocumentExtractionRun | None:
    """读取指定内容版本的宿主完整提取运行，不接受其他解析器冒充来源。"""

    return (
        db.query(DocumentExtractionRun)
        .filter(
            DocumentExtractionRun.document_id == document_id,
            DocumentExtractionRun.document_version_id == document_version_id,
            DocumentExtractionRun.status == "COMPLETED",
            DocumentExtractionRun.extractor == WORKBUDDY_DOCUMENT_EXTRACTOR,
        )
        .order_by(DocumentExtractionRun.updated_at.desc())
        .first()
    )


def require_workbuddy_document_run(
    *, db: Session, document_id: str, version: DocumentVersion
) -> DocumentExtractionRun:
    """返回同版本同哈希的完整宿主运行；缺失时给出稳定受控错误。"""

    run = latest_workbuddy_document_run(
        db=db, document_id=document_id, document_version_id=version.id
    )
    if run is None or str((version.extraction_policy_json or {}).get("source_sha256") or "") != version.sha256:
        raise WorkBuddyContentRequiredError(
            "该版本必须先完成 WorkBuddy 文档提取，服务器不会读取原件补足正文。"
        )
    return run
