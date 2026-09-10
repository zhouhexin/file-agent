"""WorkBuddy/MCP 批量导入基础 API 回归测试。

这些测试保护 P1 的清单冻结、用户隔离和幂等边界；文件字节上传和后续工作流不在本阶段伪造成功。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.db.models import (
    Document,
    DocumentCategory,
    DocumentOrganizationDecision,
    DocumentVersion,
    FilesystemJob,
    IngestBatch,
    IngestDuplicateGroup,
    IngestItem,
    IngestRequestExecution,
    IntegrationRequest,
    UploadArchiveRecord,
    UploadDuplicateReview,
    WorkingCopy,
    utcnow,
)
from app.modules.managed_files.worker import process_next_filesystem_job
from app.tests.helpers import clear_overrides, client_with_database


@pytest.fixture(autouse=True)
def _enable_integration_channel(monkeypatch):
    """集成测试显式打开默认关闭的试点通道，并在用例后清理配置缓存。"""

    monkeypatch.setenv("INTEGRATION_INGEST_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _register_and_login(client, username: str) -> tuple[str, str]:
    """注册隔离用户并返回用户 ID 和访问令牌。"""

    registered = client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    logged_in = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return registered.json()["id"], logged_in.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    """构造 Bearer 认证头。"""

    return {"Authorization": f"Bearer {token}"}


def _batch_payload(*, key: str = "batch-event-1", directory: str = "待导入") -> dict:
    """生成默认按主分类落位的批次创建请求。"""

    return {
        "client_id": "workbuddy-local",
        "request_id": "request-001",
        "idempotency_key": key,
        "source_root_ref": "local-materials",
        "relative_directory": directory,
        "recursive": True,
        "ingest_policy": "AUTO_ORGANIZE",
        "placement_mode": "BY_CATEGORY",
        "rule_profile": "content_based",
        "user_request": None,
    }


def _item_payload(index: int, *, size_bytes: int | None = None) -> dict:
    """生成位于已授权批次目录中的稳定清单项。"""

    filename = f"材料-{index}.txt"
    return {
        "client_item_id": f"item-{index}",
        "source_root_ref": "local-materials",
        "source_relative_path": f"待导入/{filename}",
        "original_filename": filename,
        "size_bytes": size_bytes if size_bytes is not None else index + 10,
        "mtime_ns": 1_000_000 + index,
        "expected_sha256": f"{index:064x}",
    }


def _create_sealed_content_item(client, token: str, *, content: bytes) -> tuple[str, str, dict]:
    """创建带真实大小和哈希的单文件 seal 批次，供内容接收测试复用。"""

    batch_id = client.post(
        "/api/integrations/v1/ingest-batches",
        headers=_headers(token),
        json=_batch_payload(key=f"content-{hashlib.sha256(content).hexdigest()}"),
    ).json()["id"]
    item_payload = _item_payload(1, size_bytes=len(content))
    item_payload["expected_sha256"] = hashlib.sha256(content).hexdigest()
    item_id = client.post(
        f"/api/integrations/v1/ingest-batches/{batch_id}/items",
        headers=_headers(token),
        json={"items": [item_payload]},
    ).json()["items"][0]["id"]
    client.post(
        f"/api/integrations/v1/ingest-batches/{batch_id}/seal",
        headers=_headers(token),
    )
    return batch_id, item_id, item_payload


def _create_published_item_with_pending_request(
    *,
    client,
    SessionLocal,
    token: str,
    key: str,
) -> tuple[str, str, str]:
    """完成真实文件发布并留下尚未领取的附带请求，供发布边界测试复用。"""

    content = f"{key} 学校会议纪要与资助材料".encode()
    payload = _batch_payload(key=key)
    payload["user_request"] = "读取并总结这些文件"
    batch_id = client.post(
        "/api/integrations/v1/ingest-batches",
        headers=_headers(token),
        json=payload,
    ).json()["id"]
    item_payload = _item_payload(1, size_bytes=len(content))
    item_payload["expected_sha256"] = hashlib.sha256(content).hexdigest()
    item_id = client.post(
        f"/api/integrations/v1/ingest-batches/{batch_id}/items",
        headers=_headers(token),
        json={"items": [item_payload]},
    ).json()["items"][0]["id"]
    client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=_headers(token))
    client.put(
        f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
        headers=_headers(token),
        files={"file": (item_payload["original_filename"], content, "text/plain")},
    )
    processed = 0
    while process_next_filesystem_job(
        session_factory=SessionLocal,
        worker_id="published-boundary-files",
        queue_names={"DUPLICATE_CHECK", "ARCHIVE", "IMPORT", "FILE_OPERATION", "ANALYSIS"},
    ):
        processed += 1
        assert processed < 12
    batch = client.get(
        f"/api/integrations/v1/ingest-batches/{batch_id}",
        headers=_headers(token),
    ).json()
    execution = batch["receipt"]["request_executions"][0]
    assert execution["status"] == "PENDING"
    # 对外回执不暴露内部队列 ID；测试从持久化审计记录核对任务边界。
    with SessionLocal() as db:
        request_job_id = db.query(IngestRequestExecution).one().filesystem_job_id
    assert request_job_id
    return batch_id, item_id, request_job_id


def test_batch_create_requires_authentication() -> None:
    """外部集成接口不能接受匿名用户或客户端自报 user_id。"""

    client, _ = client_with_database()
    try:
        response = client.post("/api/integrations/v1/ingest-batches", json=_batch_payload())

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
    finally:
        clear_overrides()


def test_integration_channel_is_closed_when_trial_flag_is_disabled(monkeypatch) -> None:
    """默认关闭的试点开关必须在后端路由边界拒绝请求，不能只隐藏 MCP 工具。"""

    monkeypatch.setenv("INTEGRATION_INGEST_ENABLED", "false")
    get_settings.cache_clear()
    client, _SessionLocal = client_with_database()
    response = client.post("/api/integrations/v1/ingest-batches", json=_batch_payload())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "INTEGRATION_INGEST_DISABLED"


def test_create_batch_is_idempotent_and_freezes_default_policy() -> None:
    """同一幂等事件只创建一个批次和一条外部请求审计。"""

    client, SessionLocal = client_with_database()
    try:
        user_id, token = _register_and_login(client, "ingest-create-user")
        first = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(),
        )
        replay = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(),
        )

        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.json()["id"] == first.json()["id"]
        assert first.json()["policy"] == {
            "source_root_ref": "local-materials",
            "relative_directory": "待导入",
            "recursive": True,
            "ingest_policy": "AUTO_ORGANIZE",
            "placement_mode": "BY_CATEGORY",
            "rule_profile": "content_based",
        }
        assert first.json()["user_request"] is None
        assert first.json()["counts"]["total"] == 0

        with SessionLocal() as db:
            batch = db.query(IngestBatch).one()
            audit = db.query(IntegrationRequest).one()
            assert batch.user_id == user_id
            assert audit.target_refs_json == {"batch_id": batch.id}
            assert audit.status == "COMPLETED"
    finally:
        clear_overrides()


def test_same_idempotency_key_with_different_payload_is_rejected() -> None:
    """同键不同目录不能悄悄复用第一次创建的批次。"""

    client, _ = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-conflict-user")
        first = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(),
        )
        conflict = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(directory="另一目录"),
        )

        assert first.status_code == 201
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    finally:
        clear_overrides()


def test_append_items_is_per_item_idempotent_and_seal_freezes_manifest() -> None:
    """分页重试不新增副本，seal 后任何追加都必须关闭式拒绝。"""

    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-items-user")
        created = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(),
        ).json()
        batch_id = created["id"]
        page = {"items": [_item_payload(1), _item_payload(2)]}

        appended = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json=page,
        )
        replay = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json=page,
        )
        sealed = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/seal",
            headers=_headers(token),
        )
        sealed_replay = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/seal",
            headers=_headers(token),
        )
        rejected = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(3)]},
        )

        assert appended.status_code == 200
        assert appended.json()["created_count"] == 2
        assert appended.json()["reused_count"] == 0
        assert appended.json()["batch"]["counts"]["pending"] == 2
        assert replay.json()["created_count"] == 0
        assert replay.json()["reused_count"] == 2
        assert replay.json()["batch"]["manifest_revision"] == appended.json()["batch"]["manifest_revision"]
        assert sealed.json()["manifest_status"] == "SEALED"
        assert sealed_replay.json()["manifest_revision"] == sealed.json()["manifest_revision"]
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "MANIFEST_SEALED"
        with SessionLocal() as db:
            assert db.query(IngestItem).count() == 2
    finally:
        clear_overrides()


def test_item_replay_with_changed_snapshot_is_rejected() -> None:
    """相同 client_item_id 的大小、时间、路径或哈希变化都属于幂等冲突。"""

    client, _ = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-item-conflict")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(),
        ).json()["id"]
        first = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(1)]},
        )
        changed = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(1, size_bytes=999)]},
        )

        assert first.status_code == 200
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    finally:
        clear_overrides()


def test_item_scope_and_path_traversal_are_rejected() -> None:
    """MCP 不能借清单项切换逻辑根、越出指定目录或提交上级路径。"""

    client, _ = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-scope-user")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(),
        ).json()["id"]
        wrong_root = _item_payload(1)
        wrong_root["source_root_ref"] = "other-root"
        outside_directory = _item_payload(2)
        outside_directory["source_relative_path"] = "其他目录/材料-2.txt"
        traversal = _item_payload(3)
        traversal["source_relative_path"] = "待导入/../材料-3.txt"

        wrong_root_response = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [wrong_root]},
        )
        outside_response = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [outside_directory]},
        )
        traversal_response = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [traversal]},
        )

        assert wrong_root_response.status_code == 409
        assert wrong_root_response.json()["error"]["code"] == "MANIFEST_SCOPE_MISMATCH"
        assert outside_response.status_code == 409
        assert outside_response.json()["error"]["code"] == "MANIFEST_SCOPE_MISMATCH"
        assert traversal_response.status_code == 422
        assert traversal_response.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        clear_overrides()


def test_items_are_paginated_and_other_users_cannot_read_batch() -> None:
    """恢复接口使用稳定游标，并隐藏其他用户批次是否存在。"""

    client, _ = client_with_database()
    try:
        _, owner_token = _register_and_login(client, "ingest-owner")
        _, other_token = _register_and_login(client, "ingest-other")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(owner_token),
            json=_batch_payload(),
        ).json()["id"]
        client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(owner_token),
            json={"items": [_item_payload(1), _item_payload(2), _item_payload(3)]},
        )

        first_page = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items?limit=2",
            headers=_headers(owner_token),
        )
        next_cursor = first_page.json()["next_cursor"]
        second_page = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items?limit=2&cursor={next_cursor}",
            headers=_headers(owner_token),
        )
        denied = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(other_token),
        )

        assert first_page.status_code == 200
        assert len(first_page.json()["items"]) == 2
        pending_item = first_page.json()["items"][0]
        assert pending_item["ingest_final_filename"] is None
        assert pending_item["current_filename"] is None
        assert pending_item["current_file_status"] == "NOT_PUBLISHED"
        assert pending_item["current_file_available"] is False
        assert pending_item["current_working_copy_revision"] is None
        assert pending_item["current_document_version_id"] is None
        assert next_cursor
        assert second_page.status_code == 200
        assert len(second_page.json()["items"]) == 1
        assert second_page.json()["next_cursor"] is None
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "INGEST_BATCH_NOT_FOUND"
    finally:
        clear_overrides()


def test_batch_counts_keep_waiting_cancelled_and_failed_separate() -> None:
    """批次回执统计不能把等待确认或用户取消误算成失败。"""

    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-counts-user")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(),
        ).json()["id"]
        client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(index) for index in range(1, 6)]},
        )
        with SessionLocal() as db:
            items = db.query(IngestItem).filter(IngestItem.batch_id == batch_id).order_by(IngestItem.id).all()
            for item, status in zip(
                items,
                ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED", "WAITING_DUPLICATE_CONFIRMATION", "FAILED"],
                strict=True,
            ):
                item.status = status
            db.commit()

        response = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(token),
        )

        assert response.status_code == 200
        counts = response.json()["counts"]
        assert counts["total"] == 5
        assert counts["succeeded"] == 3
        assert counts["waiting_duplicate_confirmation"] == 1
        assert counts["failed"] == 1
        assert counts["cancelled"] == 0
        assert response.json()["status"] == "PARTIAL"
        assert response.json()["display_status"] == "FILE_PROCESSING_COMPLETED"
        assert response.json()["final_receipt_ready"] is True
        assert response.json()["receipt"]["title"] == "文件处理完成"
    finally:
        clear_overrides()


def test_extra_summary_request_is_scheduled_once_for_ready_files_while_duplicate_waits() -> None:
    """没有运行项时，已整理文件应执行附带总结，重复待确认不能阻塞且刷新不能重复排队。"""

    client, SessionLocal = client_with_database()
    try:
        user_id, token = _register_and_login(client, "ingest-extra-request")
        payload = _batch_payload(key="extra-summary")
        payload["user_request"] = "请总结这些文件"
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=payload,
        ).json()["id"]
        created = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(1), _item_payload(2)]},
        ).json()["items"]
        with SessionLocal() as db:
            batch = db.get(IngestBatch, batch_id)
            document = Document(
                user_id=user_id,
                workspace_id=batch.workspace_id,
                original_filename="已整理材料.txt",
                content_type="text/plain",
                size_bytes=8,
                sha256="a" * 64,
                status="WORKING_COPY",
                ingest_status="INDEXED",
            )
            db.add(document)
            db.flush()
            final_document_id = document.id
            ready = db.get(IngestItem, created[0]["id"])
            ready.status = "SUCCEEDED"
            ready.final_document_id = final_document_id
            waiting = db.get(IngestItem, created[1]["id"])
            waiting.status = "WAITING_DUPLICATE_CONFIRMATION"
            db.commit()

        first = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}", headers=_headers(token)
        )
        second = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}", headers=_headers(token)
        )

        assert first.json()["status"] == "PARTIAL"
        assert first.json()["final_receipt_ready"] is True
        with SessionLocal() as db:
            execution = db.query(IngestRequestExecution).one()
            job = db.get(FilesystemJob, execution.filesystem_job_id)
            assert execution.item_ids_json == [created[0]["id"]]
            assert execution.document_ids_json == [final_document_id]
            assert job.job_type == "RUN_INGEST_EXTRA_REQUEST"
            assert db.query(IngestRequestExecution).count() == 1
        assert len(second.json()["receipt"]["request_executions"]) == 1
    finally:
        clear_overrides()


def test_per_file_summary_creates_one_fixed_execution_for_each_ready_file() -> None:
    """“逐份总结”必须逐文件固化执行范围，不能退化为一次多附件汇总。"""

    client, SessionLocal = client_with_database()
    try:
        user_id, token = _register_and_login(client, "ingest-per-file-summary")
        payload = _batch_payload(key="per-file-summary")
        payload["user_request"] = "请分别总结每个文件"
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=payload,
        ).json()["id"]
        items = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(1), _item_payload(2)]},
        ).json()["items"]
        with SessionLocal() as db:
            batch = db.get(IngestBatch, batch_id)
            for index, item_response in enumerate(items, start=1):
                document = Document(
                    user_id=user_id,
                    workspace_id=batch.workspace_id,
                    original_filename=f"已整理材料-{index}.txt",
                    content_type="text/plain",
                    size_bytes=8,
                    sha256=f"{index:064x}",
                    status="WORKING_COPY",
                    ingest_status="INDEXED",
                )
                db.add(document)
                db.flush()
                item = db.get(IngestItem, item_response["id"])
                item.status = "SUCCEEDED"
                item.stage = "COMPLETED"
                item.final_document_id = document.id
            db.commit()

        response = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(token),
        )

        assert response.status_code == 200
        with SessionLocal() as db:
            executions = db.query(IngestRequestExecution).order_by(IngestRequestExecution.created_at).all()
            assert len(executions) == 2
            assert all(len(execution.item_ids_json) == 1 for execution in executions)
            assert all(len(execution.document_ids_json) == 1 for execution in executions)
            assert db.query(FilesystemJob).filter_by(job_type="RUN_INGEST_EXTRA_REQUEST").count() == 2
    finally:
        clear_overrides()


@pytest.mark.parametrize(
    "request_text",
    ["请帮我分类并整理这些文件", "请按照系统默认分类这批文件"],
)
def test_classification_only_request_reuses_default_organization_without_agent_job(
    request_text: str,
) -> None:
    """用户附带分类文字等同默认整理，不得再次创建分类 AgentRun。"""

    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-default-classification")
        payload = _batch_payload(key="default-classification")
        payload["user_request"] = request_text
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=payload,
        ).json()["id"]
        item_id = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(1)]},
        ).json()["items"][0]["id"]
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            item.status = "FAILED"
            db.commit()

        client.get(f"/api/integrations/v1/ingest-batches/{batch_id}", headers=_headers(token))
        with SessionLocal() as db:
            assert db.query(IngestRequestExecution).count() == 0
            assert db.query(FilesystemJob).filter_by(job_type="RUN_INGEST_EXTRA_REQUEST").count() == 0
    finally:
        clear_overrides()


def test_ready_batch_executes_attached_read_request_and_restores_receipt(monkeypatch, tmp_path) -> None:
    """文件分类、命名和索引完成后，应自动执行只读请求并由 batch_get 恢复结果。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "false")
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = "学校会议纪要。会议决定开展新生资助材料复核。".encode()
    try:
        _, token = _register_and_login(client, "ingest-read-request")
        payload = _batch_payload(key="attached-read-request")
        payload["user_request"] = "读取并总结这些文件"
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches", headers=_headers(token), json=payload
        ).json()["id"]
        item_payload = _item_payload(1, size_bytes=len(content))
        item_payload["expected_sha256"] = hashlib.sha256(content).hexdigest()
        item_id = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [item_payload]},
        ).json()["items"][0]["id"]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=_headers(token))
        client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": (item_payload["original_filename"], content, "text/plain")},
        )
        processed = 0
        while process_next_filesystem_job(session_factory=SessionLocal, worker_id="attached-request-files"):
            processed += 1
            assert processed < 12

        scheduled = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}", headers=_headers(token)
        ).json()
        assert scheduled["receipt"]["request_executions"][0]["status"] == "PENDING"
        assert process_next_filesystem_job(
            session_factory=SessionLocal,
            worker_id="attached-request-agent",
            queue_names={"AGENT"},
        )
        restored = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}", headers=_headers(token)
        ).json()

        execution = restored["receipt"]["request_executions"][0]
        assert execution["status"] == "COMPLETED"
        assert execution["agent_run_id"]
        assert execution["result"]["final_response"]
        assert restored["result_revision"] > scheduled["result_revision"]
    finally:
        clear_overrides()


