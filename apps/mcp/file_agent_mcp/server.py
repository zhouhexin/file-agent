"""WorkBuddy 可启动的 stdio MCP 服务入口。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .attachment_transfer import AttachmentTransferService, WorkBuddyAttachmentInput
from .client import FileAgentIntegrationClient, LocalRootRegistry, WorkBuddyAttachmentRegistry
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
    )


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
    description="在 File Agent 已入库文件中执行聊天搜索，并返回可继续读取的稳定文件 ID。",
    structured_output=True,
)
async def file_search(conversation_ref: str, query: str) -> dict[str, Any]:
    """复用聊天搜索与澄清链路，不让 WorkBuddy 自行判断文件相关性。"""

    client = _client()
    try:
        return await WorkBuddyConversationService(client).search(
            conversation_ref=conversation_ref,
            query=query,
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
    description="恢复批次、逐文件状态和所有尚待选择的重复确认。",
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
    description="领取外部 OCR 任务并下载真实待识别页面到本地受控暂存目录。",
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
    description="续期当前外部 OCR 租约。",
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
    description="提交固定页集合的 OCR 结果并让 File Agent 继续查重和整理。",
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
