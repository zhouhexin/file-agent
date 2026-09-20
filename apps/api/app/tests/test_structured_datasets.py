"""共享累计表格 Demo 的端到端数据正确性、跨用户与旧事实保护测试。"""

import hashlib
import io
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from PIL import Image

from app.core.config import get_settings
from app.db.models import (
    ChangeItem, Document, DocumentVersion, DocumentPage, DocumentElement, DocumentExtractionRun,
    ManagedRoot, ManagedFile, WorkingCopyRoot, WorkingCopy, User,
    StructuredDatasetMember, StructuredExtractionRun, ToolInvocation,
)
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.tests.helpers import client_with_database, clear_overrides


BASE = "/api/structured-datasets"
FIELDS = [{"key": "name", "label": "姓名", "field_type": "string", "required": True},
          {"key": "amount", "label": "金额", "field_type": "decimal", "required": True}]


@pytest.fixture
def demo(monkeypatch, tmp_path):
    """使用隔离数据库和存储，不访问真实 OCR、LLM 或部署数据。"""
    monkeypatch.setenv("WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED", "true")
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    get_settings.cache_clear()
    client, sessions = client_with_database()
    # SQLite 默认关闭外键；显式开启，避免假测试掩盖真实版本和审计归属问题。
    with sessions.kw["bind"].connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    users = []
    for username in ("dataset-user-a", "dataset-user-b"):
        assert client.post("/api/auth/register", json={"username": username, "password": "password123", "display_name": username}).status_code < 300
        token = client.post("/api/auth/login", json={"username": username, "password": "password123"}).json()["access_token"]
        users.append({"Authorization": f"Bearer {token}"})
    yield client, sessions, users
    client.close()
    clear_overrides()


