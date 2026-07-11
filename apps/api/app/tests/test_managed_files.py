"""服务器受管文件元数据表测试。

这些测试先保护 P0 所需 ORM 表和唯一约束，避免扫描器把同一逻辑目录文件重复入库。
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import FilesystemJob, FilesystemJobEvent, FilesystemScanRun, ManagedFile, ManagedRoot


def _session():
    """创建隔离 SQLite 会话，用于验证 ORM metadata。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal()


def test_managed_files_tables_can_be_created():
    """P0 受管目录、文件和异步任务表必须能通过 ORM metadata 创建。"""

    assert ManagedRoot.__tablename__ == "managed_roots"
    assert ManagedFile.__tablename__ == "managed_files"
    assert FilesystemJob.__tablename__ == "filesystem_jobs"
    assert FilesystemJobEvent.__tablename__ == "filesystem_job_events"
    assert FilesystemScanRun.__tablename__ == "filesystem_scan_runs"


def test_managed_root_key_is_unique():
    """root_key 是部署层授权目录的逻辑身份，必须唯一。"""

    db = _session()
    try:
        db.add(ManagedRoot(root_key="student_affairs", display_name="学工收件箱", container_path="/managed/student-affairs"))
        db.add(ManagedRoot(root_key="student_affairs", display_name="重复目录", container_path="/managed/other"))

        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.close()


def test_upsert_managed_root_skips_update_when_config_is_unchanged():
    """受管目录配置未变化时不应重复 UPDATE，避免列表查询产生无意义写锁。"""

    from app.modules.managed_files.repository import ManagedFileRepository

    db = _session()
    try:
        repository = ManagedFileRepository(db)
        root = repository.upsert_root(
            root_key="student_affairs",
            display_name="student_affairs",
            container_path="/managed/student-affairs",
            classification_mode="NONE",
            created_by=None,
        )
        db.commit()
        db.refresh(root)
        first_updated_at = root.updated_at

        root = repository.upsert_root(
            root_key="student_affairs",
            display_name="student_affairs",
            container_path="/managed/student-affairs",
            classification_mode="NONE",
            created_by=None,
        )

        assert root.updated_at == first_updated_at
    finally:
        db.close()


def test_managed_file_relative_path_hash_is_unique_per_root():
    """同一逻辑目录内路径哈希必须唯一，避免把超长路径放进唯一索引。"""

    db = _session()
    try:
        root = ManagedRoot(root_key="student_affairs", display_name="学工收件箱", container_path="/managed/student-affairs")
        db.add(root)
        db.flush()
        for _ in range(2):
            db.add(
                ManagedFile(
                    root_id=root.id,
                    relative_path="2026/a.pdf",
                    relative_path_hash="hash-a",
                    filename="a.pdf",
                    extension=".pdf",
                    size_bytes=10,
                    modified_at=datetime.now(timezone.utc),
                    fingerprint="fp",
                    status="ACTIVE",
                )
            )

        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.close()
