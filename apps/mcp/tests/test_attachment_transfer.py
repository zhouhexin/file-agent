"""WorkBuddy 会话附件受控导入测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from file_agent_mcp.attachment_transfer import AttachmentTransferService
from file_agent_mcp.client import WorkBuddyAttachmentRegistry


class FakeAttachmentClient:
    """记录附件清单和字节上传，模拟现有集成 API。"""

    def __init__(self) -> None:
        """初始化确定性批次状态。"""

        self.batch_payload: dict = {}
        self.items: list[dict] = []
        self.uploaded: list[tuple[str, Path, str]] = []

    async def create_batch(self, payload: dict) -> dict:
        """记录冻结策略并返回开放批次。"""

        self.batch_payload = dict(payload)
        return {"id": "batch-attachment", "manifest_status": "OPEN"}

    async def append_items(self, *, batch_id: str, items: list[dict]) -> dict:
        """为附件清单分配后端条目 ID。"""

        self.items = [
            {**item, "id": f"item-{index}", "stage": "RECEIVE", "actual_sha256": None}
            for index, item in enumerate(items, start=1)
        ]
        return {"items": self.items}

    async def seal_batch(self, *, batch_id: str) -> dict:
        """模拟清单固定。"""

        return {"id": batch_id, "manifest_status": "SEALED"}

    async def upload_resolved_file(self, *, batch_id: str, item_id: str, path: Path, filename: str) -> dict:
        """记录经过授权解析的真实附件，不读取任意外部路径。"""

        self.uploaded.append((item_id, path, filename))
        return {"accepted": True}

    async def batch_get(self, *, batch_id: str) -> dict:
        """返回可恢复批次引用。"""

        return {"id": batch_id, "status": "RUNNING"}

    async def batch_items(self, *, batch_id: str) -> list[dict]:
        """返回已固定清单。"""

        return self.items


def test_workbuddy_attachments_become_logical_manifest_and_upload(tmp_path) -> None:
    """会话附件应复用既有批次入口，且后端载荷不得包含 WorkBuddy 缓存绝对路径。"""

    cache = tmp_path / "workbuddy-cache"
    cache.mkdir()
    first = cache / "申请表.xlsx"
    second = cache / "证明.pdf"
    first.write_bytes(b"spreadsheet")
    second.write_bytes(b"pdf")
    client = FakeAttachmentClient()

    result = asyncio.run(
        AttachmentTransferService(client, WorkBuddyAttachmentRegistry([cache])).ingest(
            submission_id="message-20260909-1",
            attachments=[
                {"attachment_id": "att-1", "filename": first.name, "local_path": str(first)},
                {"attachment_id": "att-2", "filename": second.name, "local_path": str(second)},
            ],
            user_request=None,
            placement_mode="BY_CATEGORY",
            rule_profile="content_based",
        )
    )

    assert result["uploaded_count"] == 2
    assert client.batch_payload["client_id"] == "workbuddy-attachment"
    assert client.batch_payload["user_request"] is None
    assert client.batch_payload["placement_mode"] == "BY_CATEGORY"
    assert [item["source_relative_path"] for item in client.items] == [
        "submitted-attachments/att-1/申请表.xlsx",
        "submitted-attachments/att-2/证明.pdf",
    ]
    assert str(cache) not in repr(client.batch_payload)
    assert str(cache) not in repr(client.items)


def test_attachment_registry_rejects_file_outside_authorized_cache(tmp_path) -> None:
    """模型即使提交任意绝对路径，也不能让 MCP 读取授权缓存根之外的文件。"""

    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    registry = WorkBuddyAttachmentRegistry([cache])

    with pytest.raises(ValueError, match="不在已授权缓存根"):
        registry.resolve(local_path=str(outside), filename=outside.name)


def test_attachment_registry_rejects_symlink_even_when_link_is_in_cache(tmp_path) -> None:
    """缓存目录中的软链接不能借附件入口越权读取其他位置。"""

    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = cache / "secret.txt"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="不能是符号链接"):
        WorkBuddyAttachmentRegistry([cache]).resolve(local_path=str(link), filename=link.name)


def test_replaying_submission_uses_stable_idempotency_key(tmp_path) -> None:
    """同一 WorkBuddy 提交事件必须使用稳定键，让后端拒绝附件集合被替换。"""

    cache = tmp_path / "cache"
    cache.mkdir()
    attachment = cache / "材料.txt"
    attachment.write_text("正文", encoding="utf-8")
    client = FakeAttachmentClient()
    service = AttachmentTransferService(client, WorkBuddyAttachmentRegistry([cache]))
    arguments = {
        "submission_id": "submission-1",
        "attachments": [
            {"attachment_id": "att-1", "filename": attachment.name, "local_path": str(attachment)}
        ],
        "user_request": "总结材料",
        "placement_mode": "BY_CATEGORY",
        "rule_profile": "content_based",
    }

    asyncio.run(service.ingest(**arguments))
    first_key = client.batch_payload["idempotency_key"]
    asyncio.run(service.ingest(**arguments))

    assert client.batch_payload["idempotency_key"] == first_key


def test_changed_attachment_snapshot_changes_request_fingerprint_but_not_idempotency_key(
    tmp_path,
) -> None:
    """同一提交事件若附件被替换，应让后端识别指纹冲突而不是创建第二批。"""

    cache = tmp_path / "cache"
    cache.mkdir()
    attachment = cache / "材料.txt"
    attachment.write_text("第一版", encoding="utf-8")
    client = FakeAttachmentClient()
    service = AttachmentTransferService(client, WorkBuddyAttachmentRegistry([cache]))
    arguments = {
        "submission_id": "submission-immutable",
        "attachments": [
            {"attachment_id": "att-1", "filename": attachment.name, "local_path": str(attachment)}
        ],
        "user_request": None,
        "placement_mode": "BY_CATEGORY",
        "rule_profile": "content_based",
    }

    asyncio.run(service.ingest(**arguments))
    first_request_id = client.batch_payload["request_id"]
    first_key = client.batch_payload["idempotency_key"]
    attachment.write_text("被替换的第二版", encoding="utf-8")
    asyncio.run(service.ingest(**arguments))

    assert client.batch_payload["idempotency_key"] == first_key
    assert client.batch_payload["request_id"] != first_request_id
