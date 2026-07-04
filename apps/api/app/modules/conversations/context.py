"""会话附件上下文解析服务。

本模块负责把用户自然语言里的“刚刚上传”“上面文件”“第二个文件”等表达，
解析成确定的 MessageAttachment 列表。Agent Runtime 只接收解析后的文件边界，
避免 LLM 或 Graph 节点自行猜测 document_id。
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from app.modules.conversations.repository import ConversationRepository
from app.modules.conversations.schemas import MessageAttachment


@dataclass(frozen=True)
class ResolvedAttachmentContext:
    """一次用户消息解析后的附件上下文。

    source 用于持久化到 messages.attachments_json，区分真实上传和后端自动补齐。
    scope 用于后续审计和扩展 Planner slot，目前不暴露给前端。
    """

    attachments: list[MessageAttachment]
    source: str
    scope: str


class ConversationAttachmentContextService:
    """解析会话内附件引用，统一维护文件上下文边界。"""

    def __init__(self, repository: ConversationRepository) -> None:
        """注入会话仓库，所有历史消息读取都通过仓库完成。"""

        self.repository = repository

    def resolve(
        self,
        *,
        conversation_id: str,
        user_id: str,
        content: str,
        explicit_attachments: list[MessageAttachment],
    ) -> ResolvedAttachmentContext:
        """解析本轮消息实际要交给 Agent 的附件列表。

        显式附件优先；无显式附件时才根据用户文本引用历史上下文。
        """

        if explicit_attachments:
            return ResolvedAttachmentContext(
                attachments=list(explicit_attachments),
                source="uploaded",
                scope="current_message",
            )
        if _has_file_task_intent(content):
            named_attachments = self.repository.get_filename_matched_attachment_references(
                conversation_id=conversation_id,
                user_id=user_id,
                content=content,
            )
            if named_attachments:
                return ResolvedAttachmentContext(
                    attachments=named_attachments,
                    source="inferred_context",
                    scope="filename_reference",
                )
        if not _should_infer_recent_attachments(content):
            return ResolvedAttachmentContext(attachments=[], source="uploaded", scope="none")

        if _should_use_latest_attachment_batch(content):
            recent_attachments = self.repository.get_latest_attachment_batch_references(
                conversation_id=conversation_id,
                user_id=user_id,
            )
            scope = "latest_upload_batch"
        elif _should_use_all_conversation_attachments(content):
            recent_attachments = self.repository.get_all_attachment_references(
                conversation_id=conversation_id,
                user_id=user_id,
            )
            scope = "all_conversation"
        else:
            recent_attachments = self.repository.get_recent_attachment_references(
                conversation_id=conversation_id,
                user_id=user_id,
            )
            scope = "all_recent_context"

        return ResolvedAttachmentContext(
            attachments=_select_referenced_attachments(
                content=content,
                recent_attachments=recent_attachments,
            ),
            source="inferred_context",
            scope=scope,
        )


def _should_infer_recent_attachments(content: str) -> bool:
    """判断用户是否在无附件消息中引用了当前会话上文文件。"""

    reference_keywords = ["上面", "上文", "前面", "刚才", "刚刚", "刚上传", "之前", "已上传", "上传的"]
    has_file_task = _has_file_task_intent(content)
    has_history_reference = any(keyword in content for keyword in reference_keywords)
    return has_file_task and (has_history_reference or _extract_file_ordinal(content) is not None)


def _has_file_task_intent(content: str) -> bool:
    """判断文本是否像文件任务，用于决定是否尝试解析历史附件引用。"""

    file_task_keywords = [
        "文件",
        "附件",
        "文章",
        "读取",
        "总结",
        "讲解",
        "内容",
        "分析",
        "分类",
        "归类",
        "重新",
        "汇总",
        "统计",
        "金额",
        "关键词",
        "关键字",
        "列",
        "表",
        "csv",
        "excel",
        "xlsx",
    ]
    lowered = content.lower()
    return any(keyword in content for keyword in file_task_keywords) or any(
        keyword in lowered for keyword in ["csv", "excel", "xlsx", "sheet"]
    )


def _select_referenced_attachments(
    *,
    content: str,
    recent_attachments: list[MessageAttachment],
) -> list[MessageAttachment]:
    """按用户自然语言选择上文附件；未指定序号时默认使用候选附件全集。"""

    ordinal = _extract_file_ordinal(content)
    if ordinal is None:
        return recent_attachments
    index = ordinal - 1
    if index < 0 or index >= len(recent_attachments):
        return []
    return [recent_attachments[index]]


def _should_use_latest_attachment_batch(content: str) -> bool:
    """判断用户是否指向最近一次上传批次，而不是历史全部文件。"""

    latest_batch_keywords = ["刚刚", "刚上传", "刚才上传", "刚才发", "刚发"]
    all_history_keywords = ["历史", "之前所有", "之前全部", "全部上传", "所有上传", "所有已上传"]
    return any(keyword in content for keyword in latest_batch_keywords) and not any(
        keyword in content for keyword in all_history_keywords
    )


def _should_use_all_conversation_attachments(content: str) -> bool:
    """判断用户是否明确要求当前会话历史全部附件。"""

    all_history_keywords = ["之前所有", "之前全部", "历史全部", "所有上传", "全部上传", "所有已上传"]
    return any(keyword in content for keyword in all_history_keywords)


def _extract_file_ordinal(content: str) -> int | None:
    """从“第二个文件 / 第2个文件 / 2号文件”中解析一基序号。"""

    digit_match = re.search(r"第\s*(\d+)\s*[个份]?\s*(?:文件|附件)", content)
    if digit_match:
        return int(digit_match.group(1))
    numbered_match = re.search(r"(\d+)\s*号\s*(?:文件|附件)", content)
    if numbered_match:
        return int(numbered_match.group(1))

    chinese_digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    chinese_match = re.search(r"第\s*([一二两三四五六七八九十])\s*[个份]?\s*(?:文件|附件)", content)
    if chinese_match:
        return chinese_digits[chinese_match.group(1)]
    return None
