"""WorkBuddy 本地目录批次传输与断点状态。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .client import FileAgentIntegrationClient, file_sha256


@dataclass(slots=True)
class LocalManifestItem:
    """本地枚举得到的不可变文件快照。"""

    client_item_id: str
    source_root_ref: str
    source_relative_path: str
    original_filename: str
    size_bytes: int
    mtime_ns: int
    expected_sha256: str
    local_path: Path

    def api_payload(self) -> dict[str, Any]:
        """返回不含本地绝对路径的后端清单载荷。"""

        return {
            "client_item_id": self.client_item_id,
            "source_root_ref": self.source_root_ref,
            "source_relative_path": self.source_relative_path,
            "original_filename": self.original_filename,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "expected_sha256": self.expected_sha256,
        }


class TransferStateStore:
    """在本机保存批次逻辑引用，进程退出后可从后端事实继续恢复。"""

    def __init__(self, state_dir: Path) -> None:
        """创建权限受控的状态目录；文件中不保存令牌或源绝对路径。"""

        self.state_dir = state_dir.expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @classmethod
    def from_environment(cls) -> "TransferStateStore":
        """读取可配置状态目录，默认位于当前用户目录。"""

        return cls(Path(os.getenv("LOCAL_TRANSFER_STATE_DIR", "~/.file-agent/transfer-state")))

    def save(self, *, batch_id: str, state: dict[str, Any]) -> None:
        """原子保存断点状态，避免进程中断留下半个 JSON。"""

        target = self.state_dir / f"{batch_id}.json"
        temporary = self.state_dir / f".{batch_id}.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)

    def load(self, *, batch_id: str) -> dict[str, Any]:
        """读取本地传输提示；业务状态仍必须再向后端查询。"""

        target = self.state_dir / f"{batch_id}.json"
        if not target.is_file():
            return {}
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("batch_id") != batch_id:
            raise ValueError("本地批次状态文件无效")
        return payload


class BatchTransferService:
    """创建固定目录清单并逐项上传，支持按后端状态恢复未完成传输。"""

    def __init__(self, client: FileAgentIntegrationClient, state_store: TransferStateStore) -> None:
        """注入 API 客户端和本地断点存储。"""

        self.client = client
        self.state_store = state_store
        self.concurrency = max(1, min(8, int(os.getenv("LOCAL_UPLOAD_CONCURRENCY", "2"))))

    async def ingest_directory(
        self,
        *,
        source_root_ref: str,
        relative_directory: str,
        recursive: bool = True,
        user_request: str | None = None,
        placement_mode: str = "BY_CATEGORY",
        rule_profile: str = "content_based",
    ) -> dict[str, Any]:
        """枚举明确目录、创建并 seal 清单，然后上传全部可运行条目。"""

        if placement_mode not in {"BY_CATEGORY", "NEUTRAL"}:
            raise ValueError("placement_mode 只能是 BY_CATEGORY 或 NEUTRAL")
        if rule_profile not in {"content_based", "legacy_school_materials"}:
            raise ValueError("rule_profile 不是 File Agent 允许的固定规则集")

        manifest = self._build_manifest(
            source_root_ref=source_root_ref,
            relative_directory=relative_directory,
            recursive=recursive,
        )
        if not manifest:
            raise ValueError("指定目录中没有可导入的普通文件")
        manifest_digest = self._manifest_digest(
            manifest,
            relative_directory=relative_directory,
            recursive=recursive,
            user_request=user_request,
            placement_mode=placement_mode,
            rule_profile=rule_profile,
        )
        batch = await self.client.create_batch(
            {
                "client_id": "workbuddy-local",
                "request_id": f"local-{time.time_ns()}",
                "idempotency_key": f"local-manifest-{manifest_digest}",
                "source_root_ref": source_root_ref,
                "relative_directory": relative_directory,
                "recursive": recursive,
                "ingest_policy": "AUTO_ORGANIZE",
                "placement_mode": placement_mode,
                "rule_profile": rule_profile,
                "user_request": user_request,
                "conversation_id": None,
            }
        )
        batch_id = str(batch["id"])
        item_map: dict[str, dict[str, Any]] = {}
        if batch.get("manifest_status") == "SEALED":
            # 整次 MCP 调用的响应可能丢失；相同清单会复用旧批次，此时不能再次追加，
            # 直接读取后端固定成员并继续尚未接收的内容。
            server_items = await self.client.batch_items(batch_id=batch_id)
            item_map = {str(item["client_item_id"]): item for item in server_items}
        else:
            for offset in range(0, len(manifest), 200):
                response = await self.client.append_items(
                    batch_id=batch_id,
                    items=[item.api_payload() for item in manifest[offset : offset + 200]],
                )
                for item in response.get("items", []):
                    item_map[str(item["client_item_id"])] = item
            await self.client.seal_batch(batch_id=batch_id)
        self.state_store.save(
            batch_id=batch_id,
            state=self._state_payload(batch_id=batch_id, manifest=manifest),
        )
        return await self._upload_manifest(batch_id=batch_id, manifest=manifest, item_map=item_map)

    async def resume(self, *, batch_id: str) -> dict[str, Any]:
        """以后端条目状态为准，只重传仍未接收内容的原清单成员。"""

        local_state = self.state_store.load(batch_id=batch_id)
        if not local_state:
            raise ValueError("本机没有该批次的来源清单，无法继续传输")
        source_root_ref = str(local_state["source_root_ref"])
        manifest: list[LocalManifestItem] = []
        for saved in local_state.get("items", []):
            relative_path = str(saved["source_relative_path"])
            local_path = self.client.roots.resolve_file(
                source_root_ref=source_root_ref,
                source_relative_path=relative_path,
            )
            stat = local_path.stat()
            if stat.st_size != int(saved["size_bytes"]) or stat.st_mtime_ns != int(saved["mtime_ns"]):
                raise ValueError(f"来源快照已变化：{relative_path}")
            if file_sha256(local_path) != str(saved["expected_sha256"]):
                raise ValueError(f"来源内容已变化：{relative_path}")
            manifest.append(
                LocalManifestItem(
                    client_item_id=str(saved["client_item_id"]),
                    source_root_ref=source_root_ref,
                    source_relative_path=relative_path,
                    original_filename=local_path.name,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    expected_sha256=str(saved["expected_sha256"]),
                    local_path=local_path,
                )
            )
        resume_state = await self.client.resume_batch(batch_id=batch_id)
        receivable_ids = {str(item_id) for item_id in resume_state.get("receivable_item_ids", [])}
        server_items = await self.client.batch_items(batch_id=batch_id)
        item_map = {str(item["client_item_id"]): item for item in server_items}
        return await self._upload_manifest(
            batch_id=batch_id,
            manifest=manifest,
            item_map=item_map,
            receivable_ids=receivable_ids,
        )

    def _build_manifest(
        self,
        *,
        source_root_ref: str,
        relative_directory: str,
        recursive: bool,
    ) -> list[LocalManifestItem]:
        """冻结大小、修改时间和哈希，确保传输期间变更会被检测。"""

        manifest: list[LocalManifestItem] = []
        for relative_path, local_path in self.client.roots.enumerate_files(
            source_root_ref=source_root_ref,
            relative_directory=relative_directory,
            recursive=recursive,
        ):
            stat = local_path.stat()
            identity = hashlib.sha256(
                f"{source_root_ref}\0{relative_path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()
            ).hexdigest()[:32]
            manifest.append(
                LocalManifestItem(
                    client_item_id=f"local-{identity}",
                    source_root_ref=source_root_ref,
                    source_relative_path=relative_path,
                    original_filename=local_path.name,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    expected_sha256=file_sha256(local_path),
                    local_path=local_path,
                )
            )
        return manifest

    async def _upload_manifest(
        self,
        *,
        batch_id: str,
        manifest: list[LocalManifestItem],
        item_map: dict[str, dict[str, Any]],
        receivable_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """只上传后端尚未绑定版本的条目，单项失败不阻止其他文件。"""

        pending_uploads: list[tuple[LocalManifestItem, dict[str, Any]]] = []
        failed: list[dict[str, str]] = []
        for local_item in manifest:
            server_item = item_map.get(local_item.client_item_id)
            if server_item is None:
                failed.append({"source_relative_path": local_item.source_relative_path, "error": "清单项缺失"})
                continue
            if receivable_ids is not None and str(server_item["id"]) not in receivable_ids:
                continue
            if server_item.get("actual_sha256") or server_item.get("stage") != "RECEIVE":
                continue
            pending_uploads.append((local_item, server_item))

        semaphore = asyncio.Semaphore(self.concurrency)

        async def upload_one(
            local_item: LocalManifestItem,
            server_item: dict[str, Any],
        ) -> dict[str, str] | None:
            """在受控并发内上传单项，并把失败收敛为逐文件结果。"""

            try:
                self._validate_local_snapshot(local_item)
                async with semaphore:
                    await self.client.file_ingest(
                        batch_id=batch_id,
                        item_id=str(server_item["id"]),
                        source_root_ref=local_item.source_root_ref,
                        source_relative_path=local_item.source_relative_path,
                    )
                return None
            except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
                return {"source_relative_path": local_item.source_relative_path, "error": str(exc)}

        upload_results = await asyncio.gather(
            *(upload_one(local_item, server_item) for local_item, server_item in pending_uploads)
        )
        for result in upload_results:
            if result is not None:
                failed.append(result)
        upload_failures = sum(result is not None for result in upload_results)
        return {
            "batch": await self.client.batch_get(batch_id=batch_id),
            "uploaded_count": len(pending_uploads) - upload_failures,
            "transfer_failed_count": len(failed),
            "transfer_failures": failed,
        }

    @staticmethod
    def _validate_local_snapshot(item: LocalManifestItem) -> None:
        """上传前再次校验来源快照，变化文件不能静默沿用旧清单事件。"""

        stat = item.local_path.stat()
        if stat.st_size != item.size_bytes or stat.st_mtime_ns != item.mtime_ns:
            raise ValueError(f"SOURCE_CHANGED: {item.source_relative_path}")
        if file_sha256(item.local_path) != item.expected_sha256:
            raise ValueError(f"SOURCE_CHANGED: {item.source_relative_path}")

    @staticmethod
    def _manifest_digest(
        manifest: list[LocalManifestItem],
        *,
        relative_directory: str,
        recursive: bool,
        user_request: str | None,
        placement_mode: str,
        rule_profile: str,
    ) -> str:
        """计算覆盖清单、目录范围和附带请求的稳定批次幂等摘要。"""

        payload = {
            "relative_directory": relative_directory,
            "recursive": recursive,
            "user_request": user_request,
            "placement_mode": placement_mode,
            "rule_profile": rule_profile,
            "items": [item.api_payload() for item in manifest],
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _state_payload(*, batch_id: str, manifest: list[LocalManifestItem]) -> dict[str, Any]:
        """构造不含令牌和绝对路径的本地恢复状态。"""

        return {
            "batch_id": batch_id,
            "source_root_ref": manifest[0].source_root_ref,
            "items": [item.api_payload() for item in manifest],
        }
