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


def test_filename_filter_treats_sql_wildcards_as_literal_text():
    """文件名过滤中的百分号和下划线不能扩大列表或批量统计范围。"""

    from app.modules.managed_files.repository import ManagedFileRepository

    db = _session()
    try:
        root = ManagedRoot(
            root_key="downloads",
            display_name="downloads",
            container_path="/managed/downloads",
        )
        db.add(root)
        db.flush()
        for index, filename in enumerate(["完成率100%.txt", "普通材料.txt"], start=1):
            db.add(
                ManagedFile(
                    root_id=root.id,
                    relative_path=filename,
                    relative_path_hash=f"hash-filter-{index}",
                    filename=filename,
                    extension=".txt",
                    size_bytes=10,
                    fingerprint=str(index) * 64,
                    status="ACTIVE",
                )
            )
        db.flush()
        repository = ManagedFileRepository(db)

        rows = repository.list_files(
            root_key="downloads",
            filename_contains="%",
            status="ACTIVE",
        )
        count = repository.count_files(
            root_key="downloads",
            filename_contains="%",
            status="ACTIVE",
        )

        assert [file.filename for file, _root in rows] == ["完成率100%.txt"]
        assert count == 1
    finally:
        db.close()


def test_managed_directory_scope_resolver_requires_unique_real_directory():
    """末级目录重名时必须澄清，完整多级路径应唯一解析。"""

    from app.modules.managed_files.directory_scope_resolver import ManagedDirectoryScopeResolver
    from app.modules.managed_files.repository import ManagedFileRepository

    db = _session()
    try:
        root = ManagedRoot(
            root_key="downloads",
            display_name="downloads",
            container_path="/managed/downloads",
        )
        db.add(root)
        db.flush()
        for index, relative_path in enumerate(
            ["校办/2024/工作通知.txt", "党办/2024/会议通知.txt"],
            start=1,
        ):
            db.add(
                ManagedFile(
                    root_id=root.id,
                    relative_path=relative_path,
                    relative_path_hash=f"hash-scope-{index}",
                    filename=relative_path.rsplit("/", 1)[-1],
                    extension=".txt",
                    size_bytes=10,
                    fingerprint=str(index) * 64,
                    status="ACTIVE",
                )
            )
        db.flush()
        resolver = ManagedDirectoryScopeResolver(ManagedFileRepository(db))

        ambiguous = resolver.resolve(
            root_key="downloads",
            configured_root_keys=["downloads"],
            path_prefix="2024",
        )
        resolved = resolver.resolve(
            root_key="downloads",
            configured_root_keys=["downloads"],
            path_prefix="校办/2024",
        )

        assert ambiguous.status == "NEEDS_CLARIFICATION"
        assert [item.path_prefix for item in ambiguous.candidates] == ["党办/2024", "校办/2024"]
        assert resolved.status == "RESOLVED"
        assert resolved.root_key == "downloads"
        assert resolved.path_prefix == "校办/2024"
    finally:
        db.close()
