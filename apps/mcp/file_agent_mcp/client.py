"""File Agent 集成 API 客户端与本地逻辑根边界。"""

from __future__ import annotations

import json
import mimetypes
import os
import hashlib
import stat
from pathlib import Path, PurePosixPath
from typing import Any

import httpx


class LocalRootRegistry:
    """把逻辑根映射到用户显式授权的本地目录，并阻止路径穿越。"""

    def __init__(self, roots: dict[str, Path]) -> None:
        """保存规范化后的根目录；不存在或非目录的配置立即失败。"""

        self._roots: dict[str, Path] = {}
        for key, path in roots.items():
            resolved = path.expanduser().resolve()
            if not key or not resolved.is_dir():
                raise ValueError(f"无效的本地授权根：{key}")
            self._roots[key] = resolved

    @classmethod
    def from_environment(cls) -> "LocalRootRegistry":
        """从 JSON 环境变量读取逻辑根，不接受工具调用临时声明绝对目录。"""

        raw = os.getenv("FILE_AGENT_LOCAL_ROOTS", "{}")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("FILE_AGENT_LOCAL_ROOTS 必须是 JSON 对象") from exc
        if not isinstance(parsed, dict):
            raise ValueError("FILE_AGENT_LOCAL_ROOTS 必须是 JSON 对象")
        return cls({str(key): Path(str(value)) for key, value in parsed.items()})

    def resolve_file(self, *, source_root_ref: str, source_relative_path: str) -> Path:
        """解析授权根内的单个普通文件，拒绝绝对路径、上级目录和符号链接越界。"""

        root = self._roots.get(source_root_ref)
        relative = PurePosixPath(source_relative_path)
        if root is None:
            raise ValueError("未授权的 source_root_ref")
        if relative.is_absolute() or any(part in {"", ".."} for part in relative.parts):
            raise ValueError("source_relative_path 必须是授权根内的 POSIX 相对路径")
        candidate = (root / Path(*relative.parts)).resolve()
        if candidate == root or root not in candidate.parents or not candidate.is_file():
            raise ValueError("来源文件不存在或已越出授权根")
        return candidate

    def enumerate_files(
        self,
        *,
        source_root_ref: str,
        relative_directory: str,
        recursive: bool,
    ) -> list[tuple[str, Path]]:
        """枚举授权目录中的普通文件，并返回稳定 POSIX 相对路径。

        目录符号链接和文件符号链接都不跟随，防止授权后通过链接把清单扩展到根外。
        """

        root = self._roots.get(source_root_ref)
        relative = PurePosixPath(relative_directory)
        if root is None:
            raise ValueError("未授权的 source_root_ref")
        if relative.is_absolute() or any(part in {"", ".."} for part in relative.parts):
            raise ValueError("relative_directory 必须是授权根内的 POSIX 相对目录")
        directory = (root / Path(*relative.parts)).resolve()
        if directory != root and root not in directory.parents:
            raise ValueError("处理目录已越出授权根")
        if not directory.is_dir():
            raise ValueError("处理目录不存在")
        pattern = "**/*" if recursive else "*"
        files: list[tuple[str, Path]] = []
        for candidate in sorted(directory.glob(pattern)):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if root not in resolved.parents:
                continue
            files.append((resolved.relative_to(root).as_posix(), resolved))
        return files


class WorkBuddyAttachmentRegistry:
    """限制 WorkBuddy 会话附件只能来自显式授权的本机缓存目录。"""

    def __init__(self, roots: list[Path]) -> None:
        """规范化缓存根；空配置关闭附件入口，避免退化为任意路径读取器。"""

        self._roots: list[Path] = []
        for path in roots:
            resolved = path.expanduser().resolve()
            if not resolved.is_dir():
                raise ValueError("WorkBuddy 附件缓存根不存在或不是目录")
            self._roots.append(resolved)

    @classmethod
    def from_environment(cls) -> "WorkBuddyAttachmentRegistry":
        """从 JSON 数组读取 WorkBuddy 已授权的附件缓存根。"""

        raw = os.getenv("FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS", "[]")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS 必须是 JSON 数组") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS 必须是路径字符串 JSON 数组")
        return cls([Path(item) for item in parsed])

    def resolve(self, *, local_path: str, filename: str) -> Path:
        """解析附件缓存文件，并拒绝越权路径、符号链接和特殊文件。"""

        if not self._roots:
            raise ValueError("WorkBuddy 附件入口未配置授权缓存根")
        raw_path = Path(local_path).expanduser()
        if not raw_path.is_absolute():
            raise ValueError("WorkBuddy attachment local_path 必须是缓存文件绝对路径")
        # 先检查路径本身而不是只检查 resolve 后结果，防止缓存根内软链接跳转到任意文件。
        if raw_path.is_symlink():
            raise ValueError("WorkBuddy 附件缓存文件不能是符号链接")
        resolved = raw_path.resolve()
        if not any(resolved != root and root in resolved.parents for root in self._roots):
            raise ValueError("WorkBuddy 附件不在已授权缓存根内")
        try:
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise ValueError("WorkBuddy 附件缓存文件不存在") from exc
        if not stat.S_ISREG(mode):
            raise ValueError("WorkBuddy 附件必须是普通文件")
        if resolved.name != filename:
            raise ValueError("WorkBuddy 附件文件名与缓存文件不一致")
        return resolved


