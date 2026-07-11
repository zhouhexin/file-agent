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


def test_scanner_fingerprint_is_fixed_length_for_deep_paths(tmp_path):
    """fingerprint 不能保存完整路径，深层中文目录也必须稳定落库。"""

    inbox = tmp_path / "student-affairs"
    deep_dir = inbox / "人事处" / "人才工程科（24年9月前原师资科）" / "2021年" / "人才工作" / "寒假工作"
    deep_dir.mkdir(parents=True)
    (deep_dir / "附件6.正确认识和规范使用高校人才称号的自查情况及整改措施.doc").write_bytes(b"doc")

    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        root = ManagedRoot(root_key="student_affairs", display_name="学工收件箱", container_path=str(inbox))
        db.add(root)
        db.commit()

        ManagedFileScanner(db).scan_root(root)

        stored = db.query(ManagedFile).one()
        assert len(stored.fingerprint) == 64
        assert "/" not in stored.fingerprint
        assert stored.relative_path.startswith("人事处/")
    finally:
        db.close()
        clear_overrides()


def test_scanner_derives_category_path_only_for_path_classified_root(tmp_path):
    """只有 PATH_AS_CATEGORY 目录会把父目录写成分类路径。"""

    classified_dir = tmp_path / "classified"
    plain_dir = tmp_path / "plain"
    (classified_dir / "奖学金" / "国家励志奖学金").mkdir(parents=True)
    plain_dir.mkdir()
    (classified_dir / "奖学金" / "国家励志奖学金" / "a.pdf").write_bytes(b"pdf")
    (plain_dir / "临时" ).mkdir()
    (plain_dir / "临时" / "b.pdf").write_bytes(b"pdf")

    client, SessionLocal = client_with_database()
    db = SessionLocal()
    try:
        classified_root = ManagedRoot(
            root_key="classified_library",
            display_name="已分类文件库",
            container_path=str(classified_dir),
            classification_mode="PATH_AS_CATEGORY",
        )
        plain_root = ManagedRoot(
            root_key="plain_inbox",
            display_name="普通收件箱",
            container_path=str(plain_dir),
            classification_mode="NONE",
        )
        db.add_all([classified_root, plain_root])
        db.commit()

        ManagedFileScanner(db).scan_root(classified_root)
        ManagedFileScanner(db).scan_root(plain_root)

        classified_file = db.query(ManagedFile).filter(ManagedFile.root_id == classified_root.id).one()
        plain_file = db.query(ManagedFile).filter(ManagedFile.root_id == plain_root.id).one()
        assert classified_file.category_path == "奖学金/国家励志奖学金"
        assert plain_file.category_path is None
    finally:
        db.close()
        clear_overrides()
