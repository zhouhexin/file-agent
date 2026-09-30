"""宿主本地不可变提取草稿；确认前正文不越过 File Agent 服务端边界。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from uuid import UUID


def result_digest(payload: dict) -> str:
    """与服务端使用相同规范 JSON 计算正文指纹，不包含租约等传输信息。"""
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class ExtractionDraftStore:
    """只接受固定任务 UUID 和摘要引用，不向模型开放任意文件读写。"""

    def __init__(self, root: Path):
        """草稿只能落到配置的宿主缓存子目录。"""
        self.root = root / "extraction-drafts"

    def _path(self, task_id: str, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("无效的草稿指纹")
        task = str(UUID(task_id))
        for path in (self.root.parent, self.root, self.root / task):
            if path.is_symlink():
                raise ValueError("草稿缓存不允许符号链接")
        path = self.root / task / f"{digest}.json"
        if path.is_symlink():
            raise ValueError("草稿文件不允许符号链接")
        return path

    def save(self, task_id: str, worker_id: str, lease_token: str, payload: dict) -> str:
        """原子保存已校验结果，重试保留相同的确定性提交键。"""
        digest = result_digest(payload)
        path = self._path(task_id, digest)
        value = {"task_id": task_id, "worker_id": worker_id, "lease_token": lease_token,
                 "result_digest": digest, "payload": payload}
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError("提取草稿超过 4 MiB 上限")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return digest

    def load(self, task_id: str, digest: str) -> dict:
        """重启后读取同一不可变草稿；丢失或损坏时绝不猜测正文。"""
        path = self._path(task_id, digest)
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("EXTRACTION_DRAFT_NOT_FOUND: 本地草稿缺失，请重新读取和预览")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("task_id") != task_id or result_digest(value["payload"]) != digest:
            raise ValueError("EXTRACTION_DRAFT_CHANGED: 草稿指纹不一致，请重新预览")
        return value


async def refresh_lease(client, task_id: str, draft: dict) -> str:
    """仅过期租约允许重新领取同一来源，最多一次，不重做 OCR 或下载。"""
    try:
        renewed = await client.document_extraction_renew(
            task_id=task_id, worker_id=draft["worker_id"], lease_token=draft["lease_token"])
        return renewed.get("lease_token", draft["lease_token"])
    except RuntimeError as exc:
        if not str(exc).startswith("LEASE_EXPIRED:"):
            raise
    claimed = await client.document_extraction_request(
        "POST", task_id, "/claim", {"worker_id": draft["worker_id"]})
    for field in ("source_version_id", "source_sha256"):
        if claimed.get(field) != draft["payload"][field]:
            raise ValueError("EXTRACTION_SOURCE_CHANGED: 任务来源变化，必须重新读取和预览")
    return claimed["lease_token"]
