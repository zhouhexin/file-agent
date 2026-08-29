"""清空文件域开发数据并严格保留用户身份数据。

本命令只用于用户明确授权的开发分类测试。它清除文件、解析、分类、检索、
工作副本、旧会话和文件任务审计，同时保留用户、工作区、受管根配置和
``alembic_version``。外部受管原始目录永远不在清理目标中。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal, engine
from app.db import models  # noqa: F401  # 注册完整 ORM 表，供清理 manifest 校验。
from app.db.base import Base
from app.db.models import ManagedRoot, User, Workspace
from app.scripts.reset_development_shared_workspace import (
    ResetTarget,
    clear_directory_contents,
    configured_external_managed_roots,
    validate_reset_targets,
)


# 用户、工作区和外部目录授权是本次测试必须保留的身份/配置事实。
PRESERVED_TABLES = frozenset({"users", "workspaces", "managed_roots"})

# 文件域 manifest 必须显式列出。后续新增 ORM 表却未归类时，命令必须拒绝执行，
# 防止新数据被误删或旧文件引用被漏清。
FILE_DOMAIN_TABLES = frozenset(
    {
        "agent_runs",
        "answer_references",
        "capability_suggestions",
        "change_items",
        "change_sets",
        "classification_clarifications",
        "classification_graph_outbox",
        "conversations",
        "document_artifacts",
        "document_categories",
        "document_category_confirmation_sources",
        "document_category_feedback",
        "document_category_suggestions",
        "document_chunks",
        "document_classification_runs",
        "document_classification_summaries",
        "document_elements",
        "document_extraction_runs",
        "document_index_runs",
        "document_insights",
        "document_organization_decisions",
        "document_pages",
        "document_search_profiles",
        "document_summaries",
        "document_versions",
        "documents",
        "evidence_spans",
        "file_objects",
        "file_rename_batch_items",
        "file_rename_batches",
        "file_rename_review_items",
        "file_search_clarifications",
        "filesystem_job_events",
        "filesystem_jobs",
        "filesystem_scan_runs",
        "graph_projection_runs",
        "managed_file_analysis_runs",
        "managed_file_events",
        "managed_file_revisions",
        "managed_file_search_profiles",
        "managed_file_snapshots",
        "managed_file_table_structures",
        "managed_file_text_chunks",
        "managed_files",
        "messages",
        "operation_confirmations",
        "operation_plans",
        "planner_shadow_comparisons",
        "qa_answers",
        "relevant_file_set_items",
        "relevant_file_sets",
        "structured_extraction_fields",
        "structured_extraction_runs",
        "tool_invocations",
        "trash_entries",
        "upload_archive_records",
        "upload_duplicate_candidates",
        "upload_duplicate_reviews",
        "working_copies",
        "working_copy_path_records",
        "working_copy_roots",
    }
)


def build_file_reset_targets(settings: Settings) -> list[ResetTarget]:
    """构造文件域精确目录目标，绝不包含外部受管原始目录。"""

    storage_root = Path(settings.file_storage_root).expanduser().resolve()
    return [
        ResetTarget(
            "共享工作副本",
            Path(settings.working_copy_storage_root).expanduser().resolve(),
        ),
        ResetTarget(
            "共享回收站",
            Path(settings.trash_storage_root).expanduser().resolve(),
        ),
        ResetTarget("上传暂存", storage_root / "uploads"),
        ResetTarget("隔离暂存", storage_root / "quarantine"),
        ResetTarget("临时处理目录", storage_root / "temp"),
        ResetTarget(
            "内部上传原件保护",
            Path(settings.managed_root_archive_write_path).expanduser().resolve(),
        ),
    ]


def validate_table_manifest(*, database_engine: Engine) -> None:
    """校验 ORM 和真实数据库表全部被明确归类，未知表出现时停止。"""

    expected = set(PRESERVED_TABLES | FILE_DOMAIN_TABLES)
    orm_tables = set(Base.metadata.tables)
    if orm_tables != expected:
        missing = sorted(orm_tables - expected)
        stale = sorted(expected - orm_tables)
        raise ValueError(
            "文件域重置表清单与 ORM 不一致："
            f"未归类={missing or '无'}，清单残留={stale or '无'}"
        )

    actual_tables = set(inspect(database_engine).get_table_names())
    actual_business_tables = actual_tables - {"alembic_version"}
    if actual_business_tables != expected:
        unknown = sorted(actual_business_tables - expected)
        absent = sorted(expected - actual_business_tables)
        raise ValueError(
            "文件域重置表清单与数据库不一致："
            f"未知表={unknown or '无'}，缺失表={absent or '无'}"
        )

    # 保留表不能引用待清理表；否则清理会留下悬空关系或诱发级联删除。
    unsafe_references: list[str] = []
    for table_name in PRESERVED_TABLES:
        table = Base.metadata.tables[table_name]
        for foreign_key in table.foreign_keys:
            target = foreign_key.column.table.name
            if target in FILE_DOMAIN_TABLES:
                unsafe_references.append(f"{table_name}->{target}")
    if unsafe_references:
        raise ValueError(
            "保留表引用待清理表，拒绝执行：" + ", ".join(sorted(unsafe_references))
        )


def identity_fingerprint(db: Session) -> tuple[int, str]:
    """生成用户身份不可逆指纹，用于证明清理前后账号没有变化。"""

    rows = (
        db.query(User)
        .order_by(User.id.asc())
        .all()
    )
    payload = [
        {
            "id": row.id,
            "username": row.username,
            "email": row.email,
            "password_hash": row.password_hash,
            "display_name": row.display_name,
            "role": row.role,
            "default_workspace_id": row.default_workspace_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(payload), hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def workspace_fingerprint(db: Session) -> tuple[int, str]:
    """生成工作区不可逆指纹，保护默认工作区和共享工作区稳定 ID。"""

    rows = db.query(Workspace).order_by(Workspace.id.asc()).all()
    payload = [
        {
            "id": row.id,
            "name": row.name,
            "owner_id": row.owner_id,
            "is_default": row.is_default,
            "workspace_type": row.workspace_type,
            "system_key": row.system_key,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(payload), hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def clear_file_domain_tables(db: Session, *, database_engine: Engine) -> None:
    """清空文件域表；PostgreSQL 不使用 CASCADE，保留表依赖会直接令命令失败。"""

    if database_engine.dialect.name == "postgresql":
        quoted = ", ".join(f'"{name}"' for name in sorted(FILE_DOMAIN_TABLES))
        db.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY"))
    else:
        # SQLite 只用于隔离测试，连接默认不启用外键约束；不用 sorted_tables，
        # 避免 ORM 中真实存在的版本/工作副本循环关系产生无意义告警。
        for table_name in sorted(FILE_DOMAIN_TABLES, reverse=True):
            db.execute(Base.metadata.tables[table_name].delete())

    # 受管根配置保留，但旧扫描游标属于文件运行状态，必须清空后重新对账。
    db.query(ManagedRoot).update(
        {ManagedRoot.last_reconciled_at: None}, synchronize_session=False
    )
    db.flush()


def run_file_data_reset(
    *,
    settings: Settings,
    project_root: Path,
    db: Session,
    database_engine: Engine,
    external_import_stopped: bool,
) -> list[str]:
    """执行文件域清理并在提交前验证用户和工作区完全未变。"""

    if not external_import_stopped:
        raise ValueError("必须先停止 watcher、scheduler 和全部 worker")
    if not settings.managed_root_archive_write_path:
        raise ValueError("MANAGED_ROOT_ARCHIVE_WRITE_PATH 未配置，拒绝猜测内部原件位置")

    targets = build_file_reset_targets(settings)
    validate_reset_targets(
        targets,
        project_root=project_root,
        protected_roots=configured_external_managed_roots(),
    )

    # 必须先验证数据库连接和完整表清单，再触碰任何目录。
    db.connection()
    validate_table_manifest(database_engine=database_engine)
    before_users = identity_fingerprint(db)
    before_workspaces = workspace_fingerprint(db)
    if before_users[0] == 0:
        raise ValueError("当前数据库没有用户，文件域重置不符合保留用户目标")

    try:
        clear_file_domain_tables(db, database_engine=database_engine)
        after_users = identity_fingerprint(db)
        after_workspaces = workspace_fingerprint(db)
        if after_users != before_users:
            raise RuntimeError("用户身份数据在文件域重置中发生变化，已拒绝提交")
        if after_workspaces != before_workspaces:
            raise RuntimeError("工作区数据在文件域重置中发生变化，已拒绝提交")
        for target in targets:
            clear_directory_contents(target)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return [
        f"用户身份（保留 {before_users[0]} 个）",
        f"工作区（保留 {before_workspaces[0]} 个）",
        f"文件域数据库表（清空 {len(FILE_DOMAIN_TABLES)} 张）",
        *[target.label for target in targets],
        "外部受管原始目录（未修改）",
    ]


def main() -> None:
    """解析双重确认参数后执行保留用户的文件域重置。"""

    parser = argparse.ArgumentParser(
        description="清空 File Agent 文件域开发数据并保留用户和工作区"
    )
    parser.add_argument(
        "--confirm-reset-file-data",
        action="store_true",
        help="确认清除文件、分类、索引、工作副本、旧会话和文件任务数据",
    )
    parser.add_argument(
        "--confirm-external-import-stopped",
        action="store_true",
        help="确认 API、scheduler、watcher 和全部 worker 已停止",
    )
    args = parser.parse_args()
    if not args.confirm_reset_file_data:
        parser.error("必须提供 --confirm-reset-file-data；本命令不会隐式删除文件数据")
    if not args.confirm_external_import_stopped:
        parser.error("必须提供 --confirm-external-import-stopped")

    settings = get_settings()
    project_root = Path.cwd().resolve()
    with SessionLocal() as db:
        completed = run_file_data_reset(
            settings=settings,
            project_root=project_root,
            db=db,
            database_engine=engine,
            external_import_stopped=True,
        )
    print("文件域重置完成：" + "、".join(completed))


if __name__ == "__main__":
    main()
