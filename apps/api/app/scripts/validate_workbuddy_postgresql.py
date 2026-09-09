"""在真实 PostgreSQL 上验证 WorkBuddy 导入迁移与并发约束。

脚本只创建带 ``workbuddy-pg-validation-`` 前缀的临时用户和业务记录，验证完成或失败后均按
该用户的稳定 ID 精确清理。它不调用文件处理、不读取正文，也不得用作生产数据重置工具。
"""

from __future__ import annotations

import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal, engine
from app.db.models import IngestBatch, IngestDuplicateGroup, IngestItem, IntegrationRequest, User, Workspace
from app.modules.ingestion.schemas import IngestBatchCreateRequest
from app.modules.ingestion.service import IngestionService


REQUIRED_TABLES = {
    "ingest_batches",
    "ingest_items",
    "integration_requests",
    "ingest_duplicate_groups",
    "ingest_duplicate_group_members",
    "external_extraction_tasks",
    "external_extraction_pages",
    "ingest_request_executions",
}


def _create_validation_identity(run_key: str) -> tuple[str, str]:
    """创建与现有用户隔离的临时身份和默认工作区。"""

    with SessionLocal() as db:
        user = User(
            username=f"workbuddy-pg-validation-{run_key}",
            password_hash="validation-only",
            display_name="WorkBuddy PostgreSQL 验证",
            role="user",
        )
        db.add(user)
        db.flush()
        workspace = Workspace(
            name="WorkBuddy PostgreSQL 验证工作区",
            owner_id=user.id,
            is_default=True,
            workspace_type="USER",
        )
        db.add(workspace)
        db.flush()
        user.default_workspace_id = workspace.id
        db.commit()
        return user.id, workspace.id


def _validate_concurrent_batch_idempotency(*, user_id: str, run_key: str) -> str:
    """并发提交同一幂等事件，必须只产生一个批次和一条请求审计。"""

    request = IngestBatchCreateRequest(
        client_id="workbuddy-pg-validation",
        request_id=f"request-{run_key}",
        idempotency_key=f"batch-{run_key}",
        source_root_ref="validation-root",
        relative_directory="validation-materials",
        recursive=True,
        user_request=None,
    )
    barrier = threading.Barrier(4)

    def create_once() -> str:
        """在独立 Session 中提交一次相同批次请求。"""

        with SessionLocal() as db:
            user = db.get(User, user_id)
            if user is None:
                raise RuntimeError("验证用户在并发请求前丢失")
            barrier.wait(timeout=10)
            return IngestionService(db).create_batch(request=request, current_user=user).id

    with ThreadPoolExecutor(max_workers=4) as executor:
        batch_ids = list(executor.map(lambda _: create_once(), range(4)))
    if len(set(batch_ids)) != 1:
        raise RuntimeError(f"并发幂等请求创建了多个批次：{batch_ids}")
    with SessionLocal() as db:
        batch_count = db.query(IngestBatch).filter(IngestBatch.user_id == user_id).count()
        audit_count = db.query(IntegrationRequest).filter(
            IntegrationRequest.user_id == user_id,
            IntegrationRequest.operation == "INGEST_BATCH_CREATE",
        ).count()
        if batch_count != 1 or audit_count != 1:
            raise RuntimeError(f"并发幂等记录数量错误：batch={batch_count}, audit={audit_count}")
    return batch_ids[0]


def _validate_duplicate_group_uniqueness(
    *,
    batch_id: str,
    user_id: str,
    workspace_id: str,
    run_key: str,
) -> None:
    """并发插入同一批次哈希组，数据库必须只允许一个活动主组。"""

    item_ids: list[str] = []
    with SessionLocal() as db:
        for index in range(2):
            item = IngestItem(
                batch_id=batch_id,
                client_item_id=f"validation-item-{run_key}-{index}",
                source_root_ref="validation-root",
                source_relative_path=f"validation-materials/item-{index}.txt",
                original_filename=f"item-{index}.txt",
                expected_size=1,
                source_mtime_ns=index + 1,
                workflow_revision=1,
                stage="RECEIVE",
                status="PENDING",
                error_json={},
                result_json={},
            )
            db.add(item)
            db.flush()
            item_ids.append(item.id)
        db.commit()
    barrier = threading.Barrier(2)

    def insert_group(primary_item_id: str) -> str:
        """竞争写入同一个活动内容组，冲突方读取数据库胜者。"""

        with SessionLocal() as db:
            barrier.wait(timeout=10)
            group = IngestDuplicateGroup(
                batch_id=batch_id,
                user_id=user_id,
                workspace_id=workspace_id,
                content_sha256="a" * 64,
                revision=1,
                primary_item_id=primary_item_id,
                status="ACTIVE",
            )
            db.add(group)
            try:
                db.commit()
                return group.id
            except IntegrityError:
                db.rollback()
                winner = db.query(IngestDuplicateGroup).filter(
                    IngestDuplicateGroup.batch_id == batch_id,
                    IngestDuplicateGroup.content_sha256 == "a" * 64,
                    IngestDuplicateGroup.status == "ACTIVE",
                ).one()
                return winner.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        group_ids = list(executor.map(insert_group, item_ids))
    if len(set(group_ids)) != 1:
        raise RuntimeError(f"相同内容形成了多个活动重复组：{group_ids}")


def _cleanup_validation_identity(*, user_id: str, workspace_id: str) -> None:
    """只清理本次临时身份范围，绝不按宽泛前缀删除业务数据。"""

    with SessionLocal() as db:
        user = db.get(User, user_id)
        workspace = db.get(Workspace, workspace_id)
        if user is not None:
            user.default_workspace_id = None
        if workspace is not None:
            workspace.owner_id = None
        db.flush()
        if user is not None:
            db.delete(user)
        db.flush()
        if workspace is not None:
            db.delete(workspace)
        db.commit()


def validate_workbuddy_postgresql() -> dict[str, object]:
    """执行迁移结构、并发幂等和重复组唯一性验证并返回安全摘要。"""

    if engine.dialect.name != "postgresql":
        raise RuntimeError("该验证脚本只能连接 PostgreSQL")
    existing_tables = set(inspect(engine).get_table_names())
    missing = sorted(REQUIRED_TABLES - existing_tables)
    if missing:
        raise RuntimeError(f"WorkBuddy 迁移表缺失：{missing}")
    run_key = uuid4().hex[:12]
    user_id, workspace_id = _create_validation_identity(run_key)
    try:
        batch_id = _validate_concurrent_batch_idempotency(user_id=user_id, run_key=run_key)
        _validate_duplicate_group_uniqueness(
            batch_id=batch_id,
            user_id=user_id,
            workspace_id=workspace_id,
            run_key=run_key,
        )
        return {
            "ok": True,
            "required_table_count": len(REQUIRED_TABLES),
            "concurrent_batch_requests": 4,
            "concurrent_duplicate_group_writers": 2,
        }
    finally:
        _cleanup_validation_identity(user_id=user_id, workspace_id=workspace_id)


def main() -> None:
    """命令行入口只输出不含连接串和业务对象 ID 的验证结果。"""

    parser = argparse.ArgumentParser(description="验证 WorkBuddy PostgreSQL 迁移和并发边界")
    parser.parse_args()
    result = validate_workbuddy_postgresql()
    print(
        "WorkBuddy PostgreSQL validation passed: "
        f"tables={result['required_table_count']}, "
        f"batch_requests={result['concurrent_batch_requests']}, "
        f"group_writers={result['concurrent_duplicate_group_writers']}"
    )


if __name__ == "__main__":
    main()
