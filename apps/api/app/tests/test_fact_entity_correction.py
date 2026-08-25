"""事实检索人名纠错候选回归测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    ManagedFile,
    ManagedFileRevision,
    ManagedFileSearchProfile,
    ManagedRoot,
)
from app.modules.retrieval.entity_correction import (
    FactEntityCorrectionService,
    attach_entity_corrections,
)


def _db():
    """创建启用完整 ORM 表的 SQLite 会话。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add_profile(db, *, suffix: str, search_text: str, entities: list[str]) -> None:
    """写入一份当前活动源侧检索投影。"""

    root = db.get(ManagedRoot, "fact-root")
    if root is None:
        root = ManagedRoot(
            id="fact-root",
            root_key="fact-root",
            display_name="事实材料",
            container_path="/managed/facts",
            enabled=True,
        )
        db.add(root)
        db.flush()
    managed_file = ManagedFile(
        id=f"fact-file-{suffix}",
        root_id=root.id,
        relative_path=f"报告/{suffix}.pdf",
        relative_path_hash=f"fact-path-{suffix}",
        filename=f"{suffix}.pdf",
        extension=".pdf",
        size_bytes=1,
        fingerprint=f"fact-fingerprint-{suffix}",
        status="ACTIVE",
    )
    revision = ManagedFileRevision(
        id=f"fact-revision-{suffix}",
        managed_file_id=managed_file.id,
        revision_number=1,
        size_bytes=1,
        quick_fingerprint=managed_file.fingerprint,
        status="READY",
        analysis_status="COMPLETED",
        is_current=True,
    )
    profile = ManagedFileSearchProfile(
        id=f"fact-profile-{suffix}",
        managed_file_revision_id=revision.id,
        analysis_run_id=f"fact-analysis-{suffix}",
        normalized_filename=suffix,
        title=suffix,
        search_text=search_text,
        entities_json=entities,
        status="ACTIVE",
    )
    db.add_all([managed_file, revision, profile])
    db.flush()


def test_unique_one_character_person_name_difference_is_only_a_correction_hint():
    """唯一一字差异可以提示，但不能把候选提升为已验证事实结果。"""

    db = _db()
    try:
        _add_profile(
            db,
            suffix="briefing-a",
            search_text="彭绍亮 国防科技大学",
            entities=["彭绍亮", "国防科技大学"],
        )
        _add_profile(
            db,
            suffix="briefing-b",
            search_text="报告人 彭绍亮 超级计算",
            entities=["彭绍亮"],
        )

        corrections = FactEntityCorrectionService(db=db).suggest(
            entity_phrases=["彭绍高"]
        )
        result = attach_entity_corrections(
            result={
                "ok": True,
                "supported_count": 0,
                "possible_count": 0,
                "results": [],
            },
            corrections=corrections,
        )

        assert [(item.original, item.candidate) for item in corrections] == [
            ("彭绍高", "彭绍亮")
        ]
        assert result["results"] == []
        assert result["supported_count"] == 0
        assert result["query_corrections"][0]["candidate"] == "彭绍亮"
    finally:
        db.close()


def test_non_person_business_phrase_is_never_approximately_rewritten():
    """机构和业务短语不进入姓名纠错，避免扩大普通文件检索语义。"""

    db = _db()
    try:
        _add_profile(
            db,
            suffix="laboratory",
            search_text="大数据联合实验室",
            entities=["大数据联合实验室"],
        )

        assert FactEntityCorrectionService(db=db).suggest(
            entity_phrases=["实验事"]
        ) == []
    finally:
        db.close()