def test_failed_receive_requires_explicit_retry_and_returns_to_original_manifest() -> None:
    """显式重试只能重开失败条目，且继续使用原清单而不是创建新成员。"""

    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-retry-item")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="retry-item"),
        ).json()["id"]
        item_id = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(1)]},
        ).json()["items"][0]["id"]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=_headers(token))
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            item.status = "FAILED"
            item.stage = "RECEIVE"
            item.error_json = {"code": "SOURCE_SNAPSHOT_CHANGED"}
            db.commit()
        payload = {
            "client_id": "workbuddy-local",
            "request_id": "retry-request",
            "idempotency_key": "retry-event",
            "reason": "已确认本地文件恢复",
        }
        first = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/retry",
            headers=_headers(token),
            json=payload,
        )
        replay = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/retry",
            headers=_headers(token),
            json=payload,
        )

        assert first.status_code == 202
        assert first.json()["item"]["status"] == "PENDING"
        assert first.json()["item"]["stage"] == "RECEIVE"
        assert replay.json()["reused"] is True
        resume = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/resume",
            headers=_headers(token),
        ).json()
        assert resume["receivable_item_ids"] == [item_id]
    finally:
        clear_overrides()


def test_failed_archive_retry_restores_lifecycle_state() -> None:
    """显式重试归档失败项时必须同时恢复归档投影，避免新任务被 FAILED 状态再次拒绝。"""

    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-retry-archive-state")
        content = "归档重试状态验证".encode()
        batch_id, item_id, _ = _create_sealed_content_item(client, token, content=content)
        client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": ("材料-1.txt", content, "text/plain")},
        )
        assert process_next_filesystem_job(
            session_factory=SessionLocal,
            worker_id="retry-archive-duplicate-check",
            queue_names={"DUPLICATE_CHECK"},
        )
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            archive = db.get(UploadArchiveRecord, item.archive_record_id)
            job = db.get(FilesystemJob, archive.filesystem_job_id)
            archive.status = "FAILED"
            archive.last_error_code = "FILESYSTEM_JOB_FAILED"
            archive.last_error_message = "归档失败"
            job.status = "FAILED"
            job.error_message = "归档失败"
            item.status = "FAILED"
            item.current_job_id = job.id
            db.commit()

        response = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/retry",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "retry-archive-state-request",
                "idempotency_key": "retry-archive-state-event",
                "reason": "重试归档",
            },
        )

        assert response.status_code == 202, response.text
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            archive = db.get(UploadArchiveRecord, item.archive_record_id)
            assert archive.status == "RETRY_WAIT"
            assert archive.last_error_code is None
            assert archive.last_error_message is None
    finally:
        clear_overrides()


