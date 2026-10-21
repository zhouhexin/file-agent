"""累计 Excel 的数据库优先发布协调器和幂等恢复器。"""

from __future__ import annotations

import os
from fastapi import HTTPException

from app.core.logging import log_event
from app.db.models import (
    AgentRun, ChangeItem, ChangeSet, DocumentVersion, FileObject, FilesystemJob, ManagedFile, StructuredDataset,
    StructuredDatasetExport, StructuredDatasetPublication, StructuredDatasetTask, ToolInvocation,
    WorkingCopy, WorkingCopyPathRecord, utcnow,
)
from app.modules.file_lifecycle.storage import FileLifecycleStorageService


def _sync_directory(path):
    """支持目录 fsync 的平台执行落盘屏障；Windows 不支持时依赖文件 fsync。"""
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def stage_publication(db, *, dataset, working_copy, version, base_version_id=None,
                      task=None, result=None, cleanup=None, archive_target=None, agent_run=None):
    """只登记已落盘的不可变版本；调用方在提交事务前不得改动可见文件。"""
    storage = FileLifecycleStorageService()
    candidate = storage.working_copy_path(version.storage_path)
    if not candidate.is_file() or storage.sha256_file(candidate) != version.sha256:
        raise HTTPException(409, detail={"code": "DATASET_CANDIDATE_MISSING", "message": "Excel 不可变版本缺失或损坏。"})
    with candidate.open("r+b") as handle:
        os.fsync(handle.fileno())
    _sync_directory(candidate.parent)
    # 通过服务层已校验的相对路径构造；绝不接收客户端提供的物理路径。
    from app.db.models import WorkingCopyRoot
    root = db.get(WorkingCopyRoot, working_copy.working_copy_root_id)
    if root is None:
        raise HTTPException(409, detail={"code": "DATASET_WORKING_COPY_INVALID", "message": "Excel 工作副本根不存在。"})
    root_part = root.relative_storage_path.strip("/\\")
    copy_part = working_copy.relative_path.strip("/\\")
    visible = f"{root_part}/{copy_part}"
    existing = db.query(StructuredDatasetPublication).filter(
        StructuredDatasetPublication.dataset_id == dataset.id,
        StructuredDatasetPublication.status.in_(("DB_COMMITTED", "NEEDS_REPAIR")),
    ).with_for_update().first()
    if existing is not None:
        raise HTTPException(423, detail={"code": "DATASET_PUBLICATION_PENDING", "message": "该 Excel 有未完成发布，请先恢复。"})
    row = StructuredDatasetPublication(
        dataset_id=dataset.id, working_copy_id=working_copy.id,
        task_id=task.id if task else None, base_document_version_id=base_version_id,
        candidate_document_version_id=version.id, candidate_sha256=version.sha256,
        candidate_storage_path=version.storage_path, visible_relative_path=visible,
        status="DB_COMMITTED", result_json=result or {},
        cleanup_json=({"legacy_cleanup": cleanup or {}, "archive_target": archive_target}
                      if archive_target else cleanup or {}),
        agent_run_id=agent_run.id if agent_run else None,
    )
    db.add(row)
    db.flush()
    for path_record in db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=working_copy.id, document_version_id=version.id, status="COMPLETED"):
        path_record.status = "PUBLISHING"
    return row


def pending_publication(db, *, dataset_id=None, working_copy_id=None):
    """统一判定是否允许读取或改写当前工作副本。"""
    query = db.query(StructuredDatasetPublication).filter(
        StructuredDatasetPublication.status.in_(("DB_COMMITTED", "NEEDS_REPAIR")))
    if dataset_id:
        query = query.filter_by(dataset_id=str(dataset_id))
    if working_copy_id:
        query = query.filter_by(working_copy_id=str(working_copy_id))
    return query.order_by(StructuredDatasetPublication.created_at.desc()).first()


def pending_replacement(db, *, dataset_id: str):
    """查找正在取代旧目标的发布；旧目标在此期间不得继续写入。"""

    rows = db.query(StructuredDatasetPublication).filter(
        StructuredDatasetPublication.status.in_(("DB_COMMITTED", "NEEDS_REPAIR"))).all()
    return next((row for row in rows
                 if ((row.cleanup_json or {}).get("archive_target") or {}).get("dataset_id") == dataset_id), None)


def assert_publication_ready(db, *, dataset_id=None, working_copy_id=None):
    """对外当前版本入口在双资源同步期间关闭式拒绝。"""
    row = pending_publication(db, dataset_id=dataset_id, working_copy_id=working_copy_id)
    if row is not None:
        raise HTTPException(423, detail={
            "code": "DATASET_PUBLICATION_PENDING" if row.status == "DB_COMMITTED" else "DATASET_PUBLICATION_NEEDS_REPAIR",
            "message": "Excel 正在同步同名工作副本；请稍后重试或联系管理员恢复。",
        })


