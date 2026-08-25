"""“全部材料”按受管源事件目录递归列举的回归测试。"""

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import ManagedFile, ManagedFileRevision, ManagedRoot
from app.modules.retrieval.event_collection import (
    EventCollectionRequest,
    EventCollectionSearchService,
)


def _session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class _PhraseStrategy:
    def __init__(self, anchor):
        self.anchor = anchor

    def search(self, **_kwargs):
        return {"results": [self.anchor], "partial": False}


class _Stage1:
    def enrich_working_copy_ids(self, *, working_copy_ids, scope):
        assert working_copy_ids == []
        assert scope.scope_mode == "global"
        return []


def test_event_collection_recursively_lists_managed_source_without_working_copies(monkeypatch):
    """唯一事件源目录中的嵌套图片和失败修订都必须出现在完整材料清单。"""

    monkeypatch.setattr(
        "app.modules.retrieval.event_collection.log_event",
        lambda *_args, **_kwargs: None,
    )
    db = _session()
    root = ManagedRoot(
        id="event-root",
        root_key="test_library",
        display_name="测试资料库",
        container_path="/managed/test-library",
        enabled=True,
    )
    relative_paths = [
        "20170606大数据联合实验室授牌/大数据联合实验室新闻稿20170606.docx",
        "20170606大数据联合实验室授牌/大数据联合实验室授牌仪式流程20170605.doc",
        "20170606大数据联合实验室授牌/大数据讲座及讲师简介.docx",
        "20170606大数据联合实验室授牌/大数据联合实验室成立授牌20170606/IMG_0198.JPG",
        "20170606大数据联合实验室授牌/大数据联合实验室成立授牌20170606/IMG_0199.JPG",
    ]
    files = []
    revisions = []
    for index, relative_path in enumerate(relative_paths):
        filename = relative_path.rsplit("/", 1)[-1]
        managed_file = ManagedFile(
            id=f"event-file-{index}",
            root_id=root.id,
            relative_path=relative_path,
            relative_path_hash=f"event-path-{index}",
            filename=filename,
            extension="." + filename.rsplit(".", 1)[-1].lower(),
            size_bytes=10,
            fingerprint=f"event-fingerprint-{index}",
            status="ACTIVE",
        )
        revision = ManagedFileRevision(
            id=f"event-revision-{index}",
            managed_file_id=managed_file.id,
            revision_number=1,
            size_bytes=10,
            quick_fingerprint=managed_file.fingerprint,
            status="READY" if index == 0 else "FAILED",
            analysis_status="READY" if index == 0 else "FAILED",
            is_current=True,
        )
        files.append(managed_file)
        revisions.append(revision)
    db.add_all([root, *files, *revisions])
    db.flush()

    anchor = {
        "resource_type": "MANAGED_SOURCE",
        "managed_file_id": files[0].id,
        "managed_file_revision_id": revisions[0].id,
        "filename": files[0].filename,
        "relative_path": files[0].relative_path,
        "relevance_tier": "SUPPORTED",
    }
    result = EventCollectionSearchService(
        db=db,
        workspace_id="shared-workspace",
        phrase_strategy=_PhraseStrategy(anchor),
        stage1_service=_Stage1(),
    ).search(
        original_query="找出2017年6月大数据联合实验室授牌相关的全部材料。",
        parsed_query=SimpleNamespace(year=2017, month=6),
        scope=SimpleNamespace(scope_mode="global"),
        request=EventCollectionRequest(
            subject_phrase="大数据联合实验室",
            action_phrases=("授牌", "揭牌"),
        ),
    )

    assert result["total_returned"] == 5
    assert result["partial"] is False
    returned_paths = [item["relative_path"] for item in result["results"]]
    assert returned_paths == sorted(relative_paths)
    assert any(path.endswith("IMG_0198.JPG") for path in returned_paths)
    assert all(item["working_copy_id"] is None for item in result["results"])
    assert all(item["resource_type"] == "MANAGED_SOURCE" for item in result["results"])
    failed = next(item for item in result["results"] if item["source_analysis_status"] == "FAILED")
    assert "仍可按受管目录关系" in failed["summary"]
    db.close()