def test_failed_analysis_is_projected_and_can_be_retried(monkeypatch, tmp_path) -> None:
    """分析任务失败必须结束“处理中”假象，并把显式重试精确绑定到分析阶段。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "false")
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-analysis-failure-projection")
        content = "分析失败状态投影测试".encode()
        batch_id, item_id, payload = _create_sealed_content_item(client, token, content=content)
        client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )
        for queue_name in ("DUPLICATE_CHECK", "ARCHIVE", "IMPORT"):
            assert process_next_filesystem_job(
                session_factory=SessionLocal,
                worker_id="analysis-failure-projection",
                queue_names={queue_name},
            )
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            archive = db.get(UploadArchiveRecord, item.archive_record_id)
            import_job = db.get(FilesystemJob, archive.filesystem_job_id)
            analysis_job = db.get(FilesystemJob, import_job.result_json["analysis_job_id"])
            analysis_job.status = "FAILED"
            analysis_job.error_message = "分析失败"
            analysis_job_id = analysis_job.id
            db.commit()

        projected = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(token),
        )

        assert projected.status_code == 200
        assert projected.json()["counts"]["failed"] == 1
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            assert item.status == "FAILED"
            assert item.current_job_id == analysis_job_id
    finally:
        clear_overrides()


def test_failed_working_copy_import_is_projected(monkeypatch, tmp_path) -> None:
    """归档成功后的工作副本导入失败必须投影为失败，不能永久停在整理中。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-import-failure-projection")
        content = "工作副本导入失败状态投影".encode()
        batch_id, item_id, payload = _create_sealed_content_item(client, token, content=content)
        client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )
        for queue_name in ("DUPLICATE_CHECK", "ARCHIVE"):
            assert process_next_filesystem_job(
                session_factory=SessionLocal,
                worker_id="import-failure-projection",
                queue_names={queue_name},
            )
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            archive = db.get(UploadArchiveRecord, item.archive_record_id)
            import_job = db.get(FilesystemJob, archive.filesystem_job_id)
            import_job.status = "FAILED"
            import_job.error_message = "工作副本导入失败"
            import_job_id = import_job.id
            db.commit()

        projected = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}", headers=_headers(token)
        )
        assert projected.json()["counts"]["failed"] == 1
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            assert item.current_job_id == import_job_id
            assert item.status == "FAILED"
    finally:
        clear_overrides()


def test_cancel_pending_item_is_idempotent_and_does_not_affect_other_members() -> None:
    """取消单个未传输文件必须保持逐项隔离，并用幂等记录恢复相同响应。"""

    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-cancel-item")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="cancel-item"),
        ).json()["id"]
        items = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(1), _item_payload(2)]},
        ).json()["items"]
        payload = {
            "client_id": "workbuddy-local",
            "request_id": "cancel-request",
            "idempotency_key": "cancel-event",
            "reason": "用户主动取消",
        }
        first = client.post(
            f"/api/integrations/v1/ingest-items/{items[0]['id']}/cancel",
            headers=_headers(token),
            json=payload,
        )
        replay = client.post(
            f"/api/integrations/v1/ingest-items/{items[0]['id']}/cancel",
            headers=_headers(token),
            json=payload,
        )

        assert first.status_code == 202
        assert first.json()["item"]["status"] == "CANCELLED"
        assert replay.json()["reused"] is True
        page = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
        ).json()["items"]
        by_id = {item["id"]: item for item in page}
        assert by_id[items[1]["id"]]["status"] == "PENDING"
    finally:
        clear_overrides()