def _mark_needs_repair(db, row, code: str) -> None:
    """同步更新用户任务和异步 Job，避免修复态被误报为普通等待。"""
    row.status = "NEEDS_REPAIR"
    row.error_code = code
    row.updated_at = utcnow()
    for path_record in db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=row.working_copy_id,
            document_version_id=row.candidate_document_version_id, status="PUBLISHING"):
        path_record.status = "NEEDS_REPAIR"
    task = db.get(StructuredDatasetTask, row.task_id) if row.task_id else None
    if task is not None:
        task.status = "NEEDS_REPAIR"
        task.error_json = {"code": code, "message": "Excel 发布状态不一致，请联系管理员核对版本。"}
        task.updated_at = utcnow()
    job_id = (row.result_json or {}).get("filesystem_job_id")
    job = db.get(FilesystemJob, job_id) if job_id else None
    if job is not None and job.status == "PUBLISHING":
        from app.modules.managed_files.jobs import FilesystemJobQueue
        FilesystemJobQueue(db).mark_failed(
            job=job, error_message="Excel 可见工作副本同步失败，请检查发布记录。")


def finalize_publication(db, publication_id: str) -> bool:
    """幂等补发可见文件，再核对版本、哈希、任务和审计后报告完成。"""
    row = db.query(StructuredDatasetPublication).filter_by(id=str(publication_id)).with_for_update().one_or_none()
    if row is None:
        return False
    if row.status == "COMPLETED":
        return True
    dataset = db.query(StructuredDataset).filter_by(id=row.dataset_id).with_for_update().one_or_none()
    working = db.query(WorkingCopy).filter_by(id=row.working_copy_id).with_for_update().one_or_none()
    managed = db.get(ManagedFile, working.managed_file_id) if working else None
    version = db.get(DocumentVersion, row.candidate_document_version_id)
    file_object = db.query(FileObject).filter_by(
        document_id=working.document_id, storage_backend="working_copy_local").one_or_none() if working else None
    exported = db.query(StructuredDatasetExport).filter_by(
        dataset_id=row.dataset_id, document_version_id=row.candidate_document_version_id).one_or_none()
    archive_target = (row.cleanup_json or {}).get("archive_target")
    old_dataset = old_working = old_managed = None
    if archive_target:
        old_dataset = (db.query(StructuredDataset).filter_by(id=archive_target["dataset_id"])
                       .with_for_update().one_or_none())
        old_working = (db.query(WorkingCopy).filter_by(id=archive_target["working_copy_id"])
                       .with_for_update().one_or_none())
        if old_working is not None:
            old_managed = (db.query(ManagedFile).filter_by(id=old_working.managed_file_id)
                           .with_for_update().one_or_none())
    if (dataset is None or working is None or version is None
            or working.current_version_id != row.candidate_document_version_id
            or working.content_sha256 != row.candidate_sha256
            or version.storage_path != row.candidate_storage_path
            or version.sha256 != row.candidate_sha256
            or file_object is None or file_object.sha256 != row.candidate_sha256
            or file_object.size_bytes != version.size_bytes
            or file_object.storage_path != row.visible_relative_path
            or exported is None or exported.sha256 != row.candidate_sha256
            or exported.size_bytes != version.size_bytes
            or (archive_target and (
                old_dataset is None or old_working is None or old_managed is None
                or managed is None or managed.status != "PUBLISHING"
                or old_dataset.working_copy_id != old_working.id
                or old_working.status != "ACTIVE"
                or old_managed.status != "ACTIVE"
                or old_working.current_version_id != archive_target["document_version_id"]
                or old_working.content_sha256 != archive_target["sha256"]
                or old_working.revision != archive_target["revision"]
                or old_working.filename.casefold() != working.filename.casefold()))):
        _mark_needs_repair(db, row, "DATASET_PUBLICATION_STATE_CONFLICT")
        db.commit()
        return False
    storage = FileLifecycleStorageService()
    try:
        visible = storage.publish_dataset_current_file(
            version_relative_path=row.candidate_storage_path,
            current_relative_path=row.visible_relative_path,
            expected_sha256=row.candidate_sha256,
        )
        if storage.sha256_file(visible) != row.candidate_sha256:
            raise ValueError("published hash mismatch")
        _sync_directory(visible.parent)
        legacy_cleanup = ((row.cleanup_json or {}).get("legacy_cleanup", {})
                          if archive_target else row.cleanup_json or {})
        if legacy_cleanup:
            storage.retire_dataset_legacy_current_file(**legacy_cleanup)
    except Exception as exc:
        row.attempt_count += 1
        row.error_code = type(exc).__name__[:100]
        if row.attempt_count >= 5:
            _mark_needs_repair(db, row, "DATASET_VISIBLE_SYNC_FAILED")
        row.updated_at = utcnow()
        db.commit()
        log_event("structured_dataset.publication.failed", level="ERROR", status=row.status,
                  error_code=row.error_code, document_id=working.document_id)
        return False
    if archive_target:
        old_working.status = "ARCHIVED"
        old_working.revision += 1
        old_managed.status = "ARCHIVED"
        working.status = "ACTIVE"
        managed.status = "ACTIVE"
        if row.agent_run_id:
            changeset = db.query(ChangeSet).filter_by(agent_run_id=row.agent_run_id).first()
            if changeset is not None:
                db.add(ChangeItem(
                    changeset_id=changeset.id,
                    target_type="DATASET",
                    target_id=old_dataset.id,
                    target_document_id=old_working.document_id,
                    change_type="STRUCTURED_DATASET_TARGET_ARCHIVED",
                    before_value_json={"status": "ACTIVE", "working_copy_id": old_working.id},
                    after_value_json={"status": "ARCHIVED", "replaced_by_dataset_id": dataset.id},
                    source="workbuddy-dataset-demo", confidence=1.0,
                    evidence_json={"replaced_version_id": archive_target["document_version_id"]},
                    execution_status="COMPLETED",
                ))
        row.result_json = {**dict(row.result_json or {}),
                           "archived_dataset_id": old_dataset.id}
    if row.task_id:
        task = db.get(StructuredDatasetTask, row.task_id)
        if task is None or task.status != "PUBLISHING":
            _mark_needs_repair(db, row, "DATASET_TASK_STATE_CONFLICT")
            db.commit()
            return False
        task.result_json = {**row.result_json, "excel_published": True}
        task.status = "COMPLETED"
        task.updated_at = utcnow()
    job_id = (row.result_json or {}).get("filesystem_job_id")
    job = db.get(FilesystemJob, job_id) if job_id else None
    if job is not None and job.status == "PUBLISHING":
        from app.modules.managed_files.jobs import FilesystemJobQueue
        FilesystemJobQueue(db).mark_completed(job=job, result={
            **row.result_json, "status": "READY", "excel_published": True,
            "saved_to_server": True})
    if dataset.active_task_id in {row.id, row.task_id}:
        dataset.active_task_id = None
    row.status = "COMPLETED"
    row.error_code = None
    row.updated_at = utcnow()
    for path_record in db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=row.working_copy_id,
            document_version_id=row.candidate_document_version_id, status="PUBLISHING"):
        path_record.status = "COMPLETED"
    if row.agent_run_id:
        run = db.get(AgentRun, row.agent_run_id)
        if run is not None:
            run.status = "COMPLETED"
            run.graph_state_json = {"status": "COMPLETED"}
            run.final_response = "累计 Excel 已发布。"
        for invocation in db.query(ToolInvocation).filter_by(agent_run_id=row.agent_run_id, status="RUNNING"):
            invocation.status = "COMPLETED"
            invocation.output_json = {**dict(invocation.output_json or {}),
                                      "excel_published": True,
                                      "document_version_id": row.candidate_document_version_id}
            invocation.finished_at = utcnow()
        for change_set in db.query(ChangeSet).filter_by(agent_run_id=row.agent_run_id, status="PENDING"):
            change_set.status = "COMPLETED"
            for item in db.query(ChangeItem).filter_by(changeset_id=change_set.id, execution_status="PENDING"):
                item.execution_status = "COMPLETED"
                item.after_value_json = {**dict(item.after_value_json or {}),
                                         "excel_published": True,
                                         "document_version_id": row.candidate_document_version_id}
    # 可见版本与搜索投影同事务提交；否则已发布的累计表无法按文件名检索，
    # 而后续版本仍会因投影指向旧版本被检索结果的最终事实校验过滤。
    from app.modules.retrieval.search_profile import DocumentSearchProfileService

    profiles = DocumentSearchProfileService(db=db)
    if archive_target:
        profiles.deactivate_profile(old_working.id)
    if not profiles.upsert_current_profile(working.id).get("ok"):
        raise RuntimeError("累计 Excel 搜索投影更新失败")
    db.commit()
    return True