def seed_image(sessions, text, filename="demo.png"):
    """直接固定真实业务模型和解析事实，避免测试依赖 OCR 服务。"""
    image_buffer = io.BytesIO()
    Image.new("RGB", (8, 8), tuple(hashlib.sha256(text.encode()).digest()[:3])).save(image_buffer, format="PNG")
    content = image_buffer.getvalue()
    with sessions() as db:
        user = db.query(User).filter_by(username="dataset-user-a").one()
        workspace_id = get_shared_workspace_id(db)
        document = Document(user_id=user.id, workspace_id=workspace_id, original_filename=filename,
            content_type="image/png", size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        db.add(document)
        db.flush()
        version = DocumentVersion(document_id=document.id, storage_path=f"shared/{document.id}.png",
            filename=filename, content_type="image/png", size_bytes=123, sha256=document.sha256,
            storage_tier="WORKING_COPY", source_type="WORKING_COPY", created_by=user.id)
        root = ManagedRoot(root_key=str(uuid4()), display_name="测试原图", container_path="/test/source")
        db.add_all([version, root])
        db.flush()
        managed = ManagedFile(root_id=root.id, relative_path=filename, relative_path_hash=hashlib.sha256(filename.encode()).hexdigest(),
            filename=filename, extension=".png", size_bytes=123, content_sha256=document.sha256)
        working_root = WorkingCopyRoot(workspace_id=workspace_id, managed_root_id=root.id,
            root_key=str(uuid4()), relative_storage_path="shared", status="ACTIVE")
        db.add_all([managed, working_root])
        db.flush()
        working = WorkingCopy(working_copy_root_id=working_root.id, workspace_id=workspace_id,
            managed_file_id=managed.id, document_id=document.id, current_version_id=version.id,
            relative_path=version.storage_path, relative_path_hash=hashlib.sha256(version.storage_path.encode()).hexdigest(),
            filename=filename, extension=".png", size_bytes=123, content_sha256=document.sha256,
            imported_source_sha256=document.sha256, status="ACTIVE")
        extraction = DocumentExtractionRun(document_id=document.id, document_version_id=version.id,
            status="COMPLETED", extractor="workbuddy-external-ocr", parser_name="external-ocr", parser_version="1", parser_config_hash="f" * 64)
        db.add_all([working, extraction])
        db.flush()
        db.add(DocumentPage(document_id=document.id, extraction_run_id=extraction.id, page_number=1, text_content=text))
        image_path = FileLifecycleStorageService().working_copy_path(version.storage_path)
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(content)
        db.commit()
        return document.id


def seed_workbuddy_document(sessions, *, filename, content_type, units):
    """建立已由 WorkBuddy 验收并发布的共享工作副本，不调用服务器解析器。"""

    content = f"immutable:{filename}".encode()
    suffix = f".{filename.rsplit('.', 1)[-1].lower()}"
    with sessions() as db:
        user = db.query(User).filter_by(username="dataset-user-a").one()
        workspace_id = get_shared_workspace_id(db)
        source_hash = hashlib.sha256(content).hexdigest()
        document = Document(user_id=user.id, workspace_id=workspace_id, original_filename=filename,
            content_type=content_type, size_bytes=len(content), sha256=source_hash)
        db.add(document)
        db.flush()
        storage_path = f"shared/{document.id}{suffix}"
        version = DocumentVersion(document_id=document.id, storage_path=storage_path,
            filename=filename, content_type=content_type, size_bytes=len(content), sha256=source_hash,
            storage_tier="WORKING_COPY", source_type="WORKING_COPY", created_by=user.id,
            extraction_policy_json={"mode": "WORKBUDDY_DOCUMENT", "contract_version": "workbuddy-document-v1",
                "origin_task_id": str(uuid4()), "origin_upload_version_id": str(uuid4()), "source_sha256": source_hash})
        root = ManagedRoot(root_key=str(uuid4()), display_name="宿主文档", container_path="/test/workbuddy")
        db.add_all([version, root])
        db.flush()
        managed = ManagedFile(root_id=root.id, relative_path=filename,
            relative_path_hash=hashlib.sha256(filename.encode()).hexdigest(), filename=filename,
            extension=suffix, size_bytes=len(content), content_sha256=source_hash)
        working_root = WorkingCopyRoot(workspace_id=workspace_id, managed_root_id=root.id,
            root_key=str(uuid4()), relative_storage_path="shared", status="ACTIVE")
        db.add_all([managed, working_root])
        db.flush()
        working = WorkingCopy(working_copy_root_id=working_root.id, workspace_id=workspace_id,
            managed_file_id=managed.id, document_id=document.id, current_version_id=version.id,
            relative_path=storage_path, relative_path_hash=hashlib.sha256(storage_path.encode()).hexdigest(),
            filename=filename, extension=suffix, size_bytes=len(content), content_sha256=source_hash,
            imported_source_sha256=source_hash, status="ACTIVE")
        extraction = DocumentExtractionRun(document_id=document.id, document_version_id=version.id,
            status="COMPLETED", extractor="workbuddy-external-document", parser_name="workbuddy-test-reader",
            parser_version="1", parser_config_hash="e" * 64)
        db.add_all([working, extraction])
        db.flush()
        for order, unit in enumerate(units):
            external_unit_id = f"unit-{order}"
            page = DocumentPage(document_id=document.id, extraction_run_id=extraction.id,
                page_number=unit.get("page_number"), text_content=unit["text"], metadata_json={
                    "content_origin": "WORKBUDDY", "contract_version": "workbuddy-document-v1",
                    "source_unit_id": external_unit_id, "unit_order": order, "unit_kind": unit["kind"],
                    "unit_status": "READ", "coverage_status": "COMPLETE", "provider_name": "workbuddy-test-reader",
                    "coverage": {"status": "COMPLETE", "unit_count": len(units), "warnings": []},
                })
            element = DocumentElement(document_id=document.id, extraction_run_id=extraction.id,
                element_index=order, label="text", text_content=unit["text"],
                page_number=unit.get("page_number"), metadata_json={"source_unit_id": external_unit_id,
                    "start": 0, "end": len(unit["text"])})
            db.add_all([page, element])
        path = FileLifecycleStorageService().working_copy_path(storage_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        db.commit()
        return document.id


def create_dataset(client, headers, key="table-one"):
    """创建共享固定 schema。"""
    response = client.post(BASE, headers=headers, json={"name": "累计登记", "fields": FIELDS, "idempotency_key": key})
    assert response.status_code == 200, response.text
    return response.json()["dataset_id"]


def candidate(client, headers, document_id, name, amount, key="append-one", confidence=0.98):
    """严格使用 source 返回的版本，不猜测对象范围。"""
    response = client.get(f"{BASE}/sources/{document_id}", headers=headers)
    assert response.status_code == 200, response.text
    source = response.json()
    return {k: source[k] for k in ("document_id", "document_version_id", "extraction_run_id", "source_sha256")} | {
        "idempotency_key": key, "records": [{"fields": {
            "name": {"raw_text": name, "confidence": confidence},
            "amount": {"raw_text": amount, "confidence": confidence},
        }}],
    }


def workbuddy_candidate(client, headers, document_id, rows, *, key):
    """从正式 source_units 生成带稳定 UUID 和原文范围的字段证据。"""

    response = client.get(f"{BASE}/sources/{document_id}?content_origin=WORKBUDDY", headers=headers)
    assert response.status_code == 200, response.text
    source = response.json()
    records = []
    for unit_index, name, amount in rows:
        unit = source["source_units"][unit_index]
        fields = {}
        for field_key, raw_text in (("name", name), ("amount", amount)):
            start = unit["text"].index(raw_text)
            fields[field_key] = {"raw_text": raw_text, "confidence": 0.99, "page_number": unit["page_number"],
                "evidence_refs": [{"source_unit_id": unit["source_unit_id"], "quote": raw_text,
                    "start": start, "end": start + len(raw_text)}]}
        records.append({"fields": fields})
    return {k: source[k] for k in ("document_id", "document_version_id", "extraction_run_id", "source_sha256")} | {
        "content_origin": "WORKBUDDY", "idempotency_key": key, "records": records,
    }


def test_shared_append_query_export_and_idempotent_retry(demo):
    """A 创建、B 追加和下载；两图累计不重复，数字排序和全量合计正确。"""
    client, sessions, (user_a, user_b) = demo
    dataset_id = create_dataset(client, user_a)
    original_ids = []
    first_export = None
    for index, (name, amount) in enumerate((("张三", "20"), ("李四", "100")), 1):
        document_id = seed_image(sessions, f"姓名：{name}\n金额：{amount}")
        original_ids.append(document_id)
        request = candidate(client, user_b, document_id, name, amount, key=f"image-{index}")
        response = client.post(f"{BASE}/{dataset_id}/records", headers=user_b, json=request)
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == index
        repeat = client.post(f"{BASE}/{dataset_id}/records", headers=user_a, json=request)
        assert repeat.status_code == 200 and repeat.json()["reused"]
        assert repeat.json()["record_count"] == index
        if index == 1:
            response = client.post(f"{BASE}/{dataset_id}/exports", headers=user_a)
            assert response.status_code == 200, response.text
            first_export = response.json()
    query = client.post(f"{BASE}/{dataset_id}/query", headers=user_a, json={"sum_field": "amount", "limit": 1}).json()
    assert query["sum"] == "120" and query["matched_records"] == 2 and query["truncated"]
    filtered = client.post(f"{BASE}/{dataset_id}/query", headers=user_b,
        json={"filters": [{"field": "amount", "op": "gte", "value": 50}]}).json()
    assert filtered["matched_records"] == 1 and filtered["records"][0]["fields"]["name"]["value"] == "李四"
    exported = client.post(f"{BASE}/{dataset_id}/exports", headers=user_b)
    assert exported.status_code == 200, exported.text
    assert exported.json()["revision"] == 2 and exported.json()["export_id"] != first_export["export_id"]
    download = client.get(exported.json()["download_url"], headers=user_a)
    sheet = load_workbook(io.BytesIO(download.content)).active
    assert list(sheet.values) == [("姓名", "金额"), ("张三", "20"), ("李四", "100")]
    old = client.get(first_export["download_url"], headers=user_b)
    assert len(list(load_workbook(io.BytesIO(old.content)).active.values)) == 2
    assert client.post(f"{BASE}/{dataset_id}/exports", headers=user_a).json()["reused"]
    assert client.get(BASE, headers=user_b).json()["datasets"][0]["dataset_id"] == dataset_id
    with sessions() as db:
        assert db.query(StructuredDatasetMember).count() == 2
        assert db.query(StructuredExtractionRun).count() == 2
        assert db.query(ChangeItem).filter_by(change_type="STRUCTURED_DATASET_APPENDED").count() == 2
        assert db.query(ToolInvocation).filter_by(status="FAILED").count() == 0
        for document_id in original_ids:
            assert db.query(DocumentExtractionRun).filter_by(document_id=document_id, extractor="workbuddy-external-ocr").count() == 1
            version = db.query(DocumentVersion).filter_by(document_id=document_id).one()
            storage = FileLifecycleStorageService()
            assert storage.sha256_file(storage.working_copy_path(version.storage_path)) == version.sha256


def test_word_and_pdf_append_use_workbuddy_units_and_export_together(demo):
    """Word 空页码和 PDF 真实页码均只复用宿主全文，并累计到同一 Excel。"""

    client, sessions, (user_a, user_b) = demo
    dataset_id = create_dataset(client, user_a, key="mixed-documents")
    word_id = seed_workbuddy_document(sessions, filename="登记.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        units=[{"kind": "TEXT_SECTION", "page_number": None, "text": "姓名：样本丙 金额：50"},
               {"kind": "TEXT_SECTION", "page_number": None, "text": "姓名：样本丁 金额：150"}])
    pdf_id = seed_workbuddy_document(sessions, filename="登记.pdf", content_type="application/pdf",
        units=[{"kind": "PDF_PAGE", "page_number": 1, "text": "姓名：样本己 金额：80"},
               {"kind": "PDF_PAGE", "page_number": 2, "text": "姓名：样本庚 金额：120"}])

    word = workbuddy_candidate(client, user_b, word_id,
        [(0, "样本丙", "50"), (1, "样本丁", "150")], key="word-1")
    pdf = workbuddy_candidate(client, user_a, pdf_id,
        [(0, "样本己", "80"), (1, "样本庚", "120")], key="pdf-1")
    assert client.post(f"{BASE}/{dataset_id}/records", headers=user_b, json=word).status_code == 200
    assert client.post(f"{BASE}/{dataset_id}/records", headers=user_a, json=pdf).status_code == 200
    summary = client.get(f"{BASE}/{dataset_id}", headers=user_b).json()
    assert summary["document_count"] == 2
    assert summary["image_count"] == 0
    assert summary["format_counts"] == {"image": 0, "word": 1, "pdf": 1}
    query = client.post(f"{BASE}/{dataset_id}/query", headers=user_a,
        json={"sum_field": "amount", "limit": 10}).json()
    assert query["matched_records"] == 4 and query["sum"] == "400"
    exported = client.post(f"{BASE}/{dataset_id}/exports", headers=user_a).json()
    rows = list(load_workbook(io.BytesIO(client.get(exported["download_url"], headers=user_b).content)).active.values)
    assert rows == [("姓名", "金额"), ("样本丙", "50"), ("样本丁", "150"), ("样本己", "80"), ("样本庚", "120")]
    with sessions() as db:
        derived = db.query(DocumentElement).filter(DocumentElement.metadata_json.is_not(None)).all()
        traced = [element for element in derived if (element.metadata_json or {}).get("source_evidence_refs")]
        assert len(traced) == 4
        assert all(item["source_element_ids"] for element in traced
                   for item in element.metadata_json["source_evidence_refs"])


def test_invalid_evidence_is_retained_but_excluded_from_totals(demo):
    """找不到原值不得被当成成功数字；待复核记录仍可检查。"""
    client, sessions, (headers, _) = demo
    dataset_id = create_dataset(client, headers)
    document_id = seed_image(sessions, "张三 金额 20")
    request = candidate(client, headers, document_id, "张三", "9999")
    response = client.post(f"{BASE}/{dataset_id}/records", headers=headers, json=request)
    assert response.status_code == 200, response.text
    assert response.json()["review_field_count"] == 1
    query = client.post(f"{BASE}/{dataset_id}/query", headers=headers, json={"sum_field": "amount"}).json()
    assert query["review_records"] == 1 and query["matched_records"] == 0 and query["sum"] == "0"
    review = client.post(f"{BASE}/{dataset_id}/query", headers=headers, json={"include_review": True, "sum_field": "amount"}).json()
    assert len(review["records"]) == 1 and review["sum"] == "0"
    exported = client.post(f"{BASE}/{dataset_id}/exports", headers=headers).json()
    content = client.get(exported["download_url"], headers=headers).content
    assert len(list(load_workbook(io.BytesIO(content)).active.values)) == 1


def test_conflicts_keep_facts_atomic_and_record_failed_invocation(demo):
    """版本错误、同键异载荷和同版本新键均不能重复增加成员。"""
    client, sessions, (headers, _) = demo
    dataset_id = create_dataset(client, headers)
    document_id = seed_image(sessions, "张三 20")
    request = candidate(client, headers, document_id, "张三", "20")
    wrong = dict(request, source_sha256="0" * 64)
    failed = client.post(f"{BASE}/{dataset_id}/records", headers=headers, json=wrong)
    assert failed.status_code == 409 and failed.json()["error"]["code"] == "SOURCE_SNAPSHOT_CHANGED"
    assert client.get(f"{BASE}/{dataset_id}", headers=headers).json()["revision"] == 0
    assert client.post(f"{BASE}/{dataset_id}/records", headers=headers, json=request).status_code == 200
    failed = client.post(f"{BASE}/{dataset_id}/records", headers=headers, json=wrong)
    assert failed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    failed = client.post(f"{BASE}/{dataset_id}/records", headers=headers, json=dict(request, idempotency_key="new-key"))
    assert failed.json()["error"]["code"] == "SOURCE_ALREADY_APPENDED"
    with sessions() as db:
        assert db.query(StructuredDatasetMember).count() == 1
        assert db.query(StructuredExtractionRun).count() == 1
        assert db.query(ToolInvocation).filter_by(status="FAILED").count() == 3


def test_formula_protection_and_export_failure_does_not_undo_append(demo, monkeypatch):
    """复用现有安全 writer；导出失败后已提交记录和 revision 仍保留。"""
    client, sessions, (headers, _) = demo
    dataset_id = create_dataset(client, headers)
    document_id = seed_image(sessions, "=1+1 金额 20")
    request = candidate(client, headers, document_id, "=1+1", "20")
    assert client.post(f"{BASE}/{dataset_id}/records", headers=headers, json=request).status_code == 200
    from app.modules.structured_datasets import service
    writer = service._write_xlsx_atomic
    def failed_writer(**kwargs):
        """模拟磁盘失败，不写入真实外部目录。"""
        raise OSError("fake disk failure")
    monkeypatch.setattr(service, "_write_xlsx_atomic", failed_writer)
    assert client.post(f"{BASE}/{dataset_id}/exports", headers=headers).status_code == 500
    assert client.get(f"{BASE}/{dataset_id}", headers=headers).json()["record_count"] == 1
    monkeypatch.setattr(service, "_write_xlsx_atomic", writer)
    exported = client.post(f"{BASE}/{dataset_id}/exports", headers=headers).json()
    sheet = load_workbook(io.BytesIO(client.get(exported["download_url"], headers=headers).content)).active
    assert sheet["A2"].data_type == "s" and sheet["A2"].value == "'=1+1"


def test_feature_gate_and_strict_input(demo, monkeypatch):
    """保留登录和独立开关；不允许自由路径或未声明字段进入写入。"""
    client, _, (headers, _) = demo
    assert client.get(BASE).status_code == 401
    response = client.post(BASE, headers=headers, json={"name": "bad", "fields": FIELDS,
        "idempotency_key": "bad", "owner_user_id": "someone"})
    assert response.status_code == 422
    monkeypatch.setenv("WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED", "false")
    get_settings.cache_clear()
    assert client.get(BASE, headers=headers).status_code == 503


def test_unknown_confidence_and_type_conflict_are_reviewed(demo):
    """未提供评分和数字解析失败不得被默认当成高质量字段。"""
    client, sessions, (headers, _) = demo
    dataset_id = create_dataset(client, headers)
    document_id = seed_image(sessions, "张三 金额 未知")
    request = candidate(client, headers, document_id, "张三", "未知", confidence=None)
    response = client.post(f"{BASE}/{dataset_id}/records", headers=headers, json=request)
    assert response.status_code == 200, response.text
    assert response.json()["review_field_count"] == 2
    query = client.post(f"{BASE}/{dataset_id}/query", headers=headers, json={"include_review": True}).json()
    assert query["records"][0]["fields"]["amount"]["value"] is None


def test_date_filters_reuse_normalization_and_reject_invalid_date(demo):
    """日期比较先规范化，不能对任意输入做字典序比较。"""
    client, sessions, (headers, _) = demo
    response = client.post(BASE, headers=headers, json={"name": "日期登记", "idempotency_key": "dates",
        "fields": [{"key": "date", "label": "日期", "field_type": "date", "required": True}]})
    dataset_id = response.json()["dataset_id"]
    document_id = seed_image(sessions, "2026年9月18日")
    request = candidate(client, headers, document_id, "unused", "unused")
    request["records"] = [{"fields": {"date": {"raw_text": "2026年9月18日", "confidence": 0.98}}}]
    assert client.post(f"{BASE}/{dataset_id}/records", headers=headers, json=request).status_code == 200
    query = client.post(f"{BASE}/{dataset_id}/query", headers=headers,
        json={"filters": [{"field": "date", "op": "gte", "value": "2026年9月1日"}]})
    assert query.json()["matched_records"] == 1
    assert client.post(f"{BASE}/{dataset_id}/query", headers=headers,
        json={"filters": [{"field": "date", "op": "gte", "value": "昨天"}]}).status_code == 422


def test_demo_migration_roundtrip_leaves_existing_tables():
    """新迁移可独立升级回退，且不会删除旧的文件事实表。"""
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = Path(__file__).resolve().parents[2] / "alembic/versions/20260920_0001_create_structured_datasets.py"
    spec = importlib.util.spec_from_file_location("dataset_migration_test", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE documents (id TEXT PRIMARY KEY)")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        assert {"structured_datasets", "structured_dataset_members", "structured_dataset_exports"}.issubset(inspector.get_table_names())
        assert len(inspector.get_unique_constraints("structured_dataset_members")) == 2
        migration.downgrade()
        assert inspect(connection).get_table_names() == ["documents"]
    # 同时编译真实部署使用的 PostgreSQL DDL，不连接或变更部署数据库。
    sql = io.StringIO()
    migration.op = Operations(MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql}))
    migration.upgrade()
    assert "schema_json JSONB" in sql.getvalue()
    assert "TIMESTAMP WITH TIME ZONE" in sql.getvalue()
    assert "ON DELETE RESTRICT" in sql.getvalue()
