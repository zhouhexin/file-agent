"""本地目录批次清单与断点恢复测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from file_agent_mcp.client import LocalRootRegistry
from file_agent_mcp.transfer import BatchTransferService, TransferStateStore


class FakeIntegrationClient:
    """记录批次调用并模拟后端持久化状态的确定性客户端。"""

    def __init__(self, root: Path) -> None:
        """配置授权根和空的服务端条目集合。"""

        self.roots = LocalRootRegistry({"materials": root})
        self.items: list[dict] = []
        self.upload_calls: list[str] = []
        self.batch_payload: dict = {}

    async def create_batch(self, payload: dict) -> dict:
        """返回固定批次 ID。"""

        assert payload["relative_directory"] == "待导入"
        self.batch_payload = dict(payload)
        return {"id": "batch-1"}

    async def append_items(self, *, batch_id: str, items: list[dict]) -> dict:
        """把清单映射为后端 upload_id。"""

        assert batch_id == "batch-1"
        self.items = [
            {
                **item,
                "id": f"upload-{index}",
                "stage": "RECEIVE",
                "status": "PENDING",
                "actual_sha256": None,
            }
            for index, item in enumerate(items, start=1)
        ]
        return {"items": self.items}

    async def seal_batch(self, *, batch_id: str) -> dict:
        """模拟固定清单。"""

        return {"id": batch_id, "manifest_status": "SEALED"}

    async def file_ingest(self, **kwargs) -> dict:
        """记录逐文件上传，并把对应服务端条目标为已接收。"""

        self.upload_calls.append(str(kwargs["source_relative_path"]))
        for item in self.items:
            if item["id"] == kwargs["item_id"]:
                item["actual_sha256"] = item["expected_sha256"]
                item["stage"] = "EXACT_CHECK"
        return {"accepted": True, "filesystem_job_id": f"job-{kwargs['item_id']}"}

    async def batch_get(self, *, batch_id: str) -> dict:
        """返回可显示的批次状态。"""

        return {"id": batch_id, "status": "RUNNING"}

    async def batch_items(self, *, batch_id: str) -> list[dict]:
        """返回当前服务端逐项事实。"""

        return self.items

    async def resume_batch(self, *, batch_id: str) -> dict:
        """只返回仍处于首次接收阶段的条目 ID。"""

        return {
            "batch": {"id": batch_id},
            "receivable_item_ids": [
                item["id"]
                for item in self.items
                if item["status"] == "PENDING" and item["stage"] == "RECEIVE"
            ],
        }


class MutatingSourceClient(FakeIntegrationClient):
    """在清单固定后模拟本地文件被外部程序修改。"""

    def __init__(self, root: Path, target: Path) -> None:
        """保存待修改文件，seal 时制造传输竞争窗口。"""

        super().__init__(root)
        self.target = target

    async def seal_batch(self, *, batch_id: str) -> dict:
        """固定后修改文件，验证真正上传前仍会复核快照。"""

        self.target.write_text("changed-after-seal", encoding="utf-8")
        return await super().seal_batch(batch_id=batch_id)


class MissingManifestItemClient(FakeIntegrationClient):
    """模拟后端清单响应缺少一项，用于保护传输统计不出现负数。"""

    async def append_items(self, *, batch_id: str, items: list[dict]) -> dict:
        """只返回第一项，缺少项应作为预检查失败而非上传失败。"""

        response = await super().append_items(batch_id=batch_id, items=items)
        self.items = self.items[:1]
        return {"items": response["items"][:1]}


class SealedReplayClient(FakeIntegrationClient):
    """模拟相同清单创建请求复用已经 seal 的后端批次。"""

    def __init__(self, root: Path) -> None:
        """记录批次创建次数，第二次返回持久化清单状态。"""

        super().__init__(root)
        self.create_calls = 0

    async def create_batch(self, payload: dict) -> dict:
        """首次返回 OPEN，后续幂等重放返回 SEALED。"""

        self.create_calls += 1
        await super().create_batch(payload)
        return {
            "id": "batch-1",
            "manifest_status": "OPEN" if self.create_calls == 1 else "SEALED",
        }


def test_directory_ingest_saves_logical_state_and_uploads_all_files(tmp_path) -> None:
    """首次目录导入必须固定清单、传输全部文件，且状态文件不泄漏绝对路径。"""

    root = tmp_path / "root"
    directory = root / "待导入"
    directory.mkdir(parents=True)
    (directory / "a.txt").write_text("A", encoding="utf-8")
    (directory / "b.txt").write_text("B", encoding="utf-8")
    source_snapshots = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
        for path in directory.iterdir()
    }
    client = FakeIntegrationClient(root)
    state_store = TransferStateStore(tmp_path / "state")

    result = asyncio.run(
        BatchTransferService(client, state_store).ingest_directory(
            source_root_ref="materials",
            relative_directory="待导入",
        )
    )

    assert result["uploaded_count"] == 2
    assert result["transfer_failed_count"] == 0
    assert client.upload_calls == ["待导入/a.txt", "待导入/b.txt"]
    raw_state = (tmp_path / "state" / "batch-1.json").read_text(encoding="utf-8")
    assert str(root) not in raw_state
    assert json.loads(raw_state)["source_root_ref"] == "materials"
    assert {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
        for path in directory.iterdir()
    } == source_snapshots


def test_directory_ingest_forwards_only_supported_frozen_organization_policy(tmp_path) -> None:
    """显式材料包规则必须进入后端冻结策略，未知策略不能由模型自由透传。"""

    root = tmp_path / "root"
    directory = root / "待导入"
    directory.mkdir(parents=True)
    (directory / "a.txt").write_text("A", encoding="utf-8")
    client = FakeIntegrationClient(root)
    service = BatchTransferService(client, TransferStateStore(tmp_path / "state"))

    asyncio.run(
        service.ingest_directory(
            source_root_ref="materials",
            relative_directory="待导入",
            placement_mode="NEUTRAL",
            rule_profile="legacy_school_materials",
        )
    )
    assert client.batch_payload["placement_mode"] == "NEUTRAL"
    assert client.batch_payload["rule_profile"] == "legacy_school_materials"

    try:
        asyncio.run(
            service.ingest_directory(
                source_root_ref="materials",
                relative_directory="待导入",
                rule_profile="model-invented-profile",
            )
        )
    except ValueError as exc:
        assert "固定规则集" in str(exc)
    else:
        raise AssertionError("未知规则集必须在创建批次前拒绝")


def test_batch_resume_skips_received_items_and_rejects_changed_source(tmp_path) -> None:
    """恢复只上传未接收项；本地文件变化必须停止，不能偷偷扩大或更新原清单。"""

    root = tmp_path / "root"
    directory = root / "待导入"
    directory.mkdir(parents=True)
    first = directory / "a.txt"
    second = directory / "b.txt"
    first.write_text("A", encoding="utf-8")
    second.write_text("B", encoding="utf-8")
    client = FakeIntegrationClient(root)
    state_store = TransferStateStore(tmp_path / "state")
    service = BatchTransferService(client, state_store)
    asyncio.run(
        service.ingest_directory(source_root_ref="materials", relative_directory="待导入")
    )
    client.upload_calls.clear()
    client.items[0]["actual_sha256"] = client.items[0]["expected_sha256"]
    client.items[0]["stage"] = "EXACT_CHECK"
    client.items[1]["actual_sha256"] = None
    client.items[1]["stage"] = "RECEIVE"

    resumed = asyncio.run(service.resume(batch_id="batch-1"))
    assert resumed["uploaded_count"] == 1
    assert client.upload_calls == ["待导入/b.txt"]

    second.write_text("changed", encoding="utf-8")
    try:
        asyncio.run(service.resume(batch_id="batch-1"))
    except ValueError as exc:
        assert "来源快照已变化" in str(exc)
    else:
        raise AssertionError("来源文件变化时必须拒绝恢复")


def test_initial_transfer_rechecks_snapshot_after_manifest_is_sealed(tmp_path) -> None:
    """首次上传也必须关闭清单与传输间的变化窗口，并保持其他条目可继续。"""

    root = tmp_path / "root"
    directory = root / "待导入"
    directory.mkdir(parents=True)
    target = directory / "a.txt"
    target.write_text("original", encoding="utf-8")
    client = MutatingSourceClient(root, target)

    result = asyncio.run(
        BatchTransferService(client, TransferStateStore(tmp_path / "state")).ingest_directory(
            source_root_ref="materials",
            relative_directory="待导入",
        )
    )

    assert result["uploaded_count"] == 0
    assert result["transfer_failed_count"] == 1
    assert "SOURCE_CHANGED" in result["transfer_failures"][0]["error"]
    assert client.upload_calls == []


def test_missing_server_manifest_item_does_not_make_uploaded_count_negative(tmp_path) -> None:
    """清单映射缺失与真实上传失败必须分别计数，成功数不能被预检查错误重复扣减。"""

    root = tmp_path / "root"
    directory = root / "待导入"
    directory.mkdir(parents=True)
    (directory / "a.txt").write_text("A", encoding="utf-8")
    (directory / "b.txt").write_text("B", encoding="utf-8")
    client = MissingManifestItemClient(root)

    result = asyncio.run(
        BatchTransferService(client, TransferStateStore(tmp_path / "state")).ingest_directory(
            source_root_ref="materials",
            relative_directory="待导入",
        )
    )

    assert result["uploaded_count"] == 1
    assert result["transfer_failed_count"] == 1


def test_whole_tool_replay_resumes_sealed_batch_without_appending_manifest(tmp_path) -> None:
    """整次工具响应丢失后可用相同参数重试，已 seal 清单不能再次追加。"""

    root = tmp_path / "root"
    directory = root / "待导入"
    directory.mkdir(parents=True)
    (directory / "a.txt").write_text("A", encoding="utf-8")
    client = SealedReplayClient(root)
    service = BatchTransferService(client, TransferStateStore(tmp_path / "state"))

    first = asyncio.run(
        service.ingest_directory(source_root_ref="materials", relative_directory="待导入")
    )
    second = asyncio.run(
        service.ingest_directory(source_root_ref="materials", relative_directory="待导入")
    )

    assert first["uploaded_count"] == 1
    assert second["uploaded_count"] == 0
    assert client.upload_calls == ["待导入/a.txt"]