def recover_pending_publications(session_factory, *, limit=20) -> dict:
    """启动或定时调用；每条使用独立事务，失败不妨碍其他目标恢复。"""
    with session_factory() as db:
        ids = [item.id for item in db.query(StructuredDatasetPublication).filter_by(
            status="DB_COMMITTED").order_by(StructuredDatasetPublication.created_at).limit(limit)]
    recovered = 0
    for publication_id in ids:
        with session_factory() as db:
            try:
                recovered += bool(finalize_publication(db, publication_id))
            except Exception as exc:
                db.rollback()
                log_event("structured_dataset.publication.recovery_failed", level="ERROR",
                          error_code=type(exc).__name__)
    return {"scanned": len(ids), "recovered": recovered}


def repair_needs_repair_publication(db, publication_id: str, *, expected_visible_sha256: str) -> bool:
    """人工核对后只补发已提交的候选版本，不重新追加数据或创建版本。"""
    row = (db.query(StructuredDatasetPublication).filter_by(id=str(publication_id))
           .with_for_update().one_or_none())
    if row is None or row.status != "NEEDS_REPAIR" or row.error_code != "DATASET_VISIBLE_SYNC_FAILED":
        raise ValueError("发布记录不是可补发的文件同步失败状态")
    dataset = db.query(StructuredDataset).filter_by(id=row.dataset_id).with_for_update().one_or_none()
    working = db.query(WorkingCopy).filter_by(id=row.working_copy_id).with_for_update().one_or_none()
    candidate = db.get(DocumentVersion, row.candidate_document_version_id)
    baseline = db.get(DocumentVersion, row.base_document_version_id) if row.base_document_version_id else None
    task = db.get(StructuredDatasetTask, row.task_id) if row.task_id else None
    exported = db.query(StructuredDatasetExport).filter_by(
        dataset_id=row.dataset_id, document_version_id=row.candidate_document_version_id).one_or_none()
    file_object = (db.query(FileObject).filter_by(
        document_id=working.document_id, storage_backend="working_copy_local").one_or_none()
        if working is not None else None)
    if (dataset is None or working is None or candidate is None or task is None or baseline is None
            or exported is None or file_object is None or task.status != "NEEDS_REPAIR"
            or dataset.working_copy_id != working.id or dataset.active_task_id != task.id
            or working.current_version_id != candidate.id or working.content_sha256 != row.candidate_sha256
            or candidate.working_copy_id != working.id or candidate.document_id != working.document_id
            or candidate.sha256 != row.candidate_sha256 or candidate.storage_path != row.candidate_storage_path
            or working.size_bytes != candidate.size_bytes
            or baseline.working_copy_id != working.id or baseline.document_id != working.document_id
            or candidate.parent_version_id != baseline.id
            or exported.sha256 != row.candidate_sha256 or exported.size_bytes != candidate.size_bytes
            or file_object.sha256 != row.candidate_sha256 or file_object.size_bytes != candidate.size_bytes
            or file_object.storage_path != row.visible_relative_path):
        raise ValueError("发布记录、数据库当前版本或基线版本不一致")
    from app.db.models import WorkingCopyRoot
    root = db.get(WorkingCopyRoot, working.working_copy_root_id)
    root_part = root.relative_storage_path.strip("/\\") if root is not None else ""
    copy_part = working.relative_path.strip("/\\")
    if root is None or row.visible_relative_path != f"{root_part}/{copy_part}":
        raise ValueError("同名可见文件路径与工作副本不一致")
    storage = FileLifecycleStorageService()
    candidate_path = storage.working_copy_path(row.candidate_storage_path)
    visible_path = storage.working_copy_path(row.visible_relative_path)
    if (not candidate_path.is_file() or candidate_path.stat().st_size != candidate.size_bytes
            or storage.sha256_file(candidate_path) != row.candidate_sha256):
        raise ValueError("不可变候选文件缺失或校验失败")
    visible_sha256 = storage.sha256_file(visible_path) if visible_path.is_file() else None
    if (visible_sha256 != expected_visible_sha256
            or visible_sha256 not in {baseline.sha256, row.candidate_sha256}):
        raise ValueError("同名可见文件不是已核对的基线或候选版本，拒绝覆盖")
    row.status = "DB_COMMITTED"
    row.attempt_count = 0
    row.error_code = None
    row.updated_at = utcnow()
    task.status = "PUBLISHING"
    task.error_json = {}
    task.updated_at = utcnow()
    for path_record in db.query(WorkingCopyPathRecord).filter_by(
            working_copy_id=working.id, document_version_id=candidate.id, status="NEEDS_REPAIR"):
        path_record.status = "PUBLISHING"
    db.flush()
    log_event("structured_dataset.publication.repair_started", status="DB_COMMITTED",
              document_id=working.document_id)
    return finalize_publication(db, row.id)
