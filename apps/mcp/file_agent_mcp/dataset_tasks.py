"""表格预览本地缓存、确认后正文提交与服务器发布的受控 MCP 编排。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal, Any
from uuid import UUID

from pydantic import Field
from mcp.types import CallToolResult, ResourceLink, TextContent

from .dataset_task_contracts import TaskPayload
from .extraction_drafts import ExtractionDraftStore, result_digest, refresh_lease


def _check_template_grid(payload: dict, download_root: Path) -> None:
    """有宿主草稿时在预览前拒绝无坐标的复杂模板，不从文本推测空格。"""

    target = payload["manifest"]["target"]
    task_id, layout = target.get("uploaded_task_id"), target.get("layout")
    if not task_id or not layout:
        return
    ref = next((item for item in payload["manifest"]["extraction_confirmations"]
                if item["task_id"] == task_id), None)
    if ref is None:
        # 已直接归档的来源可能没有本地复核草稿；服务端仍在排队前强制校验。
        return
    draft = ExtractionDraftStore(download_root).load(task_id, ref["result_digest"])
    # MCP 不依赖服务器 Office 库；布局已由输入 schema 校验，这里只做确定性坐标换算。
    bounds = re.fullmatch(r"([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)", layout["header_range"].upper())
    if bounds is None:
        raise ValueError("TARGET_EXCEL_LAYOUT_INVALID: 表头范围不是有效单元格区间")
    def column_number(letters: str) -> int:
        """把 Excel 列字母映射为从 1 开始的列号。"""
        value = 0
        for letter in letters:
            value = value * 26 + ord(letter) - ord("A") + 1
        return value
    left, top = column_number(bounds[1]), int(bounds[2])
    right, bottom = column_number(bounds[3]), int(bounds[4])
    sheet_name = target.get("sheet_name")
    cells = [block for unit in draft["payload"].get("units", [])
             if unit.get("kind") == "SHEET" and (sheet_name is None or unit.get("sheet_name") == sheet_name)
             for block in unit.get("blocks", [])
             if block.get("kind") == "TABLE_CELL"
             and isinstance(block.get("row_index"), int) and isinstance(block.get("column_index"), int)
             and top <= block["row_index"] + 1 <= bottom
             and left <= block["column_index"] + 1 <= right]
    if not cells:
        raise ValueError("TARGET_EXCEL_HEADER_CELLS_MISSING: 当前 WorkBuddy 模板草稿没有表头单元格坐标，请使用结构化表格读取能力重新预览")


async def _save_confirmed_documents(client, payload, download_root):
    """确认由服务器校验；只提交原始草稿，不允许模型重新生成长正文。"""
    store = ExtractionDraftStore(Path(download_root))
    for ref in payload["manifest"]["extraction_confirmations"]:
        task_id, digest = ref["task_id"], ref["result_digest"]
        current = await client.document_extraction_request("GET", task_id, "")
        current = current.get("task", current)
        if current.get("status") == "COMPLETE":
            continue
        draft = store.load(task_id, digest)
        body = {**draft["payload"], "worker_id": draft["worker_id"], "lease_token": draft["lease_token"],
                "submission_key": f"cached-{digest}"}
        try:
            saved = await client.document_extraction_submit(task_id=task_id, payload=body)
        except RuntimeError as exc:
            if not str(exc).startswith("LEASE_EXPIRED:"):
                raise
            body["lease_token"] = await refresh_lease(client, task_id, draft)
            store.save(task_id, draft["worker_id"], body["lease_token"], draft["payload"])
            saved = await client.document_extraction_submit(task_id=task_id, payload=body)
        if not saved.get("ok"):
            raise ValueError("DATASET_CONTENT_NOT_SAVED: 来源正文未保存，Excel 尚未发布")


async def _deliver(client, result, download_root):
    """只交付真实已发布版本，宿主下载失败不重复追加或伪报发布失败。"""
    if result.get("status") != "COMPLETED" or not result.get("excel_published"):
        return result
    required = ("dataset_id", "export_id", "working_copy_id", "document_id", "document_version_id", "filename", "task_record_count")
    if any(not result.get(key) for key in required):
        raise ValueError("DATASET_PUBLICATION_INCOMPLETE: 服务器回执缺少本轮发布事实")
    try:
        resource = await client.download_dataset_export(dataset_id=result["dataset_id"],
            export_id=result["export_id"], output_dir=Path(download_root))
    except (RuntimeError, OSError):
        return {**result, "downloaded": False, "download_error": "HOST_DOWNLOAD_FAILED"}
    receipt = {**result, "downloaded": True}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(receipt, ensure_ascii=False)),
        ResourceLink(type="resource_link", name=resource["filename"], uri=resource["resource_uri"],
                     mimeType=resource["content_type"], size=resource["size_bytes"])], structuredContent=receipt)


def register_dataset_task_tools(mcp, client_factory, download_dir):
    """追加完整表格任务工具，不改变旧接口及纯归档行为。"""

    @mcp.tool(name="structured_dataset_capabilities", description=(
        "在读取 PDF/OCR 前确认 File Agent 服务端支持当前专用模板的无章来源事实与定位印章契约。"), structured_output=True)
    async def capabilities() -> dict[str, Any]:
        """只读探测真实 API，避免宿主按本地 Skill 假设服务端已升级。"""
        client = client_factory()
        try:
            result = await client.dataset_request("GET", "/tasks/capabilities")
            if result.get("contract_version") != "sealed-item-template-v1":
                raise ValueError("DATASET_CONTRACT_UNSUPPORTED: 服务端未提供当前专用模板契约")
            return result
        finally:
            await client.close()

    def draft_store():
        """固定子目录与来源草稿隔离，不允许调用方传路径。"""
        return ExtractionDraftStore(Path(download_dir()) / "dataset-tasks")

    async def advance(client, task_id, digest):
        """短循环只推进确定阶段；异步依赖立即返回，后续用同一任务恢复。"""
        state = await client.dataset_request("GET", f"/tasks/{task_id}")
        if state.get("result_digest") != digest:
            raise ValueError("DATASET_PREVIEW_CHANGED: 指纹与服务器任务不一致")
        if state.get("status") == "WAITING_CONFIRMATION":
            raise ValueError("DATASET_CONFIRMATION_REQUIRED: 必须先展示并确认完整逐列预览")
        if state.get("status") == "COMPLETED":
            return await _deliver(client, state, download_dir())
        if state.get("status") in {"FAILED", "NEEDS_INPUT"}:
            return state
        try:
            payload = draft_store().load(task_id, digest)["payload"]
        except ValueError:
            # 首次提交后完整候选已经在服务器，可在宿主缓存丢失后继续。
            if not state.get("draft_saved"):
                raise
            payload = None
        if payload is not None:
            await _save_confirmed_documents(client, payload, download_dir())
        for step in range(24):
            body = {"result_digest": digest}
            if step == 0 and payload is not None:
                body["payload"] = payload
            state = await client.dataset_request("POST", f"/tasks/{task_id}/commit", body)
            if state.get("status") == "COMPLETED":
                return await _deliver(client, state, download_dir())
            if (state.get("status") in {"WAITING_DEPENDENCY", "PREPARING_TARGET", "FAILED", "NEEDS_REVIEW"}
                    or state.get("next_action") == "wait_for_publication"):
                return state
        return {**state, "next_action": "structured_dataset_commit"}

    @mcp.tool(name="structured_dataset_preview", description=(
        "缓存完整逐列 Excel 候选并返回可展示预览，服务器仅保存指纹与目标元数据。"
        "所有列必须有原文或留空原因，无明确项号也可通过整栏审批定位。"), structured_output=True)
    async def preview(payload: TaskPayload, request_key: Annotated[str, Field(min_length=1, max_length=120)],
                      review_mode: Literal["REVIEW_REQUIRED", "DIRECT_SAVE"] = "DIRECT_SAVE") -> dict[str, Any]:
        """复核路径必须展示后结束本轮；直接路径使用同一草稿并保留明确授权。"""
        body = payload.model_dump(mode="json")
        _check_template_grid(body, Path(download_dir()))
        digest = result_digest(body)
        client = client_factory()
        try:
            result = await client.dataset_request("POST", "/tasks/preview", {
                "request_key": request_key, "review_mode": review_mode, "result_digest": digest,
                "manifest": body["manifest"]})
            draft_store().save(result["task_id"], "dataset-task", "", body)
            return {**result, "preview_result": body,
                "display_required": review_mode == "REVIEW_REQUIRED",
                "display_instruction": "按目标列顺序展示内容、来源、审批范围、标题条件和留空原因；复核路径等待用户确认，直接路径继续 commit。"}
        finally:
            await client.close()

    @mcp.tool(name="structured_dataset_confirm", description=(
        "仅在用户确认完整逐列 Excel 预览后调用；同时确认固定文档草稿，自动提交正文并推进服务器表格发布。"
        "等待异步依赖时继续同一 task_id，不能改成本地 Office 保存。"), structured_output=False)
    async def confirm(task_id: UUID, result_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]):
        """一次确认驱动完整任务，不能通过确认 OCR 摘要替代表格候选确认。"""
        task_id = str(task_id)
        client = client_factory()
        try:
            # 草稿必须可恢复才能首次确认；正文不可由后续模型重新生成。
            current = await client.dataset_request("GET", f"/tasks/{task_id}")
            if current.get("status") == "WAITING_CONFIRMATION":
                draft_store().load(task_id, result_digest)
            await client.dataset_request("POST", f"/tasks/{task_id}/confirm", {"result_digest": result_digest})
            return await advance(client, task_id, result_digest)
        finally:
            await client.close()

    @mcp.tool(name="structured_dataset_commit", description=(
        "按 task_id 和原预览指纹继续完整 Excel 保存；自动执行目标注册、字段追加与服务器导出。"
        "无需重传长正文，等待转换/归档时保持未完成状态。"), structured_output=False)
    async def commit(task_id: UUID, result_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]):
        """恢复已确认或明确直接保存任务；不自行扩展字段、目标或授权。"""
        client = client_factory()
        try:
            return await advance(client, str(task_id), result_digest)
        finally:
            await client.close()

    @mcp.tool(name="structured_dataset_task_get", description="只读恢复本人的 Excel 任务状态；excel_published=false 不能报告完成。", structured_output=True)
    async def task_get(task_id: UUID) -> dict[str, Any]:
        """轮询不执行写入，后续推进只能使用 commit。"""
        client = client_factory()
        try:
            return await client.dataset_request("GET", f"/tasks/{task_id}")
        finally:
            await client.close()
