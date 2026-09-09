"""把 WorkBuddy 会话附件转换成 File Agent 的固定、受控导入清单。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .client import FileAgentIntegrationClient, WorkBuddyAttachmentRegistry, file_sha256


_SUBMISSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_ATTACHMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


@dataclass(slots=True)
class WorkBuddyAttachment:
    """一项由 WorkBuddy 提交、经本机缓存根校验的附件快照。"""

    attachment_id: str
    filename: str
    local_path: Path
    size_bytes: int
    mtime_ns: int
    sha256: str

    def api_payload(self) -> dict[str, Any]:
        """生成不含本机绝对路径的后端来源记录。"""

        logical_path = f"submitted-attachments/{self.attachment_id}/{self.filename}"
        return {
            "client_item_id": f"attachment-{hashlib.sha256(self.attachment_id.encode()).hexdigest()[:32]}",
            "source_root_ref": "workbuddy-attachments",
            "source_relative_path": logical_path,
            "original_filename": self.filename,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "expected_sha256": self.sha256,
        }


class AttachmentTransferService:
    """固定会话附件清单并复用既有隔离、查重和自动整理流水线。"""

    def __init__(
        self,
        client: FileAgentIntegrationClient,
        registry: WorkBuddyAttachmentRegistry,
    ) -> None:
        """注入后端客户端和附件缓存授权边界。"""

        self.client = client
        self.registry = registry

    async def ingest(
        self,
        *,
        submission_id: str,
        attachments: list[dict[str, Any]],
        user_request: str | None,
        placement_mode: str,
        rule_profile: str,
    ) -> dict[str, Any]:
        """按一次 WorkBuddy 提交事件导入全部附件，空任务文字仍执行默认整理。"""

        if not _SUBMISSION_ID_PATTERN.fullmatch(submission_id):
            raise ValueError("submission_id 必须是 WorkBuddy 提交事件的稳定安全标识")
        if not attachments:
            raise ValueError("attachments 不能为空")
        if len(attachments) > 1000:
            raise ValueError("单次 WorkBuddy 附件数量超过客户端安全上限")
        if placement_mode not in {"BY_CATEGORY", "NEUTRAL"}:
            raise ValueError("placement_mode 只能是 BY_CATEGORY 或 NEUTRAL")
        if rule_profile not in {"content_based", "legacy_school_materials"}:
            raise ValueError("rule_profile 不是 File Agent 允许的固定规则集")

        manifest = [self._resolve_attachment(raw) for raw in attachments]
        attachment_ids = [item.attachment_id for item in manifest]
        if len(attachment_ids) != len(set(attachment_ids)):
            raise ValueError("同一次提交中的 attachment_id 不能重复")
        payloads = [item.api_payload() for item in manifest]
        manifest_digest = self._manifest_digest(
            payloads=payloads,
            user_request=user_request,
            placement_mode=placement_mode,
            rule_profile=rule_profile,
        )
        batch = await self.client.create_batch(
            {
                "client_id": "workbuddy-attachment",
                # 请求 ID 携带清单摘要并受后端批次指纹保护；同一提交事件若附件变化，
                # 稳定幂等键不变而请求指纹变化，后端会关闭式返回 IDEMPOTENCY_CONFLICT。
                "request_id": f"attachment-{submission_id}-{manifest_digest[:16]}",
                # 幂等键只绑定 WorkBuddy 提交事件；同一事件若附件或策略变化，后端指纹校验必须报冲突，
                # 不能因为客户端重算新键而悄悄创建第二批文件。
                "idempotency_key": f"attachment-submit-{hashlib.sha256(submission_id.encode()).hexdigest()}",
                "source_root_ref": "workbuddy-attachments",
                "relative_directory": "submitted-attachments",
                "recursive": False,
                "ingest_policy": "AUTO_ORGANIZE",
                "placement_mode": placement_mode,
                "rule_profile": rule_profile,
                "user_request": user_request,
                "conversation_id": None,
            }
        )
        batch_id = str(batch["id"])
        if batch.get("manifest_status") == "SEALED":
            server_items = await self.client.batch_items(batch_id=batch_id)
        else:
            server_items = []
            for offset in range(0, len(payloads), 200):
                response = await self.client.append_items(
                    batch_id=batch_id,
                    items=payloads[offset : offset + 200],
                )
                server_items.extend(response.get("items", []))
            await self.client.seal_batch(batch_id=batch_id)
        item_map = {str(item["client_item_id"]): item for item in server_items}
        return await self._upload(batch_id=batch_id, manifest=manifest, item_map=item_map)

    def _resolve_attachment(self, raw: dict[str, Any]) -> WorkBuddyAttachment:
        """严格校验 WorkBuddy 结构化附件，不能信任模型生成的大小或文件名。"""

        allowed_keys = {"attachment_id", "filename", "local_path"}
        if not isinstance(raw, dict) or set(raw) != allowed_keys:
            raise ValueError("每个附件必须且只能包含 attachment_id、filename、local_path")
        attachment_id = str(raw["attachment_id"]).strip()
        filename = str(raw["filename"]).strip()
        if not _ATTACHMENT_ID_PATTERN.fullmatch(attachment_id):
            raise ValueError("attachment_id 不是稳定安全标识")
        if (
            not filename
            or len(filename) > 255
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
        ):
            raise ValueError("filename 必须是单个安全文件名")
        path = self.registry.resolve(local_path=str(raw["local_path"]), filename=filename)
        stat_result = path.stat()
        return WorkBuddyAttachment(
            attachment_id=attachment_id,
            filename=filename,
            local_path=path,
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            sha256=file_sha256(path),
        )

    async def _upload(
        self,
        *,
        batch_id: str,
        manifest: list[WorkBuddyAttachment],
        item_map: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """逐项上传仍处于接收阶段的附件，单项失败不阻断同批其他附件。"""

        failures: list[dict[str, str]] = []
        semaphore = asyncio.Semaphore(2)

        async def upload_one(item: WorkBuddyAttachment) -> bool:
            """上传前再次核验缓存快照，防止提交后缓存文件被替换。"""

            server_item = item_map.get(str(item.api_payload()["client_item_id"]))
            if server_item is None:
                failures.append({"attachment_id": item.attachment_id, "error": "清单项缺失"})
                return False
            if server_item.get("actual_sha256") or server_item.get("stage") != "RECEIVE":
                return False
            try:
                current = item.local_path.stat()
                if current.st_size != item.size_bytes or current.st_mtime_ns != item.mtime_ns:
                    raise ValueError("SOURCE_CHANGED")
                if file_sha256(item.local_path) != item.sha256:
                    raise ValueError("SOURCE_CHANGED")
                async with semaphore:
                    await self.client.upload_resolved_file(
                        batch_id=batch_id,
                        item_id=str(server_item["id"]),
                        path=item.local_path,
                        filename=item.filename,
                    )
                return True
            except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
                failures.append({"attachment_id": item.attachment_id, "error": str(exc)})
                return False

        results = await asyncio.gather(*(upload_one(item) for item in manifest))
        return {
            "batch": await self.client.batch_get(batch_id=batch_id),
            "uploaded_count": sum(results),
            "transfer_failed_count": len(failures),
            "transfer_failures": failures,
        }

    @staticmethod
    def _manifest_digest(
        *,
        payloads: list[dict[str, Any]],
        user_request: str | None,
        placement_mode: str,
        rule_profile: str,
    ) -> str:
        """计算进入后端请求指纹的附件清单和冻结策略摘要。"""

        canonical = json.dumps(
            {
                "attachments": payloads,
                "user_request": user_request,
                "placement_mode": placement_mode,
                "rule_profile": rule_profile,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