def test_cancel_received_item_releases_staged_document_quota(monkeypatch, tmp_path) -> None:
    """已接收但尚未发布的文件取消后必须标记暂存 Document，避免清理后仍占用户容量。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = b"cancel received upload"
    try:
        _, token = _register_and_login(client, "ingest-cancel-received")
        batch_id, item_id, payload = _create_sealed_content_item(client, token, content=content)
        uploaded = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )
        assert uploaded.status_code == 202
        cancelled = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/cancel",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "cancel-received-request",
                "idempotency_key": "cancel-received-event",
            },
        )
        assert cancelled.status_code == 202
        assert cancelled.json()["item"]["status"] == "CANCELLED"
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            version = db.get(DocumentVersion, item.upload_document_version_id)
            document = db.get(Document, version.document_id)
            assert document.status == "UPLOAD_CANCELLED"
    finally:
        clear_overrides()


def test_cancel_after_publish_only_cancels_pending_attached_request(monkeypatch, tmp_path) -> None:
    """文件已经发布后必须保留成功结果，只能取消尚未领取的总结等附带请求。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "false")
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-cancel-after-publish")
        _batch_id, item_id, request_job_id = _create_published_item_with_pending_request(
            client=client,
            SessionLocal=SessionLocal,
            token=token,
            key="cancel-after-publish",
        )
        response = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/cancel",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "cancel-published-request",
                "idempotency_key": "cancel-published-event",
                "reason": "不再需要总结",
            },
        )

        assert response.status_code == 202, response.text
        assert response.json()["item"]["status"] == "SUCCEEDED"
        assert response.json()["item"]["final_working_copy_id"]
        with SessionLocal() as db:
            execution = db.query(IngestRequestExecution).one()
            job = db.get(FilesystemJob, request_job_id)
            assert execution.status == "CANCELLED"
            assert job.status == "CANCELLED"
            assert db.get(IngestItem, item_id).status == "SUCCEEDED"
    finally:
        clear_overrides()


def test_retry_after_publish_reopens_only_failed_attached_request(monkeypatch, tmp_path) -> None:
    """整理成功但总结失败时，显式重试不得重新执行文件归档、分类或命名。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "false")
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-retry-after-publish")
        _batch_id, item_id, request_job_id = _create_published_item_with_pending_request(
            client=client,
            SessionLocal=SessionLocal,
            token=token,
            key="retry-after-publish",
        )
        with SessionLocal() as db:
            execution = db.query(IngestRequestExecution).one()
            execution.status = "FAILED"
            execution.error_json = {"code": "EXTRA_REQUEST_FAILED"}
            job = db.get(FilesystemJob, request_job_id)
            job.status = "FAILED"
            job.error_message = "deterministic failure"
            db.commit()
        response = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/retry",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "retry-published-request",
                "idempotency_key": "retry-published-event",
                "reason": "重试附带总结",
            },
        )

        assert response.status_code == 202, response.text
        assert response.json()["item"]["status"] == "SUCCEEDED"
        assert response.json()["filesystem_job_id"] == request_job_id
        with SessionLocal() as db:
            execution = db.query(IngestRequestExecution).one()
            job = db.get(FilesystemJob, request_job_id)
            assert execution.status == "PENDING"
            assert job.status == "PENDING"
            # 原批次只有一个附带请求执行和一个最终工作副本，不因重试产生第二份文件。
            assert db.query(IngestRequestExecution).count() == 1
            assert db.query(WorkingCopy).count() == 1
    finally:
        clear_overrides()


def test_item_content_upload_stages_file_and_starts_processing(monkeypatch, tmp_path) -> None:
    """MCP 文件字节接收后必须自动进入查重任务，不再等待额外聊天文字。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = b"student scholarship material"
    try:
        _, token = _register_and_login(client, "ingest-content-user")
        batch_id, item_id, payload = _create_sealed_content_item(client, token, content=content)

        response = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )

        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] is True
        assert body["reused"] is False
        assert body["item"]["status"] == "RUNNING"
        assert body["item"]["stage"] == "EXACT_CHECK"
        assert body["item"]["actual_sha256"] == hashlib.sha256(content).hexdigest()
        assert body["filesystem_job_id"]
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            assert item is not None
            assert item.upload_document_version_id
            assert item.archive_record_id
            assert item.current_job_id == body["filesystem_job_id"]
            assert db.query(Document).count() == 1
            assert db.query(DocumentVersion).count() == 1
            assert db.query(FilesystemJob).count() == 1
    finally:
        clear_overrides()


def test_item_content_retry_reuses_existing_version_and_job(monkeypatch, tmp_path) -> None:
    """网络重试不能重复创建 Document、DocumentVersion 或后台任务。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = b"same upload retry"
    try:
        _, token = _register_and_login(client, "ingest-retry-user")
        batch_id, item_id, payload = _create_sealed_content_item(client, token, content=content)
        endpoint = f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content"
        first = client.put(
            endpoint,
            headers=_headers(token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )
        replay = client.put(
            endpoint,
            headers=_headers(token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )

        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.json()["reused"] is True
        assert replay.json()["filesystem_job_id"] == first.json()["filesystem_job_id"]
        with SessionLocal() as db:
            assert db.query(Document).count() == 1
            assert db.query(DocumentVersion).count() == 1
            assert db.query(FilesystemJob).count() == 1
    finally:
        clear_overrides()


def test_item_content_snapshot_mismatch_is_cleaned_and_recorded(monkeypatch, tmp_path) -> None:
    """来源文件变化时不得留下 Document 或暂存字节，只保留逐文件失败事实。"""

    storage_root = tmp_path / "storage"
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(storage_root))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    expected = b"expected bytes"
    actual = b"changed source bytes"
    try:
        _, token = _register_and_login(client, "ingest-mismatch-user")
        batch_id, item_id, payload = _create_sealed_content_item(client, token, content=expected)
        response = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": (payload["original_filename"], actual, "text/plain")},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SOURCE_SNAPSHOT_CHANGED"
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            assert item is not None
            assert item.status == "FAILED"
            assert item.upload_document_version_id is None
            assert db.query(Document).count() == 0
            assert db.query(FilesystemJob).count() == 0
        assert not list(storage_root.rglob("*.txt"))
    finally:
        clear_overrides()


def test_item_content_requires_sealed_owned_batch(monkeypatch, tmp_path) -> None:
    """未 seal 清单和其他用户条目都不能接收内容。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, _ = client_with_database()
    content = b"protected content"
    try:
        _, owner_token = _register_and_login(client, "ingest-content-owner")
        _, other_token = _register_and_login(client, "ingest-content-other")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(owner_token),
            json=_batch_payload(key="unsealed-content"),
        ).json()["id"]
        payload = _item_payload(1, size_bytes=len(content))
        payload["expected_sha256"] = hashlib.sha256(content).hexdigest()
        item_id = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(owner_token),
            json={"items": [payload]},
        ).json()["items"][0]["id"]

        unsealed = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(owner_token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )
        denied = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(other_token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )

        assert unsealed.status_code == 409
        assert unsealed.json()["error"]["code"] == "MANIFEST_NOT_SEALED"
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "INGEST_BATCH_NOT_FOUND"
    finally:
        clear_overrides()


def test_manifest_enforces_batch_file_byte_and_user_quota_limits(monkeypatch) -> None:
    """服务端必须独立校验批次数量、批次字节和用户容量，不能信任 MCP 本地统计。"""

    monkeypatch.setenv("INTEGRATION_MAX_BATCH_FILES", "2")
    monkeypatch.setenv("INTEGRATION_MAX_BATCH_BYTES", "25")
    monkeypatch.setenv("INTEGRATION_USER_QUOTA_BYTES", "25")
    get_settings.cache_clear()
    client, _ = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-limit-user")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="limit-batch"),
        ).json()["id"]
        first = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(1, size_bytes=10), _item_payload(2, size_bytes=10)]},
        )
        too_many = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(3, size_bytes=1)]},
        )

        assert first.status_code == 200
        assert too_many.status_code == 413
        assert too_many.json()["error"]["code"] == "INGEST_BATCH_FILE_LIMIT_EXCEEDED"

        second_batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="quota-batch"),
        ).json()["id"]
        quota = client.post(
            f"/api/integrations/v1/ingest-batches/{second_batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(4, size_bytes=10)]},
        )
        assert quota.status_code == 413
        assert quota.json()["error"]["code"] == "INGEST_USER_QUOTA_EXCEEDED"
    finally:
        clear_overrides()


