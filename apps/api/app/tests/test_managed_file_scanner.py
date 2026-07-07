"""受管目录只读扫描器测试。"""

from app.db.models import ManagedFile, ManagedRoot
from app.modules.managed_files.scanner import ManagedFileScanner
from app.tests.helpers import clear_overrides, client_with_database


def test_scanner_records_file_metadata_and_marks_missing(tmp_path):
    """扫描器只记录元数据，重复扫描不重复入库，缺失文件标记 MISSING。"""

    inbox = tmp_path / "student-affairs"
    inbox.mkdir()
    first_file = inbox / "a.pdf"
    first_file.write_bytes(b"pdf")
    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        root = ManagedRoot(root_key="student_affairs", display_name="学工收件箱", container_path=str(inbox))
        db.add(root)
        db.commit()

        first_run = ManagedFileScanner(db).scan_root(root)
        second_run = ManagedFileScanner(db).scan_root(root)
        first_file.unlink()
        third_run = ManagedFileScanner(db).scan_root(root)

        assert first_run.files_discovered == 1
        assert second_run.files_discovered == 1
        assert third_run.files_missing == 1
        assert db.query(ManagedFile).count() == 1
        stored = db.query(ManagedFile).one()
        assert stored.relative_path == "a.pdf"
        assert stored.extension == ".pdf"
        assert stored.status == "MISSING"
    finally:
        db.close()
        clear_overrides()
