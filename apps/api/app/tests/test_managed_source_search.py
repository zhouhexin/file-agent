"""受管原始文件双范围检索回归测试。

保护首次检索尚未物化文件时，正文和 Excel 单元格关键词仍能进入源侧候选；不能
因为文件级摘要没有同一词而漏掉原始文件。
"""

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    ManagedFile,
    ManagedFileAnalysisRun,
    ManagedFileRevision,
    ManagedFileSearchProfile,
    ManagedFileTextChunk,
    ManagedRoot,
)
from app.modules.chunks.tokenizer import ChineseLexicalTokenizer
from app.modules.retrieval.managed_source_search import ManagedSourceSearchService


def _session():
    """创建隔离 SQLite 会话，验证 PostgreSQL 外的确定性源侧评分降级。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_source_search_recalls_body_only_term_before_working_copy_materialization():
    """正文独有关键词必须召回源文件，并明确禁止未物化文件被直接打开。"""

    db = _session()
    root = ManagedRoot(
        id="source-search-root",
        root_key="source-search-root",
        display_name="测试源目录",
        container_path="/managed/source-search-root",
        enabled=True,
    )
    managed_file = ManagedFile(
        id="source-search-file",
        root_id=root.id,
        relative_path="表格/经费明细.xlsx",
        relative_path_hash="source-search-path",
        filename="经费明细.xlsx",
        extension=".xlsx",
        size_bytes=10,
        fingerprint="source-search-fingerprint",
        status="ACTIVE",
    )
    revision = ManagedFileRevision(
        id="source-search-revision",
        managed_file_id=managed_file.id,
        revision_number=1,
        size_bytes=10,
        quick_fingerprint=managed_file.fingerprint,
        content_sha256="a" * 64,
        status="READY",
        analysis_status="READY",
        is_current=True,
        analysis_document_id="source-search-document",
        analysis_document_version_id="source-search-version",
    )
    analysis = ManagedFileAnalysisRun(
        id="source-search-analysis",
        managed_file_revision_id=revision.id,
        status="COMPLETED",
    )
    profile = ManagedFileSearchProfile(
        managed_file_revision_id=revision.id,
        analysis_run_id=analysis.id,
        normalized_filename="经费明细xlsx",
        title=managed_file.filename,
        # 摘要故意不含“临时补助”，验证搜索不能只靠摘要候选。
        summary="本表记录经费核算情况。",
        search_text="经费 明细 核算",
        status="ACTIVE",
    )
    chunk = ManagedFileTextChunk(
        id="source-search-chunk",
        managed_file_revision_id=revision.id,
        document_chunk_id="source-search-document-chunk",
        chunk_index=0,
        sheet_name="明细",
        cell_range="B12",
        text_content="临时补助按照审核名单发放。",
        search_text="临时 补助 审核 名单 发放",
        token_count=5,
    )
    db.add_all([root, managed_file, revision, analysis, profile, chunk])
    db.flush()

    result = ManagedSourceSearchService(
        db=db,
        workspace_id="shared-workspace",
        tokenizer=ChineseLexicalTokenizer(),
    ).search(
        parsed_query=SimpleNamespace(cleaned="临时补助", terms=["临时", "补助"]),
        scope=SimpleNamespace(scope_mode="global"),
        exact_phrase="临时补助",
    )

    assert len(result["results"]) == 1
    item = result["results"][0]
    assert item["filename"] == "经费明细.xlsx"
    assert item["match_location"] == {"page_number": None, "sheet_name": "明细", "cell_range": "B12"}
    assert item["can_open"] is False
    assert item["_body_phrase_hit"] is True
    db.close()
