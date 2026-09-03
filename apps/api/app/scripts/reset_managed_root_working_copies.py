"""按受管根重置工作副本物化结果，同时保留源文件索引和源侧分析事实。"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.db import models  # noqa: F401  # 注册完整 ORM metadata。
from app.db.base import Base
from app.db.models import (
    DocumentCategory,
    DocumentOrganizationDecision,
    FilesystemJob,
    FilesystemJobEvent,
    ManagedFile,
    ManagedFileAnalysisRun,
    ManagedFileRevision,
    ManagedRoot,
    User,
    WorkingCopy,
    WorkingCopyRoot,
    utcnow,
)
from app.modules.classification.freshness import (
    ClassificationFreshness,
    classification_refresh_deduplication_key,
    classification_refresh_priority,
    current_classification_identity,
    inspect_managed_source_classification,
)
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.scripts.reset_development_shared_workspace import (
    ResetTarget,
    clear_directory_contents,
    configured_external_managed_roots,
    validate_reset_targets,
)


def _table(name: str):
    """读取已注册表，缺失时立即失败，避免静默漏清。"""

    try:
        return Base.metadata.tables[name]
    except KeyError as exc:  # pragma: no cover - 仅防止未来模型清单漂移
        raise RuntimeError(f"工作副本重置缺少数据库表：{name}") from exc


def _delete(db: Session, table_name: str, condition, counts: Counter[str]) -> None:
    """执行一条有明确作用域的删除并累计数量。"""

    result = db.execute(_table(table_name).delete().where(condition))
    counts[table_name] += max(0, int(result.rowcount or 0))


def _source_snapshot(db: Session, *, root_id: str) -> dict[str, int]:
    """记录必须完整保留的源侧事实数量。"""

    source_documents = (
        db.query(ManagedFileRevision.analysis_document_id)
        .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
        .filter(ManagedFile.root_id == root_id)
        .filter(ManagedFileRevision.analysis_document_id.isnot(None))
        .subquery()
    )
    source_versions = (
        db.query(ManagedFileRevision.analysis_document_version_id)
        .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
        .filter(ManagedFile.root_id == root_id)
        .filter(ManagedFileRevision.analysis_document_version_id.isnot(None))
        .subquery()
    )
    return {
        "managed_files": db.query(ManagedFile).filter(ManagedFile.root_id == root_id).count(),
        "managed_file_revisions": (
            db.query(ManagedFileRevision)
            .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
            .filter(ManagedFile.root_id == root_id)
            .count()
        ),
        "managed_file_analysis_runs": (
            db.query(ManagedFileAnalysisRun)
            .join(
                ManagedFileRevision,
                ManagedFileRevision.id == ManagedFileAnalysisRun.managed_file_revision_id,
            )
            .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
            .filter(ManagedFile.root_id == root_id)
            .count()
        ),
        "source_documents": db.query(ManagedFileRevision)
        .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
        .filter(ManagedFile.root_id == root_id)
        .filter(ManagedFileRevision.analysis_document_id.isnot(None))
        .count(),
        "source_versions": db.query(ManagedFileRevision)
        .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
        .filter(ManagedFile.root_id == root_id)
        .filter(ManagedFileRevision.analysis_document_version_id.isnot(None))
        .count(),
        "source_pages": int(
            db.execute(
                select(func.count(_table("document_pages").c.id)).where(
                    _table("document_pages").c.document_id.in_(
                        select(source_documents.c.analysis_document_id)
                    )
                )
            ).scalar_one()
        ),
        "source_classification_runs": int(
            db.execute(
                select(func.count(_table("document_classification_runs").c.id)).where(
                    _table("document_classification_runs").c.document_id.in_(
                        select(source_documents.c.analysis_document_id)
                    )
                )
            ).scalar_one()
        ),
        "source_version_rows": int(
            db.execute(
                select(func.count(_table("document_versions").c.id)).where(
                    _table("document_versions").c.id.in_(
                        select(source_versions.c.analysis_document_version_id)
                    )
                )
            ).scalar_one()
        ),
    }


def _resolve_target(
    *,
    settings: Settings,
    project_root: Path,
    managed_root: ManagedRoot,
    working_root: WorkingCopyRoot,
) -> ResetTarget:
    """把数据库相对路径解析为工作副本存储内的唯一安全目标。"""

    configured_key = f"MANAGED_ROOT_{managed_root.root_key.upper()}"
    configured_source = os.getenv(configured_key, "").strip()
    if not configured_source:
        raise ValueError(f"缺少 {configured_key}，拒绝猜测外部原目录")
    if Path(configured_source).expanduser().resolve() != Path(
        managed_root.container_path
    ).expanduser().resolve():
        raise ValueError("数据库受管根路径与当前环境配置不一致")

    storage_root = Path(settings.working_copy_storage_root).expanduser().resolve()
    target_path = (storage_root / working_root.relative_storage_path).resolve()
    if target_path == storage_root:
        raise ValueError("工作副本测试目标不能是整个 WORKING_COPY_STORAGE_ROOT")
    try:
        target_path.relative_to(storage_root)
    except ValueError as exc:
        raise ValueError("工作副本测试目标越过配置存储根") from exc

    target = ResetTarget(f"工作副本根 {managed_root.root_key}", target_path)
    validate_reset_targets(
        [target],
        project_root=project_root,
        protected_roots=configured_external_managed_roots(),
    )
    return target


def reset_working_copy_materializations(
    *,
    db: Session,
    settings: Settings,
    project_root: Path,
    root_key: str,
) -> dict[str, object]:
    """删除指定受管根的工作副本事实和文件，并重新排队物化任务。"""

    managed_root = db.query(ManagedRoot).filter(ManagedRoot.root_key == root_key).one()
    working_roots = (
        db.query(WorkingCopyRoot)
        .filter(WorkingCopyRoot.managed_root_id == managed_root.id)
        .all()
    )
    if len(working_roots) != 1:
        raise ValueError(f"预期唯一工作副本根，实际为 {len(working_roots)} 个")
    working_root = working_roots[0]
    target = _resolve_target(
        settings=settings,
        project_root=project_root,
        managed_root=managed_root,
        working_root=working_root,
    )

    copies = (
        db.query(WorkingCopy)
        .filter(WorkingCopy.working_copy_root_id == working_root.id)
        .all()
    )
    copy_ids = [row.id for row in copies]
    document_ids = [row.document_id for row in copies]
    version_ids = [row.current_version_id for row in copies if row.current_version_id]
    revisions = (
        db.query(ManagedFileRevision)
        .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
        .filter(
            ManagedFile.root_id == managed_root.id,
            ManagedFile.status == "ACTIVE",
        )
        .all()
    )
    revision_ids = [row.id for row in revisions]
    source_document_ids = {
        row[0]
        for row in (
            db.query(ManagedFileRevision.analysis_document_id)
            .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
            .filter(ManagedFile.root_id == managed_root.id)
            .filter(ManagedFileRevision.analysis_document_id.isnot(None))
            .all()
        )
    }
    source_version_ids = {
        row[0]
        for row in (
            db.query(ManagedFileRevision.analysis_document_version_id)
            .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
            .filter(ManagedFile.root_id == managed_root.id)
            .filter(ManagedFileRevision.analysis_document_version_id.isnot(None))
            .all()
        )
    }
    if source_document_ids.intersection(document_ids):
        raise RuntimeError("工作副本 Document 与源分析 Document 重叠，拒绝清理")
    if source_version_ids.intersection(version_ids):
        raise RuntimeError("工作副本 Version 与源分析 Version 重叠，拒绝清理")

    before_source = _source_snapshot(db, root_id=managed_root.id)
    counts: Counter[str] = Counter()

    if copy_ids:
        categories = _table("document_categories")
        category_ids = list(
            db.execute(
                select(categories.c.id).where(categories.c.working_copy_id.in_(copy_ids))
            ).scalars()
        )
        if category_ids:
            confirmations = _table("document_category_confirmation_sources")
            _delete(
                db,
                "document_category_confirmation_sources",
                confirmations.c.document_category_id.in_(category_ids),
                counts,
            )

        graph_outbox = _table("classification_graph_outbox")
        _delete(
            db,
            "classification_graph_outbox",
            or_(
                graph_outbox.c.working_copy_id.in_(copy_ids),
                graph_outbox.c.document_version_id.in_(version_ids),
            ),
            counts,
        )
        _delete(db, "document_categories", categories.c.working_copy_id.in_(copy_ids), counts)

        for table_name in (
            "answer_references",
            "trash_entries",
            "working_copy_path_records",
            "document_organization_decisions",
        ):
            table = _table(table_name)
            conditions = []
            if "working_copy_id" in table.c:
                conditions.append(table.c.working_copy_id.in_(copy_ids))
            if "document_version_id" in table.c:
                conditions.append(table.c.document_version_id.in_(version_ids))
            if "document_id" in table.c:
                conditions.append(table.c.document_id.in_(document_ids))
            _delete(db, table_name, or_(*conditions), counts)

        search_profiles = _table("document_search_profiles")
        _delete(
            db,
            "document_search_profiles",
            search_profiles.c.working_copy_id.in_(copy_ids),
            counts,
        )
        relevant_items = _table("relevant_file_set_items")
        _delete(
            db,
            "relevant_file_set_items",
            relevant_items.c.working_copy_id.in_(copy_ids),
            counts,
        )

        change_items = _table("change_items")
        scoped_change_condition = or_(
            change_items.c.target_document_id.in_(document_ids),
            change_items.c.target_id.in_(copy_ids),
        )
        change_set_ids = list(
            db.execute(
                select(change_items.c.changeset_id)
                .where(scoped_change_condition)
                .distinct()
            ).scalars()
        )
        _delete(db, "change_items", scoped_change_condition, counts)
        if change_set_ids:
            remaining_items = _table("change_items")
            empty_change_set_ids = [
                change_set_id
                for change_set_id in change_set_ids
                if db.execute(
                    select(remaining_items.c.id)
                    .where(remaining_items.c.changeset_id == change_set_id)
                    .limit(1)
                ).first()
                is None
            ]
            if empty_change_set_ids:
                agent_runs = _table("agent_runs")
                db.execute(
                    agent_runs.update()
                    .where(agent_runs.c.changeset_id.in_(empty_change_set_ids))
                    .values(changeset_id=None)
                )
                change_sets = _table("change_sets")
                _delete(
                    db,
                    "change_sets",
                    change_sets.c.id.in_(empty_change_set_ids),
                    counts,
                )

        db.query(WorkingCopy).filter(WorkingCopy.id.in_(copy_ids)).update(
            {WorkingCopy.current_version_id: None}, synchronize_session=False
        )
        counts["working_copies"] += db.query(WorkingCopy).filter(
            WorkingCopy.id.in_(copy_ids)
        ).delete(synchronize_session=False)

        # 删除工作副本 Document 的全部派生事实。源分析使用不同 Document/Version，
        # 已在上方做集合不相交校验，因此不会进入本作用域。
        document_scoped_tables = (
            "document_category_feedback",
            "structured_extraction_runs",
            "managed_file_snapshots",
            "document_chunks",
            "evidence_spans",
            "document_index_runs",
            "document_elements",
            "document_pages",
            "document_artifacts",
            "document_insights",
            "document_summaries",
            "document_classification_summaries",
            "document_category_suggestions",
            "document_classification_runs",
            "document_extraction_runs",
            "file_objects",
        )
        for table_name in document_scoped_tables:
            table = _table(table_name)
            conditions = []
            if "document_id" in table.c:
                conditions.append(table.c.document_id.in_(document_ids))
            if "document_version_id" in table.c:
                conditions.append(table.c.document_version_id.in_(version_ids))
            if conditions:
                _delete(db, table_name, or_(*conditions), counts)

        document_versions = _table("document_versions")
        _delete(
            db,
            "document_versions",
            or_(
                document_versions.c.id.in_(version_ids),
                document_versions.c.document_id.in_(document_ids),
            ),
            counts,
        )
        documents = _table("documents")
        _delete(db, "documents", documents.c.id.in_(document_ids), counts)

    # 重置时按当前动态分类身份分流，不能把旧 taxonomy 结果直接重新物化。
    classification_user_id = str(
        managed_root.created_by
        or db.query(User.id).order_by(User.created_at.asc()).scalar()
        or ""
    )
    if not classification_user_id:
        raise RuntimeError("工作副本重置缺少可审计用户，无法判断当前分类身份")
    classification_identity = current_classification_identity(
        db=db,
        settings=settings,
        user_id=classification_user_id,
    )
    freshness_by_revision: dict[str, ClassificationFreshness] = {}
    for revision in revisions:
        if not revision.is_current or revision.status != "READY":
            continue
        freshness_by_revision[revision.id] = inspect_managed_source_classification(
            db=db,
            revision=revision,
            identity=classification_identity,
        )

    jobs = db.query(FilesystemJob).filter(
        FilesystemJob.job_type.in_(
            {
                "MATERIALIZE_WORKING_COPY",
                "ANALYZE_MANAGED_FILE_REVISION",
                "REFRESH_MANAGED_SOURCE_CLASSIFICATION",
            }
        )
    ).all()
    revision_id_set = set(revision_ids)
    requeued_jobs: Counter[str] = Counter()
    for job in jobs:
        revision_id = str((job.payload_json or {}).get("managed_file_revision_id") or "")
        if revision_id not in revision_id_set:
            continue
        freshness = freshness_by_revision.get(revision_id)
        should_requeue = (
            (
                job.job_type == "MATERIALIZE_WORKING_COPY"
                and freshness is ClassificationFreshness.CURRENT
            )
            or (
                job.job_type
                in {
                    "ANALYZE_MANAGED_FILE_REVISION",
                    "REFRESH_MANAGED_SOURCE_CLASSIFICATION",
                }
                and job.status == "RUNNING"
            )
        )
        if (
            job.job_type == "MATERIALIZE_WORKING_COPY"
            and freshness is not ClassificationFreshness.CURRENT
        ):
            # 旧物化任务先变成可重开的完成态；刷新成功后 worker 会按原幂等键重新激活。
            job.status = "COMPLETED"
            job.result_json = {
                "status": "DEFERRED_CLASSIFICATION_REFRESH",
                "managed_file_revision_id": revision_id,
            }
            job.error_message = None
            job.finished_at = utcnow()
            job.updated_at = utcnow()
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.locked_by = None
            job.locked_at = None
            continue
        if not should_requeue:
            continue
        job.status = "PENDING"
        job.progress_current = 0
        job.progress_total = 0
        job.result_json = {}
        job.error_message = None
        job.attempt_count = 0
        job.available_at = utcnow()
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.locked_by = None
        job.locked_at = None
        job.started_at = None
        job.finished_at = None
        job.updated_at = utcnow()
        db.add(
            FilesystemJobEvent(
                job_id=job.id,
                level="WARNING",
                message=f"测试根 {root_key} 工作副本物化重置后重新入队",
                details_json={"root_key": root_key, "reset_scope": "working_copy_materialization"},
            )
        )
        requeued_jobs[job.job_type] += 1

    queue = FilesystemJobQueue(db)
    refresh_job_ids: list[str] = []
    for revision in revisions:
        freshness = freshness_by_revision.get(revision.id)
        if freshness in {None, ClassificationFreshness.CURRENT}:
            continue
        refresh_job = queue.create_job(
            job_type="REFRESH_MANAGED_SOURCE_CLASSIFICATION",
            queue_name="SOURCE_ANALYSIS",
            root_id=managed_root.id,
            created_by=classification_user_id,
            priority=classification_refresh_priority(settings),
            deduplication_key=classification_refresh_deduplication_key(
                revision_id=revision.id,
                identity=classification_identity,
            ),
            reuse_completed=True,
            retry_failed=True,
            payload={
                "managed_file_revision_id": revision.id,
                "user_id": classification_user_id,
                "taxonomy_key": classification_identity.taxonomy_key,
                "taxonomy_version": classification_identity.taxonomy_version,
                "classifier_version": classification_identity.classifier_version,
            },
        )
        refresh_job = queue.promote_pending_job(
            job=refresh_job,
            priority=classification_refresh_priority(settings),
        )
        refresh_job_ids.append(str(refresh_job.id))
        requeued_jobs["REFRESH_MANAGED_SOURCE_CLASSIFICATION"] += 1

    working_root.last_imported_at = None
    working_root.status = "READY"
    db.flush()

    after_source = _source_snapshot(db, root_id=managed_root.id)
    if after_source != before_source:
        raise RuntimeError(
            f"源侧事实发生变化，拒绝提交：before={before_source}, after={after_source}"
        )
    if db.query(WorkingCopy).filter(
        WorkingCopy.working_copy_root_id == working_root.id
    ).count():
        raise RuntimeError("测试根仍存在工作副本，拒绝提交")
    if db.query(DocumentOrganizationDecision).filter(
        DocumentOrganizationDecision.working_copy_id.in_(copy_ids)
    ).count():
        raise RuntimeError("测试根仍存在组织决策，拒绝提交")
    if db.query(DocumentCategory).filter(
        DocumentCategory.working_copy_id.in_(copy_ids)
    ).count():
        raise RuntimeError("测试根仍存在正式分类，拒绝提交")

    physical_file_count = len(
        [path for path in target.path.rglob("*") if path.is_file()]
    ) if target.path.exists() else 0
    clear_directory_contents(target)
    db.commit()
    return {
        "root_key": root_key,
        "source_path": str(Path(managed_root.container_path).resolve()),
        "working_copy_path": str(target.path),
        "working_copies_removed": len(copy_ids),
        "physical_files_removed": physical_file_count,
        "database_rows_removed": dict(sorted(counts.items())),
        "jobs_requeued": dict(sorted(requeued_jobs.items())),
        "classification_identity": {
            "taxonomy_key": classification_identity.taxonomy_key,
            "taxonomy_version": classification_identity.taxonomy_version,
            "classifier_version": classification_identity.classifier_version,
            "fingerprint": classification_identity.fingerprint,
        },
        "classification_freshness": dict(
            sorted(Counter(item.value for item in freshness_by_revision.values()).items())
        ),
        "classification_refresh_job_ids": refresh_job_ids,
        "source_preserved": before_source,
    }


def main() -> None:
    """执行带双重确认和精确 root key 的受控重置。"""

    parser = argparse.ArgumentParser(
        description="仅重置指定受管根的工作副本物化数据，保留源扫描和源分析"
    )
    parser.add_argument("--root-key", required=True)
    parser.add_argument("--confirm-reset-working-copies", action="store_true")
    parser.add_argument("--confirm-writers-stopped", action="store_true")
    args = parser.parse_args()
    if not args.confirm_reset_working_copies:
        parser.error("必须提供 --confirm-reset-working-copies")
    if not args.confirm_writers_stopped:
        parser.error("必须先停止 API、scheduler、watcher 和全部 worker")

    settings = get_settings()
    with SessionLocal() as db:
        try:
            result = reset_working_copy_materializations(
                db=db,
                settings=settings,
                project_root=Path.cwd().resolve(),
                root_key=args.root_key,
            )
        except Exception:
            db.rollback()
            raise
    print(result)


if __name__ == "__main__":
    main()
