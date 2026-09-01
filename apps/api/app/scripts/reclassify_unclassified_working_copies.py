"""按当前 taxonomy 重新分类共享工作区中尚无正式主分类的文件。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from uuid import uuid4

from sqlalchemy import and_, exists
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.models import (
    AgentRun,
    Conversation,
    DocumentCategory,
    DocumentExtractionRun,
    DocumentPage,
    Message,
    User,
    WorkingCopy,
    WorkingCopyRoot,
    utcnow,
)
from app.modules.changesets.repository import ChangeSetRepository
from app.modules.changesets.service import persist_changeset_from_document_results
from app.modules.classification.auto_placement_policy import AutoPlacementPolicy
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.classification.organization_path import (
    CategoryOrganizationPathError,
    CategoryOrganizationPathResolver,
)
from app.modules.classification.organization_repository import (
    OrganizationDecisionRepository,
)
from app.modules.classification.service import persist_document_results_classifications
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


ACTIVE_PRIMARY_STATUSES = ("AUTO_APPLIED", "CONFIRMED")


def reclassify_unclassified(*, apply: bool, limit: int | None = None) -> dict:
    """预演或执行未分类工作副本的批量分类；不移动、不改名文件。"""

    with SessionLocal() as db:
        copies = _unclassified_query(db)
        if apply:
            copies = copies.with_for_update()
        copies = copies.order_by(WorkingCopy.relative_path.asc()).all()
        if limit is not None:
            copies = copies[:limit]
        if not copies:
            return {
                "mode": "apply" if apply else "dry-run",
                "matched_count": 0,
                "classified_count": 0,
                "needs_review_count": 0,
                "files": [],
            }

        actor = _audit_actor(db)
        run = _create_audit_run(db=db, actor=actor) if apply else None
        service = DocumentClassificationService(db=db, graph_mode="off")
        policy = AutoPlacementPolicy(get_settings())
        document_results: list[dict] = []
        previews: list[dict] = []
        copies_by_document_id = {item.document_id: item for item in copies}

        for working_copy in copies:
            result = _classify_one(
                db=db,
                service=service,
                working_copy=working_copy,
            )
            document_results.append(result)
            decision = policy.evaluate(
                categories=list(result.get("categories") or []),
                extraction_status=str(result.get("extraction_status") or "FAILED"),
                risk_passed=True,
            )
            primary = next(iter(result.get("categories") or []), None)
            previews.append(
                {
                    "document_id": working_copy.document_id,
                    "filename": working_copy.filename,
                    "category_id": primary.get("category_id") if primary else None,
                    "category_path": list(primary.get("category_path") or []) if primary else [],
                    "confidence": float(primary.get("confidence") or 0) if primary else 0,
                    "accepted": decision.accepted,
                    "reason_codes": list(decision.reason_codes),
                    "status": "PREVIEW",
                }
            )

        if not apply:
            db.rollback()
            return _summary(mode="dry-run", previews=previews)

        assert run is not None
        persist_document_results_classifications(
            db=db,
            agent_run_id=run.id,
            document_results=document_results,
        )
        changeset = persist_changeset_from_document_results(
            db=db,
            run=run,
            document_results=document_results,
        )
        organization_repository = OrganizationDecisionRepository(db)
        path_resolver = CategoryOrganizationPathResolver()
        changeset_repository = ChangeSetRepository(db)

        for preview, result in zip(previews, document_results, strict=True):
            working_copy = copies_by_document_id[str(result["document_id"])]
            classification_run, suggestion = organization_repository.latest_classification(
                agent_run_id=run.id,
                document_id=working_copy.document_id,
            )
            policy_result = policy.evaluate(
                categories=list(result.get("categories") or []),
                extraction_status=str(result.get("extraction_status") or "FAILED"),
                risk_passed=True,
            )
            reason_codes = list(policy_result.reason_codes)
            target_relative_path = None
            if classification_run is None or suggestion is None:
                reason_codes.append("CURRENT_RECLASSIFICATION_NOT_FOUND")
            if not reason_codes and classification_run is not None and suggestion is not None:
                working_root = db.get(
                    WorkingCopyRoot,
                    working_copy.working_copy_root_id,
                )
                if working_root is None:
                    reason_codes.append("WORKING_COPY_ROOT_MISSING")
                    target = None
                try:
                    if working_root is not None:
                        target = path_resolver.resolve_category(
                            category_id=suggestion.category_id,
                            taxonomy_key=classification_run.taxonomy_key,
                            taxonomy_version=classification_run.taxonomy_version,
                            working_copy=working_copy,
                            working_root=working_root,
                        )
                        target_relative_path = target.target_relative_path
                except CategoryOrganizationPathError:
                    reason_codes.append("TARGET_PATH_UNAVAILABLE")

            if reason_codes:
                organization_repository.create_or_update_decision(
                    working_copy=working_copy,
                    classification_run=classification_run,
                    primary_suggestion=suggestion,
                    policy_result=policy_result,
                    policy_version=get_settings().auto_classification_policy_version,
                    calibration_version=get_settings().auto_classification_calibration_version,
                    decision="NEEDS_REVIEW",
                    reason_codes=reason_codes,
                    decision_scope=f"batch-reclassification:{run.id}",
                )
                preview.update(status="NEEDS_REVIEW", reason_codes=reason_codes)
                continue

            assert classification_run is not None and suggestion is not None
            relation = organization_repository.create_auto_applied_primary(
                working_copy=working_copy,
                classification_run=classification_run,
                suggestion=suggestion,
            )
            organization_repository.create_or_update_decision(
                working_copy=working_copy,
                classification_run=classification_run,
                primary_suggestion=suggestion,
                policy_result=policy_result,
                policy_version=get_settings().auto_classification_policy_version,
                calibration_version=get_settings().auto_classification_calibration_version,
                decision="AUTO_RECLASSIFIED",
                reason_codes=[],
                target_relative_path=target_relative_path,
                decision_scope=f"batch-reclassification:{run.id}",
            )
            preview["status"] = "CLASSIFIED"
            if changeset is not None:
                changeset_repository.create_item(
                    changeset_id=changeset.id,
                    target_type="DOCUMENT_CATEGORY",
                    target_id=relation.id,
                    target_document_id=relation.document_id,
                    change_type="CATEGORY_ADDED",
                    after_value={
                        "category_id": relation.category_id,
                        "category_path": list(relation.category_path_json or []),
                        "status": relation.status,
                    },
                    source="batch-reclassification-policy",
                    confidence=float(suggestion.confidence or 0),
                    evidence={"source_suggestion_id": suggestion.id},
                )

        summary = _summary(mode="apply", previews=previews)
        run.status = "COMPLETED"
        run.final_response = (
            f"批量重新分类完成：处理 {summary['matched_count']} 个文件，"
            f"形成正式分类 {summary['classified_count']} 个，"
            f"待复核 {summary['needs_review_count']} 个；未移动或改名文件。"
        )
        run.graph_state_json = {
            "status": run.status,
            "intent": "CLASSIFY_FILES",
            "document_results": document_results,
            "final_response": run.final_response,
            "changeset_id": changeset.id if changeset is not None else None,
        }
        run.changeset_id = changeset.id if changeset is not None else None
        run.updated_at = utcnow()
        db.commit()
        summary.update(
            agent_run_id=run.id,
            changeset_id=run.changeset_id,
        )
        return summary


def _unclassified_query(db: Session):
    """返回当前共享工作区没有活动正式主分类的工作副本。"""

    workspace_id = get_shared_workspace_id(db)
    active_primary = exists().where(
        and_(
            DocumentCategory.working_copy_id == WorkingCopy.id,
            DocumentCategory.document_version_id == WorkingCopy.current_version_id,
            DocumentCategory.relation_role == "PRIMARY",
            DocumentCategory.status.in_(ACTIVE_PRIMARY_STATUSES),
        )
    )
    return db.query(WorkingCopy).filter(
        WorkingCopy.workspace_id == workspace_id,
        WorkingCopy.status == "ACTIVE",
        WorkingCopy.current_version_id.is_not(None),
        ~active_primary,
    )


def _classify_one(
    *,
    db: Session,
    service: DocumentClassificationService,
    working_copy: WorkingCopy,
) -> dict:
    """复用已持久化正文强制生成当前分类身份下的新建议。"""

    extraction = (
        db.query(DocumentExtractionRun)
        .filter(
            DocumentExtractionRun.document_id == working_copy.document_id,
            DocumentExtractionRun.status == "COMPLETED",
        )
        .order_by(DocumentExtractionRun.created_at.desc())
        .first()
    )
    if extraction is None:
        return {
            "document_id": working_copy.document_id,
            "document_version_id": str(working_copy.current_version_id or ""),
            "filename": working_copy.filename,
            "extraction_status": "FAILED",
            "categories": [],
            "source": "batch-reclassify-unclassified",
            "errors": [{"code": "EXTRACTION_NOT_FOUND", "message": "没有可复用的正文解析结果。"}],
        }
    classified = service.classify(
        document_id=working_copy.document_id,
        document_version_id=str(working_copy.current_version_id or ""),
        extraction_run_id=extraction.id,
        filename=working_copy.filename,
        force_reprocess=True,
    )
    pages = (
        db.query(DocumentPage)
        .filter(DocumentPage.extraction_run_id == extraction.id)
        .all()
    )
    return {
        **classified,
        "document_id": working_copy.document_id,
        "document_version_id": str(working_copy.current_version_id or ""),
        "workspace_id": working_copy.workspace_id,
        "filename": working_copy.filename,
        "extraction_status": str(classified.get("status") or "FAILED"),
        "extraction_run_id": extraction.id,
        "extractor": extraction.extractor,
        "page_count": len(pages),
        "char_count": sum(len(page.text_content or "") for page in pages),
        "text_reused": True,
        "classification_reused": False,
        "source": "batch-reclassify-unclassified",
        "warnings": list(classified.get("warnings") or []),
        "errors": list(classified.get("errors") or []),
    }


def _audit_actor(db: Session) -> User:
    """选择现有 ops/admin 作为维护任务审计主体，不创建隐藏用户。"""

    actor = (
        db.query(User)
        .filter(User.role.in_(("admin", "ops")))
        .order_by(User.created_at.asc())
        .first()
        or db.query(User).order_by(User.created_at.asc()).first()
    )
    if actor is None:
        raise RuntimeError("批量重新分类缺少可审计用户")
    return actor


def _create_audit_run(*, db: Session, actor: User) -> AgentRun:
    """创建隐藏维护会话和 AgentRun，保证分类建议与 ChangeSet 可追溯。"""

    conversation = Conversation(
        id=str(uuid4()),
        user_id=actor.id,
        workspace_id=get_shared_workspace_id(db),
        title="未分类文件批量重新分类",
    )
    db.add(conversation)
    db.flush()
    message = Message(
        conversation_id=conversation.id,
        user_id=actor.id,
        role="SYSTEM_AUDIT",
        content="按当前规则重新分类共享工作区中的未分类文件；不移动、不改名。",
        attachments_json=[],
    )
    db.add(message)
    db.flush()
    run = AgentRun(
        conversation_id=conversation.id,
        message_id=message.id,
        user_id=actor.id,
        intent="CLASSIFY_FILES",
        status="RUNNING_TOOL",
        selected_skills_json=["document-classification", "change-report"],
        planner_mode="maintenance-script",
        graph_state_json={"status": "RUNNING_TOOL"},
    )
    db.add(run)
    db.flush()
    return run


def _summary(*, mode: str, previews: list[dict]) -> dict:
    """生成不含正文和绝对路径的逐文件执行回执。"""

    status_counts = Counter(item.get("status") for item in previews)
    return {
        "mode": mode,
        "matched_count": len(previews),
        "classified_count": status_counts.get("CLASSIFIED", 0),
        "needs_review_count": status_counts.get("NEEDS_REVIEW", 0),
        "files": previews,
    }


def main() -> None:
    """默认只预演；只有显式传入 ``--apply`` 才提交数据库事务。"""

    parser = argparse.ArgumentParser(description="重新分类当前未分类工作副本")
    parser.add_argument("--apply", action="store_true", help="提交分类建议和正式主分类")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个文件")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")
    print(
        json.dumps(
            reclassify_unclassified(apply=args.apply, limit=args.limit),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