def test_batch_resume_only_returns_pending_receive_items() -> None:
    """自动恢复不能重试业务失败项，也不能重置等待确认或已完成条目。"""

    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-resume-user")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="resume-batch"),
        ).json()["id"]
        appended = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(index) for index in range(1, 5)]},
        ).json()["items"]
        client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/seal",
            headers=_headers(token),
        )
        with SessionLocal() as db:
            pending_item = db.get(IngestItem, appended[0]["id"])
            failed_item = db.get(IngestItem, appended[1]["id"])
            waiting_item = db.get(IngestItem, appended[2]["id"])
            completed_item = db.get(IngestItem, appended[3]["id"])
            assert pending_item and failed_item and waiting_item and completed_item
            failed_item.status = "FAILED"
            waiting_item.status = "WAITING_DUPLICATE_CONFIRMATION"
            waiting_item.stage = "DUPLICATE_DECISION"
            completed_item.status = "SUCCEEDED"
            completed_item.stage = "COMPLETED"
            db.commit()

        response = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/resume",
            headers=_headers(token),
        )

        assert response.status_code == 200
        assert response.json()["receivable_item_ids"] == [appended[0]["id"]]
        with SessionLocal() as db:
            statuses = [db.get(IngestItem, item["id"]).status for item in appended]
        assert statuses == ["PENDING", "FAILED", "WAITING_DUPLICATE_CONFIRMATION", "SUCCEEDED"]
    finally:
        clear_overrides()


def test_duplicate_review_is_versioned_and_decision_is_idempotent(monkeypatch, tmp_path) -> None:
    """WorkBuddy 必须带回固定 review 修订；相同决定重试不得创建第二个归档任务。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = b"duplicate decision source"
    try:
        _, token = _register_and_login(client, "ingest-duplicate-decision")
        batch_id, item_id, payload = _create_sealed_content_item(client, token, content=content)
        accepted = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )
        assert accepted.status_code == 202
        with SessionLocal() as db:
            review = db.query(UploadDuplicateReview).filter(UploadDuplicateReview.ingest_item_id == item_id).one()
            review.status = "WAITING_CONFIRMATION"
            db.commit()

        review_response = client.get(
            f"/api/integrations/v1/ingest-items/{item_id}/duplicate-review",
            headers=_headers(token),
        )
        assert review_response.status_code == 200
        review_body = review_response.json()
        assert review_body["item_id"] == item_id
        assert review_body["review_revision"] == 2
        assert review_body["comparison_phase"] == "EXACT_AND_NEAR"
        decision_payload = {
            "client_id": "workbuddy-local",
            "request_id": "decision-request-1",
            "idempotency_key": "decision-event-1",
            "review_id": review_body["review_id"],
            "review_revision": review_body["review_revision"],
            "candidate_id": None,
            "decision": "CONTINUE_UPLOAD",
        }
        decided = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/duplicate-decision",
            headers=_headers(token),
            json=decision_payload,
        )
        replay = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/duplicate-decision",
            headers=_headers(token),
            json=decision_payload,
        )

        assert decided.status_code == 202
        assert decided.json()["item"]["decision"] == "CONTINUE_UPLOAD"
        assert decided.json()["filesystem_job_id"]
        assert replay.status_code == 202
        assert replay.json()["reused"] is True
        assert replay.json()["filesystem_job_id"] == decided.json()["filesystem_job_id"]
        with SessionLocal() as db:
            assert (
                db.query(IntegrationRequest)
                .filter(IntegrationRequest.operation == "INGEST_DUPLICATE_DECISION")
                .count()
                == 1
            )

        changed = {**decision_payload, "decision": "CANCEL_UPLOAD"}
        conflict = client.post(
            f"/api/integrations/v1/ingest-items/{item_id}/duplicate-decision",
            headers=_headers(token),
            json=changed,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    finally:
        clear_overrides()


def test_success_plus_user_cancel_aggregates_as_succeeded() -> None:
    """成功文件加用户主动取消文件时，批次完成状态不能误报 PARTIAL。"""

    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-cancel-aggregate")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="cancel-aggregate"),
        ).json()["id"]
        client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(1), _item_payload(2)]},
        )
        with SessionLocal() as db:
            items = db.query(IngestItem).filter(IngestItem.batch_id == batch_id).all()
            items[0].status = "SUCCEEDED"
            items[1].status = "CANCELLED"
            db.commit()

        response = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(token),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "SUCCEEDED"
        assert response.json()["counts"]["cancelled"] == 1
    finally:
        clear_overrides()


def test_any_expired_confirmation_marks_mixed_batch_expired_without_losing_counts() -> None:
    """任一确认过期都结束本批交互，但成功和失败条目的事实必须继续保留。"""

    client, SessionLocal = client_with_database()
    try:
        _, token = _register_and_login(client, "ingest-expired-mixed-aggregate")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="expired-mixed-aggregate"),
        ).json()["id"]
        created = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [_item_payload(index) for index in (1, 2, 3)]},
        ).json()["items"]
        with SessionLocal() as db:
            statuses = ["SUCCEEDED", "FAILED", "EXPIRED"]
            for item_response, status in zip(created, statuses, strict=True):
                item = db.get(IngestItem, item_response["id"])
                assert item is not None
                item.status = status
                item.stage = "COMPLETED"
            db.commit()

        response = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(token),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "EXPIRED"
        assert body["display_status"] == "FILE_PROCESSING_COMPLETED"
        assert body["counts"]["succeeded"] == 1
        assert body["counts"]["failed"] == 1
        assert body["counts"]["expired"] == 1
    finally:
        clear_overrides()


def test_same_batch_exact_duplicate_can_wait_and_reuse_primary(monkeypatch, tmp_path) -> None:
    """同批后到重复项应等待固定主条目，且不能重复启动相同内容的查重主任务。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = b"same batch duplicate bytes"
    digest = hashlib.sha256(content).hexdigest()
    try:
        _, token = _register_and_login(client, "ingest-same-batch-duplicate")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="same-batch-duplicate"),
        ).json()["id"]
        first_payload = _item_payload(1, size_bytes=len(content))
        second_payload = _item_payload(2, size_bytes=len(content))
        first_payload["expected_sha256"] = digest
        second_payload["expected_sha256"] = digest
        items = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [first_payload, second_payload]},
        ).json()["items"]
        client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/seal",
            headers=_headers(token),
        )
        first = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{items[0]['id']}/content",
            headers=_headers(token),
            files={"file": (first_payload["original_filename"], content, "text/plain")},
        )
        second = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{items[1]['id']}/content",
            headers=_headers(token),
            files={"file": (second_payload["original_filename"], content, "text/plain")},
        )

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["item"]["status"] == "WAITING_DUPLICATE_CONFIRMATION"
        assert second.json()["filesystem_job_id"] == first.json()["filesystem_job_id"]
        with SessionLocal() as db:
            assert db.query(FilesystemJob).filter(FilesystemJob.job_type == "CHECK_UPLOAD_DUPLICATES").count() == 1

        review = client.get(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-review",
            headers=_headers(token),
        )
        assert review.status_code == 200
        review_body = review.json()
        assert "WAIT_AND_REUSE" in review_body["allowed_decisions"]
        assert review_body["candidates"][0]["match_scope"] == "SAME_BATCH"
        decision = client.post(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-decision",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "same-batch-decision",
                "idempotency_key": "same-batch-decision-event",
                "review_id": review_body["review_id"],
                "review_revision": review_body["review_revision"],
                "group_revision": review_body["group_revision"],
                "group_member_item_ids": review_body["group_member_item_ids"],
                "candidate_id": review_body["candidates"][0]["candidate_id"],
                "decision": "WAIT_AND_REUSE",
            },
        )
        assert decision.status_code == 202
        assert decision.json()["item"]["status"] == "WAITING_EXISTING_RESULT"
        assert decision.json()["item"]["decision"] == "WAIT_AND_REUSE"
    finally:
        clear_overrides()


