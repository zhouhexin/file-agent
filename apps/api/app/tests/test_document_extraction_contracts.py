"""WorkBuddy 完整文档提取 v1 的输入契约测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.external_extraction.document_schemas import DocumentExtractionSubmitRequest


def _payload(**overrides):
    """构造一份最小完整 Word 区段，所有测试只改变需要保护的边界。"""

    payload = {
        "worker_id": "workbuddy-document-worker",
        "lease_token": "x" * 32,
        "submission_key": "submission-1",
        "source_sha256": "a" * 64,
        "source_version_id": "12345678-1234-1234-1234-123456789012",
        "provider_name": "workbuddy-document-reader",
        "coverage": {"status": "COMPLETE", "unit_count": 1, "warnings": []},
        "units": [{
            "unit_id": "section-0",
            "kind": "TEXT_SECTION",
            "page_number": None,
            "order": 0,
            "status": "READ",
            "text": "姓名：样本甲\n金额：100",
            "blocks": [{
                "block_id": "paragraph-0",
                "kind": "PARAGRAPH",
                "text": "姓名：样本甲",
                "start": 0,
                "end": 6,
            }],
        }],
    }
    payload.update(overrides)
    return payload


def test_complete_word_section_keeps_null_page_number_and_real_block_range() -> None:
    """Word 没有可靠物理页码时必须保留 null，不能用内部容器编号冒充。"""

    request = DocumentExtractionSubmitRequest.model_validate(_payload())

    assert request.units[0].page_number is None
    assert request.units[0].blocks[0].text == "姓名：样本甲"


def test_rejects_fabricated_word_page_number() -> None:
    """宿主即使带回页码，也不能让 Word TEXT_SECTION 伪造可定位物理页。"""

    payload = _payload()
    payload["units"][0]["page_number"] = 1

    with pytest.raises(ValidationError, match="不能伪造 page_number"):
        DocumentExtractionSubmitRequest.model_validate(payload)


def test_rejects_block_slice_that_is_not_supported_by_unit_text() -> None:
    """字符范围和原文必须双向核对，防止模型借用别处文本作为字段证据。"""

    payload = _payload()
    payload["units"][0]["blocks"][0]["text"] = "虚构内容"

    with pytest.raises(ValidationError, match="字符范围与 unit 正文不一致"):
        DocumentExtractionSubmitRequest.model_validate(payload)


def test_partial_pdf_must_keep_known_page_units() -> None:
    """PARTIAL 可以保留失败页诊断，但 PDF 页号仍必须由上层任务范围校验。"""

    payload = _payload(
        coverage={"status": "PARTIAL", "unit_count": 2, "warnings": ["第二页不可读"]},
        units=[
            {
                "unit_id": "page-1", "kind": "PDF_PAGE", "page_number": 1, "order": 0,
                "status": "READ", "text": "第一页正文", "blocks": [],
            },
            {
                "unit_id": "page-2", "kind": "PDF_PAGE", "page_number": 2, "order": 1,
                "status": "FAILED", "text": "", "blocks": [],
                "error": {"code": "UNREADABLE", "message": "无法读取", "retryable": True},
            },
        ],
    )

    request = DocumentExtractionSubmitRequest.model_validate(payload)

    assert request.coverage.status == "PARTIAL"
    assert [unit.page_number for unit in request.units] == [1, 2]


def test_rejects_non_finite_bbox_and_duplicate_units() -> None:
    """bbox 与单位标识均为不可信宿主输入，必须拒绝非有限坐标和重复范围。"""

    payload = _payload()
    payload["units"].append({**payload["units"][0], "order": 1})
    payload["coverage"]["unit_count"] = 2

    with pytest.raises(ValidationError, match="unit_id 不能重复"):
        DocumentExtractionSubmitRequest.model_validate(payload)

    payload = _payload()
    payload["units"][0]["kind"] = "PDF_PAGE"
    payload["units"][0]["page_number"] = 1
    payload["units"][0]["blocks"][0]["bbox"] = {"left": 0, "top": 0, "right": float("nan"), "bottom": 1}

    with pytest.raises(ValidationError):
        DocumentExtractionSubmitRequest.model_validate(payload)


def test_rejects_duplicate_block_ids_across_units() -> None:
    """block_id 是文件级稳定定位，不能只在各 unit 内检查唯一性。"""

    payload = _payload(
        coverage={"status": "COMPLETE", "unit_count": 2, "warnings": []},
        units=[
            _payload()["units"][0],
            {
                **_payload()["units"][0],
                "unit_id": "section-1",
                "order": 1,
                "text": "姓名：样本乙",
                "blocks": [{"block_id": "paragraph-0", "kind": "PARAGRAPH",
                            "text": "姓名：样本乙", "start": 0, "end": 6}],
            },
        ],
    )

    with pytest.raises(ValidationError, match="文件内 block_id 不能重复"):
        DocumentExtractionSubmitRequest.model_validate(payload)


def test_document_extraction_migration_roundtrip() -> None:
    """新迁移可在隔离 SQLite 回退，并可编译 PostgreSQL JSONB DDL。"""

    import importlib.util
    import io
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = Path(__file__).resolve().parents[2] / "alembic/versions/20260920_0002_workbuddy_document_extraction.py"
    spec = importlib.util.spec_from_file_location("document_extraction_migration_test", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE document_versions (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE external_extraction_tasks (id TEXT PRIMARY KEY)")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert "extraction_policy_json" in {column["name"] for column in inspect(connection).get_columns("document_versions")}
        assert {"source_manifest_json", "result_summary_json"}.issubset(
            {column["name"] for column in inspect(connection).get_columns("external_extraction_tasks")}
        )
        migration.downgrade()
        assert {column["name"] for column in inspect(connection).get_columns("document_versions")} == {"id"}
        assert {column["name"] for column in inspect(connection).get_columns("external_extraction_tasks")} == {"id"}

    sql = io.StringIO()
    migration.op = Operations(MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql}
    ))
    migration.upgrade()
    assert sql.getvalue().count("JSONB") == 3