class FileAgentIntegrationClient:
    """调用受认证的批次内容和任务查询 API。"""

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        roots: LocalRootRegistry,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """创建请求客户端；令牌仅进入 Authorization 头，不写入工具结果。"""

        if not access_token.strip():
            raise ValueError("FILE_AGENT_ACCESS_TOKEN 不能为空")
        self.roots = roots
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=httpx.Timeout(120.0, connect=15.0),
            transport=transport,
        )

    @classmethod
    def from_environment(cls, *, roots: LocalRootRegistry) -> "FileAgentIntegrationClient":
        """从进程配置构造客户端，避免验收脚本接收或输出明文访问令牌。"""

        return cls(
            base_url=os.getenv("FILE_AGENT_API_BASE_URL", "http://127.0.0.1:8000"),
            access_token=os.getenv("FILE_AGENT_ACCESS_TOKEN", ""),
            roots=roots,
        )

    async def close(self) -> None:
        """关闭连接池，供 MCP 服务生命周期回收资源。"""

        await self.http.aclose()

    async def file_ingest(
        self,
        *,
        batch_id: str,
        item_id: str,
        source_root_ref: str,
        source_relative_path: str,
    ) -> dict[str, Any]:
        """上传一个固定清单项；只返回后端结构化业务结果。"""

        path = self.roots.resolve_file(
            source_root_ref=source_root_ref,
            source_relative_path=source_relative_path,
        )
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as source:
            response = await self.http.put(
                f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
                files={"file": (path.name, source, content_type)},
            )
        return self._business_json(response)

    async def upload_resolved_file(
        self,
        *,
        batch_id: str,
        item_id: str,
        path: Path,
        filename: str,
    ) -> dict[str, Any]:
        """上传已由专用来源适配器校验的文件，不把本机路径发送给后端。"""

        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with path.open("rb") as source:
            response = await self.http.put(
                f"/api/integrations/v1/ingest-batches/{batch_id}/items/{item_id}/content",
                files={"file": (filename, source, content_type)},
            )
        return self._business_json(response)

    async def create_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """幂等创建批次，调用方必须提供已经固定的逻辑目录和策略。"""

        return self._business_json(
            await self.http.post("/api/integrations/v1/ingest-batches", json=payload)
        )

    async def append_items(
        self,
        *,
        batch_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """分页登记本地文件快照；单页数量由后端 Schema 再次限制。"""

        return self._business_json(
            await self.http.post(
                f"/api/integrations/v1/ingest-batches/{batch_id}/items",
                json={"items": items},
            )
        )

    async def seal_batch(self, *, batch_id: str) -> dict[str, Any]:
        """固定批次成员范围。"""

        return self._business_json(
            await self.http.post(f"/api/integrations/v1/ingest-batches/{batch_id}/seal")
        )

    async def batch_get(self, *, batch_id: str) -> dict[str, Any]:
        """读取批次聚合状态。"""

        return self._business_json(
            await self.http.get(f"/api/integrations/v1/ingest-batches/{batch_id}")
        )

    async def resume_batch(self, *, batch_id: str) -> dict[str, Any]:
        """请求后端返回仍可接收的原清单条目，不重置业务失败。"""

        return self._business_json(
            await self.http.post(f"/api/integrations/v1/ingest-batches/{batch_id}/resume")
        )

    async def batch_items(self, *, batch_id: str) -> list[dict[str, Any]]:
        """遍历服务端游标分页，恢复批次全部条目。"""

        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = self._business_json(
                await self.http.get(
                    f"/api/integrations/v1/ingest-batches/{batch_id}/items",
                    params=params,
                )
            )
            items.extend(page.get("items", []))
            cursor = page.get("next_cursor")
            if not cursor:
                return items

    async def batch_snapshot(self, *, batch_id: str) -> dict[str, Any]:
        """恢复批次、全部逐项状态和仍待用户选择的结构化确认。"""

        batch = await self.batch_get(batch_id=batch_id)
        items = await self.batch_items(batch_id=batch_id)
        pending_reviews: list[dict[str, Any]] = []
        for item in items:
            if item.get("status") != "WAITING_DUPLICATE_CONFIRMATION":
                continue
            pending_reviews.append(await self.duplicate_review_get(item_id=str(item["id"])))
        return {
            "batch": batch,
            "items": items,
            "pending_duplicate_reviews": pending_reviews,
        }

    async def job_get(self, *, job_id: str) -> dict[str, Any]:
        """查询当前令牌所属用户可见的异步任务状态。"""

        response = await self.http.get(f"/api/jobs/{job_id}")
        return self._business_json(response)

    async def duplicate_review_get(self, *, item_id: str) -> dict[str, Any]:
        """恢复一个批次条目的最新重复候选和允许决定。"""

        response = await self.http.get(
            f"/api/integrations/v1/ingest-items/{item_id}/duplicate-review"
        )
        return self._business_json(response)

    async def duplicate_decide(
        self,
        *,
        item_id: str,
        review_id: str,
        review_revision: int,
        group_revision: int | None,
        group_member_item_ids: list[str],
        decision: str,
        candidate_id: str | None,
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """原样提交用户选择的 review、修订、候选和决定。"""

        response = await self.http.post(
            f"/api/integrations/v1/ingest-items/{item_id}/duplicate-decision",
            json={
                "client_id": "workbuddy-local",
                "request_id": request_id,
                "idempotency_key": idempotency_key,
                "review_id": review_id,
                "review_revision": review_revision,
                "group_revision": group_revision,
                "group_member_item_ids": group_member_item_ids,
                "candidate_id": candidate_id,
                "decision": decision,
            },
        )
        return self._business_json(response)

    async def ingest_item_action(
        self,
        *,
        item_id: str,
        action: str,
        request_id: str,
        idempotency_key: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """提交显式取消或重试；动作名只允许固定白名单。"""

        if action not in {"retry", "cancel"}:
            raise ValueError("不支持的导入条目动作")
        return self._business_json(
            await self.http.post(
                f"/api/integrations/v1/ingest-items/{item_id}/{action}",
                json={
                    "client_id": "workbuddy-local",
                    "request_id": request_id,
                    "idempotency_key": idempotency_key,
                    "reason": reason,
                },
            )
        )

    async def extraction_claim(
        self,
        *,
        task_id: str,
        worker_id: str,
        output_dir: Path,
    ) -> dict[str, Any]:
        """领取外部 OCR 任务并把真实页面下载到受控本地暂存目录。"""

        claim = self._business_json(
            await self.http.post(
                f"/api/integrations/v1/extraction-tasks/{task_id}/claim",
                json={"worker_id": worker_id},
            )
        )
        task_dir = (output_dir.expanduser().resolve() / task_id)
        task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        local_pages = []
        for page in claim.get("pages", []):
            response = await self.http.get(
                str(page["resource_url"]),
                params={"worker_id": worker_id},
                headers={"X-Extraction-Lease-Token": claim["lease_token"]},
            )
            if response.is_error:
                self._business_json(response)
            suffix = ".png" if page.get("content_type") == "image/png" else ".image"
            target = task_dir / f"page-{int(page['page_number'])}{suffix}"
            target.write_bytes(response.content)
            os.chmod(target, 0o600)
            local_pages.append({**page, "local_path": str(target)})
        return {**claim, "pages": local_pages}

    async def extraction_renew(
        self,
        *,
        task_id: str,
        worker_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        """续期仍在运行的外部 OCR 任务。"""

        return self._business_json(
            await self.http.post(
                f"/api/integrations/v1/extraction-tasks/{task_id}/renew",
                json={"worker_id": worker_id, "lease_token": lease_token},
            )
        )

    async def extraction_submit(self, *, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """提交 OCR Provider 真实返回的逐页文本、置信度和坐标。"""

        return self._business_json(
            await self.http.post(
                f"/api/integrations/v1/extraction-tasks/{task_id}/results",
                json=payload,
            )
        )

    @staticmethod
    def _business_json(response: httpx.Response) -> dict[str, Any]:
        """把后端错误压缩为不含令牌和本地路径的 MCP 可读异常。"""

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"File Agent 返回非 JSON 响应（HTTP {response.status_code}）") from exc
        if response.is_error:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = str(error.get("code") or "INTEGRATION_API_ERROR")
            message = str(error.get("message") or f"HTTP {response.status_code}")
            raise RuntimeError(f"{code}: {message}")
        if not isinstance(payload, dict):
            raise RuntimeError("File Agent 返回了无效业务响应")
        return payload


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """分块计算本地文件哈希，供不可变清单和实际接收双重校验。"""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