def test_same_batch_duplicate_decision_rejects_stale_group_revision(monkeypatch, tmp_path) -> None:
    """用户看到重复组后若又加入成员，旧修订不得作用于已经变化的成员集合。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, _ = client_with_database()
    content = b"same batch changing duplicate group"
    digest = hashlib.sha256(content).hexdigest()
    try:
        _, token = _register_and_login(client, "ingest-stale-duplicate-group")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="stale-duplicate-group"),
        ).json()["id"]
        payloads = [_item_payload(index, size_bytes=len(content)) for index in (1, 2, 3)]
        for payload in payloads:
            payload["expected_sha256"] = digest
        items = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": payloads},
        ).json()["items"]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=_headers(token))

        for item, payload in zip(items[:2], payloads[:2], strict=True):
            response = client.put(
                f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
                headers=_headers(token),
                files={"file": (payload["original_filename"], content, "text/plain")},
            )
            assert response.status_code == 202
        stale_review = client.get(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-review",
            headers=_headers(token),
        ).json()
        assert stale_review["group_revision"] == 2
        assert stale_review["group_member_item_ids"] == [items[0]["id"], items[1]["id"]]

        incomplete_members = client.post(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-decision",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "incomplete-group-decision",
                "idempotency_key": "incomplete-group-decision-event",
                "review_id": stale_review["review_id"],
                "review_revision": stale_review["review_revision"],
                "group_revision": stale_review["group_revision"],
                "group_member_item_ids": [items[1]["id"]],
                "candidate_id": stale_review["candidates"][0]["candidate_id"],
                "decision": "WAIT_AND_REUSE",
            },
        )
        assert incomplete_members.status_code == 409
        assert incomplete_members.json()["error"]["code"] == "DUPLICATE_GROUP_REVISION_CONFLICT"

        third = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{items[2]['id']}/content",
            headers=_headers(token),
            files={"file": (payloads[2]["original_filename"], content, "text/plain")},
        )
        assert third.status_code == 202
        stale_decision = client.post(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-decision",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "stale-group-decision",
                "idempotency_key": "stale-group-decision-event",
                "review_id": stale_review["review_id"],
                "review_revision": stale_review["review_revision"],
                "group_revision": stale_review["group_revision"],
                "group_member_item_ids": stale_review["group_member_item_ids"],
                "candidate_id": stale_review["candidates"][0]["candidate_id"],
                "decision": "WAIT_AND_REUSE",
            },
        )
        assert stale_decision.status_code == 409
        assert stale_decision.json()["error"]["code"] == "DUPLICATE_GROUP_REVISION_CONFLICT"

        refreshed = client.get(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-review",
            headers=_headers(token),
        ).json()
        assert refreshed["group_revision"] == 3
        assert refreshed["group_member_item_ids"] == [item["id"] for item in items]
    finally:
        clear_overrides()


def test_same_batch_duplicate_can_keep_independent_logical_copies(monkeypatch, tmp_path) -> None:
    """用户选择保留两份时必须发布两个独立工作副本，不能因内容哈希相同而合并。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "archive"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = "2026年学院科研工作总结，包含项目建设与成果归档。".encode()
    digest = hashlib.sha256(content).hexdigest()
    try:
        _, token = _register_and_login(client, "ingest-keep-same-batch-copies")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="keep-same-batch-copies"),
        ).json()["id"]
        payloads = [_item_payload(index, size_bytes=len(content)) for index in (1, 2)]
        for payload in payloads:
            payload["expected_sha256"] = digest
        items = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": payloads},
        ).json()["items"]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=_headers(token))
        for item, payload in zip(items, payloads, strict=True):
            response = client.put(
                f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
                headers=_headers(token),
                files={"file": (payload["original_filename"], content, "text/plain")},
            )
            assert response.status_code == 202

        review = client.get(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-review",
            headers=_headers(token),
        ).json()
        decision = client.post(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-decision",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "keep-same-batch-decision",
                "idempotency_key": "keep-same-batch-event",
                "review_id": review["review_id"],
                "review_revision": review["review_revision"],
                "group_revision": review["group_revision"],
                "group_member_item_ids": review["group_member_item_ids"],
                "candidate_id": None,
                "decision": "CONTINUE_UPLOAD",
            },
        )
        assert decision.status_code == 202
        while process_next_filesystem_job(session_factory=SessionLocal, worker_id="keep-same-batch"):
            pass

        page = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
        ).json()
        completed = {item["id"]: item for item in page["items"]}
        assert {completed[item["id"]]["status"] for item in items} == {"SUCCEEDED"}
        assert completed[items[0]["id"]]["final_document_id"] != completed[items[1]["id"]]["final_document_id"]
        assert (
            completed[items[0]["id"]]["final_working_copy_id"]
            != completed[items[1]["id"]]["final_working_copy_id"]
        )
    finally:
        clear_overrides()


def test_same_content_in_different_batches_never_uses_cross_batch_wait_group(monkeypatch, tmp_path) -> None:
    """批内等待组必须包含 batch_id，不得把另一批处理中条目误当成本批主任务。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = b"same bytes across independent batches"
    digest = hashlib.sha256(content).hexdigest()
    try:
        _, token = _register_and_login(client, "ingest-cross-batch-content")
        responses = []
        for index in (1, 2):
            batch_id = client.post(
                "/api/integrations/v1/ingest-batches",
                headers=_headers(token),
                json=_batch_payload(key=f"cross-batch-{index}"),
            ).json()["id"]
            payload = _item_payload(index, size_bytes=len(content))
            payload["expected_sha256"] = digest
            item = client.post(
                f"/api/integrations/v1/ingest-batches/{batch_id}/items",
                headers=_headers(token),
                json={"items": [payload]},
            ).json()["items"][0]
            client.post(
                f"/api/integrations/v1/ingest-batches/{batch_id}/seal",
                headers=_headers(token),
            )
            responses.append(
                client.put(
                    f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
                    headers=_headers(token),
                    files={"file": (payload["original_filename"], content, "text/plain")},
                )
            )

        assert all(response.status_code == 202 for response in responses)
        assert all(response.json()["item"]["status"] == "RUNNING" for response in responses)
        assert responses[0].json()["filesystem_job_id"] != responses[1].json()["filesystem_job_id"]
        with SessionLocal() as db:
            groups = db.query(IngestDuplicateGroup).all()
            assert len(groups) == 2
            assert len({group.batch_id for group in groups}) == 2
    finally:
        clear_overrides()


def test_frozen_batch_policy_drives_initial_organization_when_legacy_flags_are_off(monkeypatch, tmp_path) -> None:
    """MCP 批次的 AUTO_ORGANIZE 授权必须贯穿异步任务，不能被旧聊天上传开关静默关闭。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "false")
    monkeypatch.setenv("AUTO_PRIMARY_CLASSIFICATION_ENABLED", "false")
    monkeypatch.setenv("AUTO_INITIAL_PLACEMENT_ENABLED", "false")
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = "学校会议纪要。会议围绕学生工作议题研究并形成决定。".encode()
    try:
        _, token = _register_and_login(client, "ingest-policy-owner")
        batch_id, item_id, _ = _create_sealed_content_item(client, token, content=content)
        accepted = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": ("材料-1.txt", content, "text/plain")},
        )
        assert accepted.status_code == 202

        processed = 0
        while process_next_filesystem_job(session_factory=SessionLocal, worker_id="integration-policy-test"):
            processed += 1
            assert processed < 12
        result = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(token),
        ).json()

        assert processed >= 4
        assert result["status"] == "SUCCEEDED"
        projected = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
        ).json()["items"][0]
        assert projected["ingest_status"] == "SUCCEEDED"
        assert projected["extraction_status"] == "COMPLETED"
        assert projected["organization_status"] == "COMPLETED"
        assert projected["index_status"] == "COMPLETED"
        assert projected["user_task_status"] == "NOT_REQUESTED"
        assert result["receipt"]["new_file_count"] == 1
        assert result["receipt"]["reused_count"] == 0
        assert result["receipt"]["excluded_count"] == 0
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            working_copy = db.get(WorkingCopy, item.final_working_copy_id)
            decision = db.query(DocumentOrganizationDecision).filter_by(
                working_copy_id=working_copy.id
            ).one()
            assert working_copy.status == "ACTIVE"
            assert ".internal" not in working_copy.relative_path
            assert working_copy.relative_path.startswith("学校/")
            assert item.result_json["rename_status"] in {"NO_CHANGE", "COMPLETED"}
            assert decision.authorization_source == "WORKBUDDY_INGEST_POLICY"
            assert decision.source_request_id == "request-001"
            assert decision.policy_version == "ingest-v1"
            assert decision.after_revision > decision.before_revision
    finally:
        clear_overrides()


