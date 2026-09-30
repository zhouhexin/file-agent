"""WorkBuddy 完整文档提取 MCP 工具。

本模块只编排固定任务、受控下载、租约和强类型回传；正文读取仍由 WorkBuddy
宿主的真实图片/Word/PDF 工具完成。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from mcp.types import CallToolResult, ResourceLink, TextContent
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .extraction_drafts import ExtractionDraftStore, refresh_lease


class StrictModel(BaseModel):
    """拒绝未声明参数和非有限浮点数。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExtractionError(StrictModel):
    """宿主读取失败摘要，不包含正文或本地路径。"""

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False


class ExtractionCoverage(StrictModel):
    """宿主声明的完整、部分或失败覆盖。"""

    status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    unit_count: int = Field(ge=0, le=500)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("coverage warnings 必须是 1 到 500 字符")
        return value


class ExtractionBbox(StrictModel):
    """真实 PDF/图像归一化坐标。"""

    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_rectangle(self):
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("bbox 必须是有效矩形")
        return self


class ExtractionBlock(StrictModel):
    """宿主返回的可定位段落或表格元素。"""

    block_id: str = Field(min_length=1, max_length=120)
    kind: Literal["PARAGRAPH", "TABLE_ROW", "TABLE_CELL", "TEXT", "SEAL"]
    text: str = Field(min_length=1, max_length=20_000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    table_id: str | None = Field(default=None, min_length=1, max_length=120)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)
    bbox: ExtractionBbox | None = None

    @model_validator(mode="after")
    def validate_range(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("start/end 必须同时提供或同时省略")
        if self.end is not None and self.end <= self.start:
            raise ValueError("block end 必须大于 start")
        return self


class ExtractionUnit(StrictModel):
    """一个真实页面、Word 有序正文区段或电子表格工作表。"""

    unit_id: str = Field(min_length=1, max_length=120)
    kind: Literal["IMAGE_PAGE", "PDF_PAGE", "TEXT_SECTION", "SHEET"]
    page_number: int | None = Field(default=None, ge=1)
    sheet_name: str | None = Field(default=None, min_length=1, max_length=255)
    order: int = Field(ge=0)
    status: Literal["READ", "BLANK", "FAILED", "UNSUPPORTED"]
    text: str = Field(default="", max_length=100_000)
    blocks: list[ExtractionBlock] = Field(default_factory=list, max_length=2_000)
    blank_reason: str | None = Field(default=None, min_length=1, max_length=500)
    error: ExtractionError | None = None

    @model_validator(mode="after")
    def validate_content(self):
        if self.kind in {"TEXT_SECTION", "SHEET"} and self.page_number is not None:
            raise ValueError("Word 文本区段和电子表格工作表不能伪造 page_number")
        if self.kind in {"IMAGE_PAGE", "PDF_PAGE"} and self.page_number is None:
            raise ValueError("图片和 PDF 区段必须提供真实 page_number")
        if self.kind == "SHEET" and not self.sheet_name:
            raise ValueError("SHEET 必须提供真实 sheet_name")
        if self.kind != "SHEET" and self.sheet_name is not None:
            raise ValueError("只有 SHEET 可以提供 sheet_name")
        if self.status == "READ" and not self.text.strip():
            raise ValueError("READ 区段必须包含非空正文")
        if self.status == "BLANK" and (self.text or not self.blank_reason):
            raise ValueError("BLANK 区段必须为空且说明 blank_reason")
        if self.status in {"FAILED", "UNSUPPORTED"} and self.error is None:
            raise ValueError("失败区段必须提供结构化 error")
        if self.status != "READ" and self.blocks:
            raise ValueError("只有 READ 区段可以带 blocks")
        for block in self.blocks:
            if block.kind == "SEAL" and (self.kind not in {"IMAGE_PAGE", "PDF_PAGE"} or block.bbox is None):
                raise ValueError("印章区块必须来自真实 PDF/图片页面并带位置")
            # 仅唯一精确子串可确定偏移，重复或 OCR 不一致时拒绝猜测。
            if block.start is None:
                start = self.text.find(block.text)
                if start < 0 or self.text.find(block.text, start + 1) >= 0:
                    raise ValueError("block 无法唯一定位，请提供准确 start/end")
                block.start, block.end = start, start + len(block.text)
            if block.end > len(self.text) or self.text[block.start:block.end] != block.text:
                raise ValueError("block 文本或字符范围与 unit 正文不一致")
            if block.bbox is not None and self.kind in {"TEXT_SECTION", "SHEET"}:
                raise ValueError("Word 文本区段和电子表格工作表不能伪造 bbox")
            if self.kind == "SHEET" and block.kind == "TABLE_CELL":
                if block.row_index is None or block.column_index is None:
                    raise ValueError("SHEET 的 TABLE_CELL 必须提供真实行列索引")
        return self


class DocumentExtractionClaimSource(StrictModel):
    """已下载并校验、可交给宿主读取的固定源文件。"""

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    source_kind: Literal["IMAGE", "PDF", "WORD", "SPREADSHEET"]
    page_count: int | None = Field(default=None, ge=1)
    resource_url: str = Field(min_length=1)
    resource_uri: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentExtractionClaimOutput(StrictModel):
    """claim 必须向 MCP 宿主公开的完整结构化结果。"""

    task_id: str = Field(min_length=1)
    provider_contract_version: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_token: str = Field(min_length=1)
    lease_expires_at: str = Field(min_length=1)
    source: DocumentExtractionClaimSource
    limits: dict[str, Any]
    extraction_save_policy: Literal["DIRECT_ARCHIVE", "CONFIRM_BEFORE_SAVE"]
    confirmation_required: bool
    confirmation: dict[str, Any] = Field(default_factory=dict)
    downloaded: Literal[True]


def register_document_extraction_tools(mcp: Any, client_factory: Any, download_dir: Any) -> None:
    """同时启用文档与累计表开关时注册读取、确认和提交工具。"""

    @mcp.tool(name="document_extraction_get", description="读取 WorkBuddy 文档提取任务状态和覆盖摘要，不读取正文。", structured_output=True)
    async def get(task_id: UUID) -> dict[str, Any]:
        """恢复任务状态。"""

        client = client_factory()
        try:
            return await client.document_extraction_get(task_id=str(task_id))
        finally:
            await client.close()

    @mcp.tool(
        name="document_extraction_claim",
        description=(
            "领取固定文档任务并下载、校验原文件。结构化结果包含租约、固定源版本、哈希、保存策略和宿主本地资源；"
            "文件资源交给 WorkBuddy 真实图片、Word、PDF 或电子表格工具读取。若结构化结果缺失，必须停止，"
            "不得读取 MCP 配置、环境变量或直接调用后端 API。"
        ),
        structured_output=True,
    )
    async def claim(
        task_id: UUID,
        worker_id: Annotated[str, Field(min_length=1, max_length=160)],
    ) -> Annotated[CallToolResult, DocumentExtractionClaimOutput]:
        """claim 成功后必须确认文件下载和哈希校验也成功。"""

        client = client_factory()
        try:
            result = await client.document_extraction_claim(
                task_id=str(task_id), worker_id=worker_id, output_dir=Path(download_dir())
            )
            source = dict(result.get("source") or {})
            resource_uri = str(source.get("resource_uri") or "")
            structured = {**result, "source": source, "downloaded": True}
            # 某些 WorkBuddy 版本只把普通文本内容交给 Agent，丢弃 MCP
            # structuredContent 和 ResourceLink。先用同一个严格输出模型校验，
            # 再把完全相同的 JSON 同时放入普通内容，避免宿主被迫绕过 MCP。
            validated = DocumentExtractionClaimOutput.model_validate(structured).model_dump(mode="json")
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ),
                    ResourceLink(type="resource_link", name=str(source["filename"]), uri=resource_uri,
                                 mimeType=str(source.get("content_type") or "application/octet-stream"),
                                 size=int(source.get("size_bytes") or 0)),
                ],
                structuredContent=validated,
            )
        finally:
            await client.close()

    @mcp.tool(name="document_extraction_renew", description="宿主读取耗时较长且租约即将到期时续租；过期 token 不能恢复。", structured_output=True)
    async def renew(task_id: UUID, worker_id: Annotated[str, Field(min_length=1, max_length=160)],
                    lease_token: Annotated[str, Field(min_length=20, max_length=500)]) -> dict[str, Any]:
        """续期固定 Worker 的当前租约。"""

        client = client_factory()
        try:
            return await client.document_extraction_renew(
                task_id=str(task_id), worker_id=worker_id, lease_token=lease_token
            )
        finally:
            await client.close()

    @mcp.tool(name="document_extraction_resume", description="只恢复已有完整提取运行且归档尚未推进的旧任务；不重新 OCR、不重存正文。", structured_output=True)
    async def resume(task_id: UUID) -> dict[str, Any]:
        """由 File Agent 校验源版本、摘要与持久化运行后幂等恢复。"""

        client = client_factory()
        try:
            return await client.document_extraction_resume(task_id=str(task_id))
        finally:
            await client.close()

    @mcp.tool(
        name="document_extraction_preview",
        description=(
            "用户明确要求提取内容时，在生成完整结果后、向用户展示该结果的同一轮调用。"
            "本工具只在本机计算完整结果指纹，服务器仅收到指纹、来源和计数，不接收正文；"
            "调用后必须展示结果并结束本轮，等待用户在后续消息明确确认。"
        ),
        structured_output=True,
    )
    async def preview(
        task_id: UUID,
        worker_id: Annotated[str, Field(min_length=1, max_length=160)],
        lease_token: Annotated[str, Field(min_length=20, max_length=500)],
        preview_key: Annotated[str, Field(min_length=1, max_length=200)],
        source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        source_version_id: UUID,
        provider_name: Annotated[str, Field(min_length=1, max_length=120)],
        coverage: ExtractionCoverage,
        units: list[ExtractionUnit],
        provider_version: Annotated[str | None, Field(max_length=120)] = None,
        provider_request_id: Annotated[str | None, Field(max_length=240)] = None,
        error: ExtractionError | None = None,
    ) -> dict[str, Any]:
        """服务器只收摘要；完整本地预览随工具结果返回给当前会话用于展示和修正。"""

        digest_value = {
            "source_sha256": source_sha256,
            "source_version_id": str(source_version_id),
            "provider_name": provider_name,
            "provider_version": provider_version,
            "provider_request_id": provider_request_id,
            "coverage": coverage.model_dump(mode="json"),
            "units": [unit.model_dump(mode="json") for unit in units],
            "error": error.model_dump(mode="json") if error else None,
        }
        _validate_result(coverage, units, error)
        result_digest = hashlib.sha256(json.dumps(
            digest_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        store = ExtractionDraftStore(Path(download_dir()))
        store.save(str(task_id), worker_id, lease_token, digest_value)
        client = client_factory()
        try:
            # 在模型生成长参数之后、发送摘要之前处理过期租约，正文不需要重新生成。
            lease_token = await refresh_lease(client, str(task_id), store.load(str(task_id), result_digest))
            store.save(str(task_id), worker_id, lease_token, digest_value)
            confirmation = await client.document_extraction_prepare_confirmation(
                task_id=str(task_id),
                payload={
                    "worker_id": worker_id,
                    "lease_token": lease_token,
                    "preview_key": preview_key,
                    "source_sha256": source_sha256,
                    "source_version_id": str(source_version_id),
                    "result_digest": result_digest,
                    "provider_name": provider_name,
                    "coverage_status": coverage.status,
                    "unit_count": len(units),
                    "block_count": sum(len(unit.blocks) for unit in units),
                },
            )
            # preview_result 不进入 File Agent API 或数据库，只返回到当前 WorkBuddy
            # 工具调用上下文。这样 Agent 不必依靠调用前的临时推理记忆，可以逐项展示
            # 用户实际要确认的内容，并在用户纠正后重新构造新 preview。
            return {
                **confirmation,
                "confirmation_id": confirmation["id"],
                "cached_result_digest": result_digest,
                "content_saved": False,
                "excel_published": False,
                "display_required": True,
                "preview_result": digest_value,
                "display_instruction": (
                    "必须向用户展示 preview_result 中的实际提取内容和待复核项，"
                    "然后结束本轮；不得在同一轮确认或保存。"
                ),
            }
        finally:
            await client.close()

    @mcp.tool(
        name="document_extraction_confirm",
        description=(
            "仅在用户看到提取结果后明确同意保存时调用，把确认绑定到 preview 返回的固定结果指纹。"
        ),
        structured_output=True,
    )
    async def confirm(
        task_id: UUID,
        confirmation_id: UUID,
        result_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        save_cached_result: bool = False,
    ) -> dict[str, Any]:
        """确认工具不接收正文；含糊回复或同一轮自动继续均不得调用。"""

        client = client_factory()
        try:
            # 选择合并保存时先确认草稿可恢复，避免只剩下无法提交的确认状态。
            if save_cached_result:
                ExtractionDraftStore(Path(download_dir())).load(str(task_id), result_digest)
            confirmation = await client.document_extraction_confirm(
                task_id=str(task_id),
                confirmation_id=str(confirmation_id),
                result_digest=result_digest,
            )
            if save_cached_result:
                saved = await submit_cached(task_id, result_digest)
                return {"confirmation": confirmation, **saved}
            return {**confirmation, "content_saved": False, "excel_published": False,
                    "next_action": "document_extraction_submit_cached"}
        finally:
            await client.close()

    @mcp.tool(name="document_extraction_submit_cached", description=(
        "按 preview 的 cached_result_digest 提交本地已校验正文，无需重复长参数。"
        "仍由服务端校验用户确认；仅正文成功保存，不代表 Excel 已写入。"), structured_output=True)
    async def submit_cached(
        task_id: UUID,
        result_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    ) -> dict[str, Any]:
        """固定摘要的幂等提交；服务器确认、版本和证据校验不可绕过。"""
        store = ExtractionDraftStore(Path(download_dir()))
        draft = store.load(str(task_id), result_digest)
        client = client_factory()
        try:
            # 先使用稳定提交键重试，服务端可在租约检查前返回已成功结果。
            payload = {**draft["payload"], "worker_id": draft["worker_id"],
                       "lease_token": draft["lease_token"], "submission_key": f"cached-{result_digest}"}
            try:
                result = await client.document_extraction_submit(task_id=str(task_id), payload=payload)
            except RuntimeError as exc:
                if not str(exc).startswith("LEASE_EXPIRED:"):
                    raise
                payload["lease_token"] = await refresh_lease(client, str(task_id), draft)
                store.save(str(task_id), draft["worker_id"], payload["lease_token"], draft["payload"])
                result = await client.document_extraction_submit(task_id=str(task_id), payload=payload)
            saved = bool(result.get("ok") and (result.get("task") or {}).get("extraction_run_id"))
            return {**result, "content_saved": saved, "excel_published": False,
                    "next_action": "structured_dataset_source" if saved else "inspect_extraction_failure"}
        finally:
            await client.close()

    @mcp.tool(name="document_extraction_submit", description="提交 WorkBuddy 真实工具读取的完整文档结果。纯归档可直接提交；显式内容提取必须先完成 preview 和用户确认，且结果不能变化。", structured_output=True)
    async def submit(
        task_id: UUID,
        worker_id: Annotated[str, Field(min_length=1, max_length=160)],
        lease_token: Annotated[str, Field(min_length=20, max_length=500)],
        submission_key: Annotated[str, Field(min_length=1, max_length=200)],
        source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        source_version_id: UUID,
        provider_name: Annotated[str, Field(min_length=1, max_length=120)],
        coverage: ExtractionCoverage,
        units: list[ExtractionUnit],
        provider_version: Annotated[str | None, Field(max_length=120)] = None,
        provider_request_id: Annotated[str | None, Field(max_length=240)] = None,
        error: ExtractionError | None = None,
    ) -> dict[str, Any]:
        """嵌套 schema 与 API 对齐，结构错误在调用后端前拒绝。"""

        _validate_result(coverage, units, error)
        client = client_factory()
        try:
            payload = {
                    "worker_id": worker_id,
                    "lease_token": lease_token,
                    "submission_key": submission_key,
                    "source_sha256": source_sha256,
                    "source_version_id": str(source_version_id),
                    "provider_name": provider_name,
                    "provider_version": provider_version,
                    "provider_request_id": provider_request_id,
                    "coverage": coverage.model_dump(mode="json"),
                    "units": [unit.model_dump(mode="json") for unit in units],
                    "error": error.model_dump(mode="json") if error else None,
                }
            try:
                return await client.document_extraction_submit(task_id=str(task_id), payload=payload)
            except RuntimeError as exc:
                if not str(exc).startswith("LEASE_EXPIRED:"):
                    raise
                payload["lease_token"] = await refresh_lease(client, str(task_id), {
                    "worker_id": worker_id, "lease_token": lease_token, "payload": payload})
                return await client.document_extraction_submit(task_id=str(task_id), payload=payload)
        finally:
            await client.close()


def _validate_result(coverage, units, error) -> None:
    """预览与提交复用 API 的跨单元约束，避免先确认后发现载荷必然无效。"""
    if coverage.unit_count != len(units) or len(units) > 500:
        raise ValueError("coverage.unit_count 必须等于 units 数量，且不超过 500")
    if coverage.status == "COMPLETE" and (not units or error or any(u.status != "READ" for u in units)):
        raise ValueError("COMPLETE 必须包含成功 READ 区段且不能带整体 error")
    if coverage.status == "FAILED" and error is None:
        raise ValueError("FAILED 必须提供整体 error")
    if [u.order for u in units] != list(range(len(units))):
        raise ValueError("units 的 order 必须从 0 连续递增；不是 PDF 页码")
    ids = [u.unit_id for u in units]
    blocks = [b.block_id for u in units for b in u.blocks]
    if len(set(ids)) != len(ids) or len(set(blocks)) != len(blocks):
        raise ValueError("unit_id 和 block_id 在文件内必须唯一")
    if sum(len(u.text) for u in units) > 100_000 or len(blocks) > 2_000:
        raise ValueError("正文总长度不能超过 100000 字符，blocks 总数不能超过 2000")
