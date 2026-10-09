"""WorkBuddy 定向 PDF 区域领取、OCR 回传与已存证据读取。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from mcp.types import CallToolResult, ResourceLink, TextContent
from pydantic import Field

from .client import FileAgentIntegrationClient
from .scoped_pdf_types import ReviewProbePayload, ReviewRegionRejectPayload, ScopedSubmitPayload


def register_scoped_pdf_tools(mcp, client_factory, download_dir):
    @mcp.tool(name="scoped_pdf_source_get", description="读取 B–I 与 L 定向来源状态；SCOPE_COMPLETE 只代表冻结区域已覆盖。", structured_output=True)
    async def get(task_id: UUID) -> dict[str, Any]:
        client = client_factory()
        try:
            return client._business_json(await client.http.get(f"/api/integrations/v1/scoped-pdf-sources/{task_id}"))
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_source_claim", description=(
        "领取 B–I 与 L 定向来源；新版任务先下载身份区域，L 页由服务端倒序定位后再冻结。"
        "把返回的 PNG 分别交给 WorkBuddy 实际 OCR Provider；禁止把整份 PDF 交给 OCR。"), structured_output=False)
    async def claim(task_id: UUID, worker_id: Annotated[str, Field(min_length=1, max_length=160)]):
        client = client_factory()
        try:
            receipt = client._business_json(await client.http.post(
                f"/api/integrations/v1/scoped-pdf-sources/{task_id}/claim", json={"worker_id": worker_id}))
            token = receipt["lease_token"]
            base = Path(download_dir()).expanduser().resolve() / "scoped-pdf" / str(task_id)
            base.mkdir(parents=True, exist_ok=True)
            regions = []
            links = []
            for region in receipt["regions"]:
                region_id = region["region_id"]
                response = await client.http.get(
                    f"/api/integrations/v1/scoped-pdf-sources/{task_id}/regions/{region_id}",
                    params={"worker_id": worker_id}, headers={"X-Extraction-Lease-Token": token})
                response.raise_for_status()
                image_bytes = response.content
                digest = hashlib.sha256(image_bytes).hexdigest()
                if digest != response.headers.get("X-Region-SHA256"):
                    raise ValueError("SCOPE_IMAGE_CHANGED: 区域下载校验失败")
                path = base / f"{region_id}-{digest[:16]}.png"
                path.write_bytes(image_bytes)
                regions.append({**region, "image_sha256": digest, "resource_uri": path.as_uri(),
                                "mime_type": "image/png", "size_bytes": len(image_bytes)})
                links.append(ResourceLink(type="resource_link", name=f"{region_id}.png", uri=path.as_uri(),
                                          mimeType="image/png", size=len(image_bytes)))
            result = {**receipt, "regions": regions, "downloaded": True}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
                                           *links], structuredContent=result)
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_review_discovery_upgrade", description=(
        "旧 v2 任务的 L 裁剪图没有考核栏目时，将尚未完成的任务升级为 v3 倒序定位；"
        "旧任务租约随即失效，返回新的 task_id，随后必须对新任务重新 claim。"),
        structured_output=True)
    async def review_discovery_upgrade(task_id: UUID) -> dict[str, Any]:
        """只通过受控 API 重签旧任务，不自行改变 PDF 页码或来源版本。"""
        client = client_factory()
        try:
            return client._business_json(await client.http.post(
                f"/api/integrations/v1/scoped-pdf-sources/{task_id}/review-discovery-upgrade"))
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_fixed25_upgrade", description=(
        "把尚未完成的 v2/v3 定向任务重签为只检查物理第 25 页的 v4 任务；"
        "返回新 task_id，旧租约和旧范围随即作废。"), structured_output=True)
    async def fixed25_upgrade(task_id: UUID) -> dict[str, Any]:
        client = client_factory()
        try:
            return client._business_json(await client.http.post(
                f"/api/integrations/v1/scoped-pdf-sources/{task_id}/fixed25-upgrade"))
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_review_probe", description=(
        "获取服务端当前倒序候选页的低分辨率定位 PNG；每次仅一页，找到考核栏目即停止。"),
        structured_output=False)
    async def review_probe(task_id: UUID, worker_id: Annotated[str, Field(min_length=1, max_length=160)],
                           lease_token: Annotated[str, Field(min_length=20, max_length=500)]):
        """只下载服务端签发的当前候选页，并核对图像哈希。"""
        client = client_factory()
        try:
            path = f"/api/integrations/v1/scoped-pdf-sources/{task_id}/review-probe"
            headers = {"X-Extraction-Lease-Token": lease_token}
            receipt = client._business_json(await client.http.get(path,
                params={"worker_id": worker_id}, headers=headers))
            if (receipt.get("review_discovery") or {}).get("status") != "DISCOVERING":
                return receipt
            response = await client.http.get(f"{path}/image", params={"worker_id": worker_id},
                                             headers=headers)
            response.raise_for_status()
            digest = hashlib.sha256(response.content).hexdigest()
            if digest != receipt.get("image_sha256") or digest != response.headers.get("X-Region-SHA256"):
                raise ValueError("SCOPE_IMAGE_CHANGED: 倒序定位图像校验失败")
            base = Path(download_dir()).expanduser().resolve() / "scoped-pdf" / str(task_id)
            base.mkdir(parents=True, exist_ok=True)
            image_path = base / f"review-probe-{receipt['page_number']}-{digest[:16]}.png"
            image_path.write_bytes(response.content)
            result = {**receipt, "resource_uri": image_path.as_uri(), "mime_type": "image/png",
                      "size_bytes": len(response.content)}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
                ResourceLink(type="resource_link", name=image_path.name, uri=image_path.as_uri(),
                             mimeType="image/png", size=len(response.content))], structuredContent=result)
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_review_probe_submit", description=(
        "提交当前候选页真实 OCR 标题定位结果；服务端只允许按页码递减，命中后冻结 L 页。"),
        structured_output=True)
    async def review_probe_submit(task_id: UUID, payload: ReviewProbePayload) -> dict[str, Any]:
        """回传真正 OCR 的当前页结论，由服务端控制递减顺序。"""
        client = client_factory()
        try:
            return client._business_json(await client.http.post(
                f"/api/integrations/v1/scoped-pdf-sources/{task_id}/review-probe/results",
                json=payload.model_dump(mode="json")))
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_review_region", description=(
        "下载服务器冻结的 annual_review 裁剪图；v4 仅为第 25 页候选图，须核对真实栏目。"),
        structured_output=False)
    async def review_region(task_id: UUID, worker_id: Annotated[str, Field(min_length=1, max_length=160)],
                            lease_token: Annotated[str, Field(min_length=20, max_length=500)]):
        """只下载已冻结的 L 栏目裁剪图，供正式 OCR 或复用定位文本。"""
        client = client_factory()
        try:
            root = f"/api/integrations/v1/scoped-pdf-sources/{task_id}"
            receipt = client._business_json(await client.http.get(root))
            if (receipt.get("review_discovery") or {}).get("status") != "FOUND":
                raise ValueError("REVIEW_DISCOVERY_NOT_COMPLETE: 尚未冻结考核栏目")
            region = next(item for item in receipt["regions"] if item["region_id"] == "annual_review")
            response = await client.http.get(f"{root}/regions/annual_review",
                params={"worker_id": worker_id}, headers={"X-Extraction-Lease-Token": lease_token})
            response.raise_for_status()
            digest = hashlib.sha256(response.content).hexdigest()
            if digest != response.headers.get("X-Region-SHA256"):
                raise ValueError("SCOPE_IMAGE_CHANGED: 考核栏目图像校验失败")
            base = Path(download_dir()).expanduser().resolve() / "scoped-pdf" / str(task_id)
            base.mkdir(parents=True, exist_ok=True)
            image_path = base / f"annual_review-{digest[:16]}.png"
            image_path.write_bytes(response.content)
            result = {**region, "image_sha256": digest, "scope_digest": receipt["scope_digest"],
                      "resource_uri": image_path.as_uri(), "mime_type": "image/png",
                      "size_bytes": len(response.content)}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
                ResourceLink(type="resource_link", name=image_path.name, uri=image_path.as_uri(),
                             mimeType="image/png", size=len(response.content))], structuredContent=result)
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_review_region_reject", description=(
        "冻结的 L 栏目图缺少真实考核栏目时提交该图 SHA；v4 将 L 留空，旧 v3 才继续向前查找；"
        "只能用于真实误裁剪，不能以合格或字迹不清为由跳过真实栏目。"),
        structured_output=True)
    async def review_region_reject(task_id: UUID, payload: ReviewRegionRejectPayload) -> dict[str, Any]:
        """把错误裁剪的图像哈希交回服务端，由当前契约决定留空或继续定位。"""
        client = client_factory()
        try:
            return client._business_json(await client.http.post(
                f"/api/integrations/v1/scoped-pdf-sources/{task_id}/review-region/reject",
                json=payload.model_dump(mode="json")))
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_source_submit", description=(
        "提交冻结区域的真实 OCR 原文、B–I 已识别列的逐字证据及 L 列状态；v4 允许省略取不到的 B–I 列，哈希错误或范围外来源会失败。"
        "成功返回 SCOPE_COMPLETE 和正式 extraction_run_id。"), structured_output=True)
    async def submit(task_id: UUID, payload: ScopedSubmitPayload) -> dict[str, Any]:
        client = client_factory()
        try:
            result = client._business_json(await client.http.post(
                f"/api/integrations/v1/scoped-pdf-sources/{task_id}/results",
                json=payload.model_dump(mode="json")))
            if result.get("status") == "SCOPE_COMPLETE":
                cache = Path(download_dir()).expanduser().resolve() / "scoped-pdf" / str(task_id)
                if cache.is_dir() and not cache.is_symlink():
                    for path in cache.glob("*.png"):
                        if path.is_file() and not path.is_symlink():
                            path.unlink(missing_ok=True)
            return result
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_source_read", description="读取已保存的 B–I 与 L 定向 OCR 区域和正式 source_unit_id，供 Excel 逐列证据引用。", structured_output=True)
    async def read(document_id: UUID, document_version_id: UUID, extraction_run_id: UUID) -> dict[str, Any]:
        client = client_factory()
        try:
            return await client.dataset_request("GET", f"/sources/{document_id}?document_version_id={document_version_id}"
                f"&extraction_run_id={extraction_run_id}&content_origin=WORKBUDDY_SCOPED")
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_source_renew", description="定向区域 OCR 接近租约期限时续期同一 Worker，不重新 OCR。", structured_output=True)
    async def renew(task_id: UUID, worker_id: Annotated[str, Field(min_length=1, max_length=160)],
                    lease_token: Annotated[str, Field(min_length=20, max_length=500)]) -> dict[str, Any]:
        client = client_factory()
        try:
            return client._business_json(await client.http.post(
                f"/api/integrations/v1/scoped-pdf-sources/{task_id}/renew",
                json={"worker_id": worker_id, "lease_token": lease_token}))
        finally:
            await client.close()