def test_legacy_source_rule_profile_applies_only_when_explicitly_selected(monkeypatch, tmp_path) -> None:
    """显式旧材料规则必须消费冻结来源路径，默认内容规则不能静默继承目录分类。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "false")
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = "材料清单，仅用于验证显式来源规则。".encode()
    try:
        _, token = _register_and_login(client, "ingest-source-profile")
        payload = _batch_payload(key="legacy-source-profile", directory="人事处/职称评定")
        payload["rule_profile"] = "legacy_school_materials"
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=payload,
        ).json()["id"]
        item_payload = _item_payload(1, size_bytes=len(content))
        item_payload["source_relative_path"] = "人事处/职称评定/材料-1.txt"
        item_payload["expected_sha256"] = hashlib.sha256(content).hexdigest()
        item_id = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": [item_payload]},
        ).json()["items"][0]["id"]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=_headers(token))
        uploaded = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": ("材料-1.txt", content, "text/plain")},
        )
        assert uploaded.status_code == 202
        while process_next_filesystem_job(
            session_factory=SessionLocal,
            worker_id="legacy-source-profile-test",
        ):
            pass
        client.get(f"/api/integrations/v1/ingest-batches/{batch_id}", headers=_headers(token))
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            working_copy = db.get(WorkingCopy, item.final_working_copy_id)
            decision = db.query(DocumentOrganizationDecision).filter_by(
                working_copy_id=working_copy.id
            ).one()
            assert working_copy.relative_path.startswith("学校/人事师资/职称/")
            assert decision.category_id == "school.hr.title-review"
    finally:
        clear_overrides()


def test_all_exact_duplicates_can_reuse_existing_without_modifying_it(monkeypatch, tmp_path) -> None:
    """全部重复选择已有文件时新增数为零，且不能改动已有工作副本名称、修订或人工分类。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "false")
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = "国家励志奖学金申请材料，内容完全相同。".encode()
    try:
        _, token = _register_and_login(client, "ingest-reuse-existing")
        first_batch, first_item, first_payload = _create_sealed_content_item(client, token, content=content)
        client.put(
            f"/api/integrations/v1/ingest-batches/{first_batch}/items/{first_item}/content",
            headers=_headers(token),
            files={"file": (first_payload["original_filename"], content, "text/plain")},
        )
        while process_next_filesystem_job(session_factory=SessionLocal, worker_id="reuse-existing-first"):
            pass
        first_result = client.get(
            f"/api/integrations/v1/ingest-batches/{first_batch}", headers=_headers(token)
        ).json()
        assert first_result["status"] == "SUCCEEDED"
        with SessionLocal() as db:
            first_row = db.get(IngestItem, first_item)
            existing = db.get(WorkingCopy, first_row.final_working_copy_id)
            # 模拟人工确认分类，重复复用链路不得删除或重建它。
            manual = DocumentCategory(
                working_copy_id=existing.id,
                document_id=existing.document_id,
                document_version_id=existing.current_version_id,
                category_id="manual-protected-category",
                category_path_json=["人工分类"],
                relation_role="RELATED",
                status="CONFIRMED",
                taxonomy_key="manual",
                taxonomy_version="manual-v1",
                classifier_version="human",
                source="human",
                evidence_json=[],
            )
            db.add(manual)
            db.commit()
            existing_id = existing.id
            existing_document_id = existing.document_id
            existing_name = existing.filename
            existing_revision = existing.revision

        second_payload = _batch_payload(key="reuse-existing-second")
        second_batch = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=second_payload,
        ).json()["id"]
        item_payload = _item_payload(2, size_bytes=len(content))
        item_payload["expected_sha256"] = hashlib.sha256(content).hexdigest()
        second_item = client.post(
            f"/api/integrations/v1/ingest-batches/{second_batch}/items",
            headers=_headers(token),
            json={"items": [item_payload]},
        ).json()["items"][0]["id"]
        client.post(f"/api/integrations/v1/ingest-batches/{second_batch}/seal", headers=_headers(token))
        client.put(
            f"/api/integrations/v1/ingest-batches/{second_batch}/items/{second_item}/content",
            headers=_headers(token),
            files={"file": (item_payload["original_filename"], content, "text/plain")},
        )
        while process_next_filesystem_job(session_factory=SessionLocal, worker_id="reuse-existing-second"):
            pass
        review = client.get(
            f"/api/integrations/v1/ingest-items/{second_item}/duplicate-review",
            headers=_headers(token),
        ).json()
        candidate = next(value for value in review["candidates"] if value["existing_document_id"])
        decided = client.post(
            f"/api/integrations/v1/ingest-items/{second_item}/duplicate-decision",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "reuse-existing-decision",
                "idempotency_key": "reuse-existing-decision-event",
                "review_id": review["review_id"],
                "review_revision": review["review_revision"],
                "candidate_id": candidate["candidate_id"],
                "decision": "USE_EXISTING_FILE",
            },
        )
        assert decided.status_code == 202
        final = client.get(
            f"/api/integrations/v1/ingest-batches/{second_batch}", headers=_headers(token)
        ).json()
        assert final["status"] == "SUCCEEDED"
        assert final["receipt"]["new_file_count"] == 0
        assert final["receipt"]["reused_count"] == 1
        assert final["receipt"]["retained_file_count"] == 1
        with SessionLocal() as db:
            reused = db.get(IngestItem, second_item)
            existing = db.get(WorkingCopy, existing_id)
            assert reused.final_document_id == existing_document_id
            assert reused.final_working_copy_id == existing_id
            assert existing.filename == existing_name
            assert existing.revision == existing_revision
            assert db.query(DocumentCategory).filter_by(
                document_id=existing_document_id,
                status="CONFIRMED",
            ).count() == 1
    finally:
        clear_overrides()


def test_expired_duplicate_confirmation_expires_single_item_batch(monkeypatch, tmp_path) -> None:
    """待确认过期后，只有该条目的批次必须聚合为 EXPIRED。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = b"expiring duplicate review"
    try:
        _, token = _register_and_login(client, "ingest-expired-review")
        batch_id, item_id, payload = _create_sealed_content_item(client, token, content=content)
        client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )
        with SessionLocal() as db:
            review = db.query(UploadDuplicateReview).filter(UploadDuplicateReview.ingest_item_id == item_id).one()
            review.status = "WAITING_CONFIRMATION"
            review.expires_at = utcnow() - timedelta(seconds=1)
            db.commit()

        response = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(token),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "EXPIRED"
        assert response.json()["counts"]["expired"] == 1
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            version = db.get(DocumentVersion, item.upload_document_version_id)
            document = db.get(Document, version.document_id)
            assert document.status == "UPLOAD_CANCELLED"
    finally:
        clear_overrides()


def test_wait_and_reuse_binds_primary_result_without_creating_second_file(monkeypatch, tmp_path) -> None:
    """同批主任务成功后，等待项只绑定相同最终 ID，并清理自身暂存副本。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = b"wait and reuse completion"
    digest = hashlib.sha256(content).hexdigest()
    try:
        _, token = _register_and_login(client, "ingest-wait-complete")
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=_batch_payload(key="wait-complete"),
        ).json()["id"]
        payloads = [_item_payload(index, size_bytes=len(content)) for index in (1, 2)]
        for payload in payloads:
            payload["expected_sha256"] = digest
        items = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": payloads},
        ).json()["items"]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=_headers(token))
        for item, payload in zip(items, payloads, strict=True):
            client.put(
                f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
                headers=_headers(token),
                files={"file": (payload["original_filename"], content, "text/plain")},
            )
        review = client.get(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-review",
            headers=_headers(token),
        ).json()
        client.post(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-decision",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": "wait-complete-decision",
                "idempotency_key": "wait-complete-event",
                "review_id": review["review_id"],
                "review_revision": review["review_revision"],
                "group_revision": review["group_revision"],
                "group_member_item_ids": review["group_member_item_ids"],
                "candidate_id": review["candidates"][0]["candidate_id"],
                "decision": "WAIT_AND_REUSE",
            },
        )
        with SessionLocal() as db:
            primary = db.get(IngestItem, items[0]["id"])
            assert primary and primary.upload_document_version_id
            upload_version = db.get(DocumentVersion, primary.upload_document_version_id)
            assert upload_version
            primary.status = "SUCCEEDED"
            primary.stage = "COMPLETED"
            primary.final_document_id = upload_version.document_id
            primary.final_version_id = upload_version.id
            db.commit()

        batch = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}", headers=_headers(token)
        )
        assert batch.status_code == 200
        assert batch.json()["status"] == "SUCCEEDED"
        page = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items", headers=_headers(token)
        ).json()
        by_id = {item["id"]: item for item in page["items"]}
        follower = by_id[items[1]["id"]]
        assert follower["status"] == "SUCCEEDED"
        assert follower["final_document_id"] == by_id[items[0]["id"]]["final_document_id"]
        assert follower["result"]["new_logical_file_created"] is False
    finally:
        clear_overrides()


