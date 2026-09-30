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
from .scoped_pdf_types import ScopedSubmitPayload


def register_scoped_pdf_tools(mcp, client_factory, download_dir):
    @mcp.tool(name="scoped_pdf_source_get", description="读取 B–I 定向来源状态；SCOPE_COMPLETE 只代表两页的冻结区域已覆盖。", structured_output=True)
    async def get(task_id: UUID) -> dict[str, Any]:
        client = client_factory()
        try:
            return client._business_json(await client.http.get(f"/api/integrations/v1/scoped-pdf-sources/{task_id}"))
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_source_claim", description=(
        "领取 B–I 定向来源，仅下载服务器冻结的第 1、3 页区域 PNG。"
        "把返回的两个 PNG 分别交给 WorkBuddy 实际 OCR Provider；禁止把整份 PDF 交给 OCR。"), structured_output=False)
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

    @mcp.tool(name="scoped_pdf_source_submit", description=(
        "提交两个冻结区域的真实 OCR 原文及 B–I 八列逐字证据；缺列、区域哈希错误或范围外来源会失败。"
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

    @mcp.tool(name="scoped_pdf_source_read", description="读取已保存的 B–I 定向 OCR 区域和正式 source_unit_id，供 Excel 逐列证据引用。", structured_output=True)
    async def read(document_id: UUID, document_version_id: UUID, extraction_run_id: UUID) -> dict[str, Any]:
        client = client_factory()
        try:
            return await client.dataset_request("GET", f"/sources/{document_id}?document_version_id={document_version_id}"
                f"&extraction_run_id={extraction_run_id}&content_origin=WORKBUDDY_SCOPED")
        finally:
            await client.close()

    @mcp.tool(name="scoped_pdf_source_renew", description="两张区域 OCR 接近租约期限时续期同一 Worker，不重新 OCR。", structured_output=True)
    async def renew(task_id: UUID, worker_id: Annotated[str, Field(min_length=1, max_length=160)],
                    lease_token: Annotated[str, Field(min_length=20, max_length=500)]) -> dict[str, Any]:
        client = client_factory()
        try:
            return client._business_json(await client.http.post(
                f"/api/integrations/v1/scoped-pdf-sources/{task_id}/renew",
                json={"worker_id": worker_id, "lease_token": lease_token}))
        finally:
            await client.close()
