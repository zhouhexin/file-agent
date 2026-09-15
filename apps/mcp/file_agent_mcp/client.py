"""File Agent 集成 API 客户端与本地逻辑根边界。"""

from __future__ import annotations

import json
import mimetypes
import os
import hashlib
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urljoin
from uuid import uuid4

import httpx


_EXTRACTION_PAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
}


def _extraction_page_suffix(content_type: object) -> str:
    """按服务端已验证的图片 MIME 保存扩展名，方便宿主 OCR 工具识别格式。"""

    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    return _EXTRACTION_PAGE_SUFFIXES.get(normalized, ".image")


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

    def permits_cache_root(self, root: Path) -> bool:
        """仅允许桥接层写入显式登记的独立缓存根，不能借用整个 WorkBuddy 数据目录。"""

        return not root.is_symlink() and root.resolve() in self._roots

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

    def resolve_trusted_cache_file(self, *, local_path: str, filename: str) -> Path:
        """解析受信提交单中的缓存文件。

        该方法只供 ``workbuddy_submission_ingest`` 的本机可信提交单使用。WorkBuddy
        blob 的物理文件名通常是内容哈希，不能要求它等于用户可见的原始文件名；旧的
        ``resolve`` 和手写参数入口仍保持同名校验，不能用此方法放宽。
        """

        if (
            not filename
            or len(filename) > 255
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
        ):
            raise ValueError("filename 必须是单个安全文件名")
        if not self._roots:
            raise ValueError("WorkBuddy 附件入口未配置授权缓存根")
        raw_path = Path(local_path).expanduser()
        if not raw_path.is_absolute() or raw_path.is_symlink():
            raise ValueError("WorkBuddy 附件缓存文件不是允许的普通绝对路径")
        resolved = raw_path.resolve()
        if not any(resolved != root and root in resolved.parents for root in self._roots):
            raise ValueError("WorkBuddy 附件不在已授权缓存根内")
        try:
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise ValueError("WorkBuddy 附件缓存文件不存在") from exc
        if not stat.S_ISREG(mode):
            raise ValueError("WorkBuddy 附件必须是普通文件")
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

    async def conversation_task(
        self,
        *,
        conversation_id: str,
        content: str,
        document_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """通过聊天主入口执行文件任务，只接收稳定文件 ID，不接受路径或正文。"""

        return self._business_json(
            await self.http.post(
                f"/api/conversations/{conversation_id}/messages",
                json={
                    "content": content,
                    "attachments": [
                        {"document_id": document_id}
                        for document_id in (document_ids or [])
                    ],
                },
            )
        )

    async def file_search(
        self,
        *,
        conversation_id: str,
        query: str,
        top_k: int = 10,
    ) -> dict[str, Any]:
        """调用后端只读搜索接口，避免搜索文字被通用 Agent 解释为文件写操作。"""

        payload = self._business_json(
            await self.http.post(
                "/api/search",
                json={
                    "query": query,
                    "conversation_id": conversation_id,
                    "attachment_document_ids": [],
                    "top_k": top_k,
                },
            )
        )
        for item in payload.get("files", []):
            if not isinstance(item, dict):
                continue
            for key in ("preview_url", "download_url"):
                value = str(item.get(key) or "").strip()
                if value.startswith("/"):
                    item[key] = urljoin(str(self.http.base_url), value)
        return payload

    async def classification_placement_submit(
        self,
        *,
        command: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """提交一个已经冻结版本和修订号的分类落位命令，不传递本机路径。"""

        working_copy_id = str(command.get("working_copy_id") or "").strip()
        action = str(command.get("action") or "").strip()
        if not working_copy_id or action not in {"SET_PRIMARY", "MOVE"}:
            raise ValueError("分类落位命令缺少稳定 working_copy_id 或合法 action")
        payload = {
            key: value
            for key, value in command.items()
            if key not in {"working_copy_id", "action"}
        }
        suffix = "primary-category" if action == "SET_PRIMARY" else "placement"
        return self._business_json(
            await self.http.post(
                f"/api/integrations/v1/working-copies/{_path_segment(working_copy_id)}/{suffix}",
                json=payload,
                headers={"X-Request-ID": request_id},
            )
        )

    async def download_working_copy(
        self,
        *,
        working_copy_id: str,
        output_dir: Path,
        max_bytes: int = 512 * 1024 * 1024,
    ) -> dict[str, Any]:
        """鉴权下载工作副本到 MCP 专用缓存，并返回标准资源引用。

        下载目录来自本机固定配置，不接受 Tool 参数；文件名只信任后端响应头并再次
        校验为 basename。临时文件达到大小上限或请求失败时会被删除，不能留下被误认
        为完整文件的半成品。
        """

        if max_bytes <= 0:
            raise ValueError("本地下载大小上限必须大于 0")
        configured_root = output_dir.expanduser()
        if configured_root.is_symlink():
            raise ValueError("本地下载缓存目录不能是符号链接")
        root = configured_root.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("本地下载缓存目录无效")

        cache_key = hashlib.sha256(working_copy_id.encode("utf-8")).hexdigest()[:24]
        target_dir = (root / cache_key).resolve()
        if root not in target_dir.parents:
            raise ValueError("本地下载缓存目标越出固定目录")
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        async with self.http.stream(
            "GET",
            f"/api/working-copies/{_path_segment(working_copy_id)}/download",
        ) as response:
            if response.is_error:
                await response.aread()
                self._business_json(response)
            declared_size = _content_length(response.headers.get("content-length"))
            if declared_size is not None and declared_size > max_bytes:
                raise RuntimeError("文件超过本机下载大小上限")
            filename = _download_filename(
                response.headers.get("content-disposition"),
                fallback=f"file-{cache_key}",
            )
            raw_target = target_dir / filename
            if raw_target.is_symlink():
                raise RuntimeError("本地下载缓存文件不能是符号链接")
            target = raw_target.resolve()
            if target_dir not in target.parents:
                raise RuntimeError("后端返回的下载文件名不安全")
            temporary = target_dir / f".{uuid4().hex}.part"
            written = 0
            try:
                with temporary.open("xb") as destination:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise RuntimeError("文件超过本机下载大小上限")
                        destination.write(chunk)
                os.replace(temporary, target)
                os.chmod(target, 0o600)
            finally:
                if temporary.exists():
                    temporary.unlink()

        return {
            "filename": filename,
            "resource_uri": target.as_uri(),
            "content_type": str(response.headers.get("content-type") or "").split(
                ";", 1
            )[0]
            or "application/octet-stream",
            "size_bytes": written,
        }

    async def classification_placement_status(
        self,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        """读取分类落位的后端事实状态；该请求没有文件写入副作用。"""

        return self._business_json(
            await self.http.get(
                f"/api/integrations/v1/placement-operations/{_path_segment(operation_id)}"
            )
        )

    async def evidence_answer(
        self,
        *,
        conversation_id: str,
        question: str,
        document_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """调用后端证据回答入口，禁止调用方自行提交引用或模型生成参数。"""

        return self._business_json(
            await self.http.post(
                f"/api/conversations/{conversation_id}/evidence-answer",
                json={
                    "question": question,
                    "attachment_document_ids": document_ids or [],
                },
            )
        )

    async def resolve_file_search_clarification(
        self,
        *,
        clarification_id: str,
        option_id: str | None,
        option_ids: list[str],
        custom_phrase: str | None,
    ) -> dict[str, Any]:
        """原样提交后端签发的搜索澄清选项，不能让模型伪造文件范围。"""

        return self._business_json(
            await self.http.post(
                f"/api/file-search/clarifications/{_path_segment(clarification_id)}/resolve",
                json={
                    "option_id": option_id,
                    "option_ids": option_ids,
                    "custom_phrase": custom_phrase,
                },
            )
        )

    async def operation_plan_get(self, *, plan_id: str) -> dict[str, Any]:
        """读取当前用户自己的文件操作计划，供 WorkBuddy 恢复待确认状态。"""

        return self._business_json(
            await self.http.get(f"/api/operations/plans/{_path_segment(plan_id)}")
        )

    async def operation_plan_confirm(
        self,
        *,
        plan_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        """提交用户明确确认；真正执行仍由后端白名单执行器和修订检查控制。"""

        return self._business_json(
            await self.http.post(
                f"/api/operations/plans/{_path_segment(plan_id)}/confirm",
                json={"confirmation": confirmation},
            )
        )

    async def duplicate_review_get(self, *, item_id: str) -> dict[str, Any]:
        """恢复一个批次条目的最新重复候选和允许决定。"""

        response = await self.http.get(
            f"/api/integrations/v1/ingest-items/{item_id}/duplicate-review"
        )
        return self._business_json(response)

    async def duplicate_comparison_get(
        self, *, item_id: str, review_id: str, review_revision: int, candidate_id: str, group_revision: int | None = None
    ) -> dict[str, Any]:
        """读取重复候选的脱敏对比入口，不下载正文或替用户提交决定。"""

        params: dict[str, Any] = {"review_id": review_id, "review_revision": review_revision, "candidate_id": candidate_id}
        if group_revision is not None:
            params["group_revision"] = group_revision
        return self._business_json(await self.http.get(
            f"/api/integrations/v1/ingest-items/{_path_segment(item_id)}/duplicate-comparison", params=params
        ))

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
            suffix = _extraction_page_suffix(page.get("content_type"))
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


def _path_segment(value: str) -> str:
    """把后端业务 ID 固定为单个 URL 路径段，拒绝空值并编码路径分隔符。"""

    normalized = value.strip()
    if not normalized or len(normalized) > 200 or "\x00" in normalized:
        raise ValueError("后端业务 ID 不能为空或超过长度限制")
    return quote(normalized, safe="")


def _content_length(value: str | None) -> int | None:
    """解析可信响应头中的长度；缺失或格式异常时由流式累计继续限制。"""

    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _download_filename(value: str | None, *, fallback: str) -> str:
    """从 Content-Disposition 提取安全 basename，拒绝目录和控制字符。"""

    candidates: list[str] = []
    if value:
        encoded = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", value, re.IGNORECASE)
        if encoded:
            candidates.append(unquote(encoded.group(1).strip().strip('"')))
        plain = re.search(r'filename\s*=\s*"([^"]+)"', value, re.IGNORECASE)
        if plain:
            candidates.append(plain.group(1))
        else:
            unquoted = re.search(r"filename\s*=\s*([^;]+)", value, re.IGNORECASE)
            if unquoted:
                candidates.append(unquoted.group(1).strip())
    candidates.append(fallback)
    for candidate in candidates:
        normalized = candidate.strip()
        if (
            normalized
            and len(normalized) <= 255
            and normalized not in {".", ".."}
            and "/" not in normalized
            and "\\" not in normalized
            and "\x00" not in normalized
            and not any(ord(character) < 32 for character in normalized)
        ):
            return normalized
    raise RuntimeError("后端没有返回安全的下载文件名")