@pytest.mark.parametrize("primary_status", ["FAILED", "CANCELLED"])
def test_wait_and_reuse_reports_primary_failure_without_fallback_or_user_task(
    monkeypatch,
    tmp_path,
    primary_status: str,
) -> None:
    """主任务失败或取消后，等待项只能报告失败，不能自动另存或继续附带总结。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = b"wait dependency terminal failure"
    digest = hashlib.sha256(content).hexdigest()
    try:
        _, token = _register_and_login(client, f"ingest-wait-{primary_status.lower()}")
        batch_payload = _batch_payload(key=f"wait-{primary_status.lower()}")
        batch_payload["user_request"] = "请整理后总结这些文件"
        batch_id = client.post(
            "/api/integrations/v1/ingest-batches",
            headers=_headers(token),
            json=batch_payload,
        ).json()["id"]
        payloads = [_item_payload(index, size_bytes=len(content)) for index in (1, 2)]
        for payload in payloads:
            payload["expected_sha256"] = digest
        items = client.post(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
            json={"items": payloads},
        ).json()["items"]
        client.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal", headers=_headers(token))
        for item, payload in zip(items, payloads, strict=True):
            response = client.put(
                f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item['id']}/content",
                headers=_headers(token),
                files={"file": (payload["original_filename"], content, "text/plain")},
            )
            assert response.status_code == 202
        review = client.get(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-review",
            headers=_headers(token),
        ).json()
        decision = client.post(
            f"/api/integrations/v1/ingest-items/{items[1]['id']}/duplicate-decision",
            headers=_headers(token),
            json={
                "client_id": "workbuddy-local",
                "request_id": f"wait-{primary_status.lower()}-decision",
                "idempotency_key": f"wait-{primary_status.lower()}-event",
                "review_id": review["review_id"],
                "review_revision": review["review_revision"],
                "group_revision": review["group_revision"],
                "group_member_item_ids": review["group_member_item_ids"],
                "candidate_id": review["candidates"][0]["candidate_id"],
                "decision": "WAIT_AND_REUSE",
            },
        )
        assert decision.status_code == 202

        with SessionLocal() as db:
            primary = db.get(IngestItem, items[0]["id"])
            assert primary is not None
            archive = db.get(UploadArchiveRecord, primary.archive_record_id)
            review_row = db.query(UploadDuplicateReview).filter(
                UploadDuplicateReview.ingest_item_id == primary.id
            ).one()
            assert archive is not None
            if primary_status == "FAILED":
                archive.status = "FAILED"
                archive.last_error_code = "TEST_PRIMARY_FAILED"
                archive.last_error_message = "主任务测试失败"
            else:
                review_row.status = "RESOLVED"
                review_row.decision = "CANCEL_UPLOAD"
                archive.status = "CANCELLED"
            db.commit()

        response = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(token),
        )
        assert response.status_code == 200
        page = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
        ).json()
        follower = next(item for item in page["items"] if item["id"] == items[1]["id"])
        assert follower["status"] == "FAILED"
        assert follower["error"]["code"] == "WAITED_PRIMARY_NOT_AVAILABLE"
        assert follower["final_document_id"] is None
        assert follower["user_task_status"] == "EXCLUDED"
        with SessionLocal() as db:
            executions = db.query(IngestRequestExecution).filter(
                IngestRequestExecution.batch_id == batch_id
            ).all()
            assert all(items[1]["id"] not in execution.item_ids_json for execution in executions)
    finally:
        clear_overrides()


def test_batch_items_separate_ingest_filename_snapshot_from_current_file_state(
    monkeypatch,
    tmp_path,
) -> None:
    """批次回执保留导入名称，同时实时投影改名、回收站和关联失效状态。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("INTEGRATION_EXTERNAL_OCR_ENABLED", "false")
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    content = "请假材料，用于验证批次历史名称与当前名称分离。".encode()
    try:
        _, token = _register_and_login(client, "ingest-current-filename")
        batch_id, item_id, payload = _create_sealed_content_item(
            client,
            token,
            content=content,
        )
        uploaded = client.put(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
            headers=_headers(token),
            files={"file": (payload["original_filename"], content, "text/plain")},
        )
        assert uploaded.status_code == 202
        processed = 0
        while process_next_filesystem_job(
            session_factory=SessionLocal,
            worker_id="ingest-current-filename-worker",
        ):
            processed += 1
            assert processed < 12
        client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}",
            headers=_headers(token),
        )
        initial = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
        ).json()["items"][0]
        ingest_name = initial["result"]["final_filename"]
        assert initial["result"]["ingest_final_filename"] == ingest_name
        assert initial["ingest_final_filename"] == ingest_name
        assert initial["current_filename"] == ingest_name
        assert initial["current_file_status"] == "ACTIVE"
        assert initial["current_file_available"] is True
        assert initial["current_working_copy_revision"] is not None
        assert initial["current_document_version_id"] == initial["final_version_id"]

        renamed = "重命名后的请假材料.txt"
        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            assert item is not None and item.final_working_copy_id
            working_copy = db.get(WorkingCopy, item.final_working_copy_id)
            assert working_copy is not None
            # 模拟旧批次只持久化 final_filename，验证兼容读取不会把当前名称写回快照。
            legacy_result = dict(item.result_json or {})
            legacy_result.pop("ingest_final_filename", None)
            item.result_json = legacy_result
            working_copy.filename = renamed
            working_copy.revision += 1
            db.commit()

        after_rename = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
        ).json()["items"][0]
        assert after_rename["result"]["final_filename"] == ingest_name
        assert after_rename["ingest_final_filename"] == ingest_name
        assert after_rename["current_filename"] == renamed
        assert after_rename["current_file_status"] == "ACTIVE"
        assert after_rename["current_file_available"] is True
        assert after_rename["current_working_copy_revision"] > initial["current_working_copy_revision"]

        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            working_copy = db.get(WorkingCopy, item.final_working_copy_id)
            working_copy.status = "TRASHED"
            working_copy.revision += 1
            db.commit()

        trashed = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
        ).json()["items"][0]
        assert trashed["ingest_final_filename"] == ingest_name
        assert trashed["current_filename"] == renamed
        assert trashed["current_file_status"] == "TRASHED"
        assert trashed["current_file_available"] is False

        with SessionLocal() as db:
            item = db.get(IngestItem, item_id)
            item.final_working_copy_id = None
            db.commit()

        unavailable = client.get(
            f"/api/integrations/v1/ingest-batches/{batch_id}/items",
            headers=_headers(token),
        ).json()["items"][0]
        assert unavailable["ingest_final_filename"] == ingest_name
        assert unavailable["current_filename"] is None
        assert unavailable["current_file_status"] == "UNAVAILABLE"
        assert unavailable["current_file_available"] is False
    finally:
        clear_overrides()
