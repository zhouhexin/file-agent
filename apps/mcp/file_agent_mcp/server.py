"""WorkBuddy 可启动的 stdio MCP 服务入口。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import Annotations, CallToolResult, ResourceLink, TextContent

from .attachment_transfer import AttachmentTransferService, WorkBuddyAttachmentInput
from .client import FileAgentIntegrationClient, LocalRootRegistry, WorkBuddyAttachmentRegistry
from .workbuddy_submission import WorkBuddySubmissionService
from .conversation_tools import (
    ExplicitRenameInput,
    MoveWorkingCopyInput,
    SetPrimaryCategoryInput,
    WorkBuddyConversationService,
)
from .transfer import BatchTransferService, TransferStateStore


mcp = FastMCP(
    "file-agent-local-import",
    instructions="仅处理用户明确提交的 WorkBuddy 附件或已经配置授权根并明确指定的本地文件。",
)


def _client() -> FileAgentIntegrationClient:
    """按当前进程配置构造客户端，避免把访问令牌放进 MCP 工具参数。"""

    return FileAgentIntegrationClient(
        base_url=os.getenv("FILE_AGENT_API_BASE_URL", "http://127.0.0.1:8000"),
        access_token=os.getenv("FILE_AGENT_ACCESS_TOKEN", ""),
        roots=LocalRootRegistry.from_environment(),
        web_base_url=os.getenv("FILE_AGENT_WEB_BASE_URL", "").strip() or None,
    )


def _local_download_dir() -> Path:
    """确定 WorkBuddy 下载缓存；优先使用显式目录，否则落在桥接状态目录内。"""

    configured = os.getenv("LOCAL_FILE_DOWNLOAD_DIR", "").strip()
    if configured:
        return Path(configured)
    bridge_state = os.getenv("FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR", "").strip()
    if bridge_state:
        return Path(bridge_state) / "downloads"
    return Path("~/.file-agent/downloads")


@mcp.tool(
    name="file_ingest",
    description="上传已登记批次中的一个本地文件，并自动启动后端整理任务。",
    structured_output=True,
)
async def file_ingest(
    batch_id: str,
    item_id: str,
    source_root_ref: str,
    source_relative_path: str,
) -> dict[str, Any]:
    """从授权逻辑根上传一个固定清单项，不接受绝对路径。"""

    client = _client()
    try:
        return await client.file_ingest(
            batch_id=batch_id,
            item_id=item_id,
            source_root_ref=source_root_ref,
            source_relative_path=source_relative_path,
        )
    finally:
        await client.close()


@mcp.tool(
    name="job_get",
    description="查询当前用户可见的 File Agent 后台任务状态。",
    structured_output=True,
)
async def job_get(job_id: str) -> dict[str, Any]:
    """读取后端持久化任务状态，供 WorkBuddy 轮询单文件处理进度。"""

    client = _client()
    try:
        return await client.job_get(job_id=job_id)
    finally:
        await client.close()


@mcp.tool(
    name="file_batch_ingest",
    description="枚举明确授权目录，固定批次清单并传输全部文件。",
    structured_output=True,
)
async def file_batch_ingest(
    source_root_ref: str,
    relative_directory: str,
    recursive: bool = True,
    user_request: str | None = None,
    placement_mode: Literal["BY_CATEGORY", "NEUTRAL"] = "BY_CATEGORY",
    rule_profile: Literal["content_based", "legacy_school_materials"] = "content_based",
) -> dict[str, Any]:
    """启动本地目录批次导入；目录与非默认规则都必须由用户明确指定。"""

    client = _client()
    try:
        return await BatchTransferService(
            client,
            TransferStateStore.from_environment(),
        ).ingest_directory(
            source_root_ref=source_root_ref,
            relative_directory=relative_directory,
            recursive=recursive,
            user_request=user_request,
            placement_mode=placement_mode,
            rule_profile=rule_profile,
        )
    finally:
        await client.close()


@mcp.tool(
    name="workbuddy_attachment_ingest",
    description="导入用户在本轮 WorkBuddy 消息中明确提交的附件，并自动分类、命名、落位和索引。",
    structured_output=True,
)
async def workbuddy_attachment_ingest(
    submission_id: str,
    attachments: list[WorkBuddyAttachmentInput],
    user_request: str | None = None,
    placement_mode: Literal["BY_CATEGORY", "NEUTRAL"] = "BY_CATEGORY",
    rule_profile: Literal["content_based", "legacy_school_materials"] = "content_based",
) -> dict[str, Any]:
    """接收 WorkBuddy 附件缓存引用；调用本身即构成本轮附件的提交授权。"""

    client = _client()
    try:
        return await AttachmentTransferService(
            client,
            WorkBuddyAttachmentRegistry.from_environment(),
        ).ingest(
            submission_id=submission_id,
            attachments=attachments,
            user_request=user_request,
            placement_mode=placement_mode,
            rule_profile=rule_profile,
        )
    finally:
        await client.close()


@mcp.tool(
    name="file_search",
    description=(
        "在 File Agent 已入库文件中执行只读搜索。工具返回的 content.text 已经是最终用户答复，"
        "调用方必须逐字原样输出，不得重新组织、总结、改写或增删列，即使用户要求文件类型等"
        "附加字段也不能修改该表格。固定保留“文件名｜依据｜操作”三列以及全部预览/下载链接；"
        "稳定 ID 仅供后续只读或受控文件工具使用，不能向用户展示。"
    ),
    structured_output=False,
)
async def file_search(conversation_ref: str, query: str) -> Any:
    """复用聊天搜索与澄清链路，并分离用户展示文本与后续 Tool 引用。"""

    client = _client()
    try:
        result = await WorkBuddyConversationService(client).search(
            conversation_ref=conversation_ref,
            query=query,
        )
        structured = {
            key: value
            for key, value in result.items()
            if key != "display_text"
        }
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=str(result.get("display_text") or ""),
                    annotations=Annotations(audience=["user"], priority=1.0),
                )
            ],
            structuredContent=structured,
        )
    finally:
        await client.close()


@mcp.tool(
    name="classification_overview",
    description=(
        "只读查看 File Agent 当前分类总数、具体业务分类数、其他分类数和一级分类入口。"
        "content.text 必须逐字原样展示；完整分类树应通过返回的分类页面链接打开。"
    ),
    structured_output=False,
)
async def classification_overview() -> Any:
    """返回分类总览及原 Web 分类页链接，不执行重新分类或文件移动。"""

    client = _client()
    try:
        result = await WorkBuddyConversationService(client).classification_overview()
        structured = {key: value for key, value in result.items() if key != "display_text"}
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=str(result.get("display_text") or ""),
                    annotations=Annotations(audience=["user"], priority=1.0),
                )
            ],
            structuredContent=structured,
        )
    finally:
        await client.close()


@mcp.tool(
    name="classification_files",
    description=(
        "只读分页查看某个稳定 category_id 及其全部子分类下的已发布文件。"
        "category_id 可从 classification_overview 的结构化结果取得；content.text 必须逐字原样展示，"
        "并保留分类页面、预览和下载链接。"
    ),
    structured_output=False,
)
async def classification_files(
    category_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> Any:
    """返回分类文件分页；缺省 category_id 时查看全部活动文件。"""

    client = _client()
    try:
        result = await WorkBuddyConversationService(client).classification_files(
            category_id=category_id,
            page=page,
            page_size=page_size,
        )
        structured = {key: value for key, value in result.items() if key != "display_text"}
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=str(result.get("display_text") or ""),
                    annotations=Annotations(audience=["user"], priority=1.0),
                )
            ],
            structuredContent=structured,
        )
    finally:
        await client.close()


@mcp.tool(
    name="file_read",
    description="读取 file_search 已确定的文件，可执行读取、总结或讲解等固定只读任务。",
    structured_output=True,
)
async def file_read(
    conversation_ref: str,
    document_ids: list[str],
    read_mode: Literal["READ", "SUMMARY", "EXPLAIN"] = "SUMMARY",
) -> dict[str, Any]:
    """只按稳定 document_id 读取，不能由 WorkBuddy 提交服务器路径或正文。"""

    client = _client()
    try:
        return await WorkBuddyConversationService(client).read(
            conversation_ref=conversation_ref,
            document_ids=document_ids,
            read_mode=read_mode,
        )
    finally:
        await client.close()


@mcp.tool(
    name="evidence_answer",
    description="基于已入库文件完整原文回答问题，并返回 File Agent 校验过的引用。",
    structured_output=True,
)
async def evidence_answer(
    conversation_ref: str,
    question: str,
    document_ids: list[str] | None = None,
) -> dict[str, Any]:
    """证据和答案都由 File Agent 生成校验，WorkBuddy 不能传入自造引用。"""

    client = _client()
    try:
        return await WorkBuddyConversationService(client).answer(
            conversation_ref=conversation_ref,
            question=question,
            document_ids=document_ids or [],
        )
    finally:
        await client.close()


@mcp.tool(
    name="file_rename",
    description="按用户明确给出的原文件名和目标文件名重命名已确定文件。",
    structured_output=True,
)
async def file_rename(
    conversation_ref: str,
    renames: list[ExplicitRenameInput],
) -> dict[str, Any]:
    """复用后端受控重命名链路；MCP 不直接改文件，也不绕过 OperationPlan 审计。"""

    client = _client()
    try:
        return await WorkBuddyConversationService(client).rename(
            conversation_ref=conversation_ref,
            renames=renames,
        )
    finally:
        await client.close()


@mcp.tool(
    name="file_download",
    description=(
        "下载 file_search 已确定且已生成工作副本的文件。必须原样使用搜索结果"
        " tool_context 中的 working_copy_id；文件保存到 MCP 专用本机缓存。"
    ),
    structured_output=False,
)
async def file_download(working_copy_id: str) -> Any:
    """通过当前用户令牌下载工作副本，并以 MCP ResourceLink 交给 WorkBuddy。"""

    client = _client()
    try:
        result = await client.download_working_copy(
            working_copy_id=working_copy_id,
            output_dir=_local_download_dir(),
        )
        filename = str(result["filename"])
        return CallToolResult(
            content=[
                TextContent(type="text", text=f"已准备下载：{filename}"),
                ResourceLink(
                    type="resource_link",
                    name=filename,
                    title=f"下载 {filename}",
                    uri=str(result["resource_uri"]),
                    mimeType=str(result["content_type"]),
                    size=int(result["size_bytes"]),
                    description="File Agent 已鉴权下载到本机专用缓存的文件。",
                ),
            ],
            structuredContent={
                "filename": filename,
                "downloaded": True,
                "size_bytes": int(result["size_bytes"]),
            },
        )
    finally:
        await client.close()


@mcp.tool(
    name="workbuddy_submission_ingest",
    description=(
        "导入本轮 WorkBuddy 附件桥接插件已捕获的可信附件提交单，并自动解析、"
        "查重、归档和分类。文件助手是面向用户的中文称呼。仅在当前消息包含 "
        "FILE_AGENT_HOST_SUBMISSION 且用户说‘请用文件助手’处理附件，或明确"
        "要求导入、归档、整理或分类附件时调用；不得要求或填写本地路径、附件 ID。"
        "调用后必须用 batch_get 检查逐文件状态；若为 WAITING_EXTERNAL_EXTRACTION，"
        "应立即进入 extraction_claim、宿主 OCR、extraction_submit 流程，不能只重复查询状态。"
    ),
    structured_output=True,
)
async def workbuddy_submission_ingest(
    submission_ref: str,
    user_request: str | None = None,
    placement_mode: Literal["BY_CATEGORY", "NEUTRAL"] = "BY_CATEGORY",
    rule_profile: Literal["content_based", "legacy_school_materials"] = "content_based",
) -> dict[str, Any]:
    """按插件提交单导入当前会话附件；用户任务由 Hook 冻结，模型不能伪造附件范围。"""

    client = _client()
    try:
        registry = WorkBuddyAttachmentRegistry.from_environment()
        transfer = AttachmentTransferService(client, registry)
        return await WorkBuddySubmissionService.from_environment(
            transfer=transfer,
            registry=registry,
        ).ingest(
            submission_ref=submission_ref,
            user_request=user_request,
            placement_mode=placement_mode,
            rule_profile=rule_profile,
        )
    finally:
        await client.close()


@mcp.tool(
    name="file_set_primary_category",
    description=(
        "把已确定文件的主分类设为指定 taxonomy 节点，并按该主类异步落位。"
        "必须提供 file_search 返回的 working_copy_id、当前版本、revision 和 taxonomy 目标；"
        "不接受本机路径，不需要也不能提供二次确认字段。"
    ),
    structured_output=True,
)
async def file_set_primary_category(
    command: SetPrimaryCategoryInput,
    request_id: str,
) -> dict[str, Any]:
    """转交冻结的 SET_PRIMARY 命令；后端负责授权快照、冲突检查和异步执行。"""

    client = _client()
    try:
        return await WorkBuddyConversationService(client).submit_classification_placement(
            command=command,
            request_id=request_id,
        )
    finally:
        await client.close()


@mcp.tool(
    name="file_move",
    description=(
        "将已确定文件移动到指定分类或预先配置的受控目录。"
        "必须提供 file_search 返回的 working_copy_id、当前版本、revision 和稳定提交 ID；"
        "不接受宿主本机路径，也不能移动原件。"
    ),
    structured_output=True,
)
async def file_move(
    command: MoveWorkingCopyInput,
    request_id: str,
) -> dict[str, Any]:
    """转交冻结的 MOVE 命令；仅后端可解析受控目录并执行工作副本移动。"""

    client = _client()
    try:
        return await WorkBuddyConversationService(client).submit_classification_placement(
            command=command,
            request_id=request_id,
        )
    finally:
        await client.close()


@mcp.tool(
    name="file_placement_status",
    description="查询当前用户先前提交的分类落位操作进度和最终结果；此工具只读，不移动文件。",
    structured_output=True,
)
async def file_placement_status(operation_id: str) -> dict[str, Any]:
    """读取后端持久化的落位状态；其他用户的操作按不存在处理。"""

    client = _client()
    try:
        return await WorkBuddyConversationService(client).get_classification_placement_status(
            operation_id=operation_id,
        )
    finally:
        await client.close()


@mcp.tool(
    name="file_search_clarification_resolve",
    description="提交 File Agent 搜索结果中的结构化澄清选项并继续原任务。",
    structured_output=True,
)
async def file_search_clarification_resolve(
    clarification_id: str,
    option_id: str | None = None,
    option_ids: list[str] | None = None,
    custom_phrase: str | None = None,
) -> dict[str, Any]:
    """只接受后端签发选项；修订或归属不正确时由后端拒绝。"""

    client = _client()
    try:
        return await client.resolve_file_search_clarification(
            clarification_id=clarification_id,
            option_id=option_id,
            option_ids=option_ids or [],
            custom_phrase=custom_phrase,
        )
    finally:
        await client.close()


@mcp.tool(
    name="operation_plan_get",
    description="恢复当前用户文件操作计划的真实状态和 before/after。",
    structured_output=True,
)
async def operation_plan_get(plan_id: str) -> dict[str, Any]:
    """从后端持久化事实恢复计划，不能依赖旧聊天气泡判断是否已执行。"""

    client = _client()
    try:
        return await client.operation_plan_get(plan_id=plan_id)
    finally:
        await client.close()


@mcp.tool(
    name="operation_plan_confirm",
    description="在用户明确确认后执行 File Agent 已生成且仍有效的文件操作计划。",
    structured_output=True,
)
async def operation_plan_confirm(plan_id: str, confirmation: str) -> dict[str, Any]:
    """仅转交确认文字；目标、范围、修订和白名单执行器均由后端再次校验。"""

    client = _client()
    try:
        return await client.operation_plan_confirm(
            plan_id=plan_id,
            confirmation=confirmation,
        )
    finally:
        await client.close()


@mcp.tool(
    name="batch_resume",
    description="按后端事实恢复原批次尚未完成的本地文件传输。",
    structured_output=True,
)
async def batch_resume(batch_id: str) -> dict[str, Any]:
    """恢复已保存清单，不扩大目录范围或重置业务失败项。"""

    client = _client()
    try:
        return await BatchTransferService(
            client,
            TransferStateStore.from_environment(),
        ).resume(batch_id=batch_id)
    finally:
        await client.close()


@mcp.tool(
    name="batch_get",
    description=(
        "恢复批次、逐文件状态和所有尚待选择的重复确认。若条目状态为 "
        "WAITING_EXTERNAL_EXTRACTION，必须从条目 result.external_extraction_task_id 取得任务 ID，"
        "随后调用 extraction_claim、WorkBuddy 可用 OCR 和 extraction_submit；重复调用本工具不会完成 OCR。"
        "已发布的成功条目同时返回 primary_category 和 classification_outcome，"
        "应逐文件展示主分类路径与分类结果状态；本回执不提供分类证据或关键词。"
    ),
    structured_output=True,
)
async def batch_get(batch_id: str) -> dict[str, Any]:
    """以后端持久化事实重建展示状态，不依赖本地聊天记录。"""

    client = _client()
    try:
        return await client.batch_snapshot(batch_id=batch_id)
    finally:
        await client.close()


@mcp.tool(
    name="duplicate_review_get",
    description="恢复一个导入条目的最新重复候选、修订和允许决定。",
    structured_output=True,
)
async def duplicate_review_get(item_id: str) -> dict[str, Any]:
    """读取后端候选事实，不能从旧聊天气泡恢复选择。"""

    client = _client()
    try:
        return await client.duplicate_review_get(item_id=item_id)
    finally:
        await client.close()


@mcp.tool(
    name="duplicate_comparison_get",
    description="读取一个重复候选的预览/分别下载浏览器链接和两侧安全元数据；不会读取正文或提交决定。",
    structured_output=True,
)
async def duplicate_comparison_get(
    item_id: str, review_id: str, review_revision: int, candidate_id: str, group_revision: int | None = None,
) -> dict[str, Any]:
    """只转发当前候选稳定身份，模型不能传路径、URL 或正文。"""

    client = _client()
    try:
        return await client.duplicate_comparison_get(
            item_id=item_id, review_id=review_id, review_revision=review_revision,
            candidate_id=candidate_id, group_revision=group_revision,
        )
    finally:
        await client.close()


@mcp.tool(
    name="duplicate_decide",
    description="提交用户明确选择的重复候选和处理决定。",
    structured_output=True,
)
async def duplicate_decide(
    item_id: str,
    review_id: str,
    review_revision: int,
    decision: str,
    request_id: str,
    idempotency_key: str,
    candidate_id: str | None = None,
    group_revision: int | None = None,
    group_member_item_ids: list[str] | None = None,
) -> dict[str, Any]:
    """把结构化选择原样交给后端校验，不从自然语言推断内部候选 ID。"""

    client = _client()
    try:
        return await client.duplicate_decide(
            item_id=item_id,
            review_id=review_id,
            review_revision=review_revision,
            group_revision=group_revision,
            group_member_item_ids=group_member_item_ids or [],
            decision=decision,
            candidate_id=candidate_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
    finally:
        await client.close()


@mcp.tool(
    name="ingest_retry",
    description="显式重试一个后端已标记失败的导入条目。",
    structured_output=True,
)
async def ingest_retry(
    item_id: str,
    request_id: str,
    idempotency_key: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """只重试指定条目，不重置批次或其他文件。"""

    client = _client()
    try:
        return await client.ingest_item_action(
            item_id=item_id,
            action="retry",
            request_id=request_id,
            idempotency_key=idempotency_key,
            reason=reason,
        )
    finally:
        await client.close()


@mcp.tool(
    name="ingest_cancel",
    description="取消一个尚未完成的导入条目并保留审计。",
    structured_output=True,
)
async def ingest_cancel(
    item_id: str,
    request_id: str,
    idempotency_key: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """取消本次条目，不删除或修改已经选择复用的现有文件。"""

    client = _client()
    try:
        return await client.ingest_item_action(
            item_id=item_id,
            action="cancel",
            request_id=request_id,
            idempotency_key=idempotency_key,
            reason=reason,
        )
    finally:
        await client.close()


@mcp.tool(
    name="extraction_claim",
    description=(
        "领取外部 OCR 任务并下载真实待识别页面到本地受控暂存目录。领取后必须逐页调用 "
        "WorkBuddy 已连接的识图工具，并把真实文本和可用坐标交给 extraction_submit；"
        "不得只查询 batch_get，也不得让 File Agent 后端自行 OCR。"
    ),
    structured_output=True,
)
async def extraction_claim(task_id: str, worker_id: str) -> dict[str, Any]:
    """返回租约、固定页集合和 WorkBuddy 可交给 OCR 工具的本地页面路径。"""

    client = _client()
    try:
        output_dir = os.getenv("LOCAL_EXTRACTION_PAGE_DIR", "~/.file-agent/extraction-pages")
        return await client.extraction_claim(
            task_id=task_id,
            worker_id=worker_id,
            output_dir=Path(output_dir),
        )
    finally:
        await client.close()


@mcp.tool(
    name="extraction_renew",
    description="仅在正在执行宿主 OCR 且租约即将到期时续期当前任务；不得用续租代替 OCR 或提交结果。",
    structured_output=True,
)
async def extraction_renew(task_id: str, worker_id: str, lease_token: str) -> dict[str, Any]:
    """长时间 OCR 期间续租，过期租约不能复活。"""

    client = _client()
    try:
        return await client.extraction_renew(
            task_id=task_id,
            worker_id=worker_id,
            lease_token=lease_token,
        )
    finally:
        await client.close()


@mcp.tool(
    name="extraction_submit",
    description=(
        "一次性提交 extraction_claim 固定页集合的真实 OCR 结果，并让 File Agent 继续查重、归档和分类。"
        "页码、source_sha256、source_version_id 必须沿用领取结果；Provider 未返回的置信度、版本和请求 ID "
        "必须为 null，不能推测或伪造。提交后按返回的 filesystem_job_id 查询实际处理任务。"
    ),
    structured_output=True,
)
async def extraction_submit(
    task_id: str,
    worker_id: str,
    lease_token: str,
    submission_key: str,
    source_sha256: str,
    source_version_id: str,
    pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """提交 Provider 原始可用字段；缺失置信度或坐标时不得伪造。"""

    client = _client()
    try:
        return await client.extraction_submit(
            task_id=task_id,
            payload={
                "worker_id": worker_id,
                "lease_token": lease_token,
                "submission_key": submission_key,
                "source_sha256": source_sha256,
                "source_version_id": source_version_id,
                "pages": pages,
            },
        )
    finally:
        await client.close()


def main() -> None:
    """通过 stdio 启动 MCP，避免额外暴露本地 HTTP 监听端口。"""

    mcp.run(transport="stdio")
