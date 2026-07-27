"""会话附件上下文解析服务。

本模块负责把用户自然语言里的“刚刚上传”“上面文件”“第二个文件”等表达，
解析成确定的 MessageAttachment 列表。Agent Runtime 只接收解析后的文件边界，
避免 LLM 或 Graph 节点自行猜测 document_id。
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from app.modules.file_lifecycle.conversation_intents import (
    has_contextual_file_removal_reference,
    has_trash_working_copy_intent,
    is_target_only_rename_request,
)
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
                # 重复上传确认选择“使用已有文件”后，多个前端卡片可能暂时指向同一
                # document_id。这里只按稳定 ID 去重；同名但 ID 不同的文件必须保留，
                # 后续仍由用户明确选择，不能按文件名或内容哈希擅自合并。
                attachments=_deduplicate_attachments(explicit_attachments),
                source="uploaded",
                scope="current_message",
            )
        if is_target_only_rename_request(content):
            # 目标文件名不是源文件引用。这里必须保持空范围，让 Planner 要求用户
            # 重新附加文件或同时写出源文件名，不能在会话历史中碰巧命中同名文件。
            return ResolvedAttachmentContext(attachments=[], source="uploaded", scope="none")
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
        if _is_single_file_rename_confirmation(content):
            # “改名”可以确认上一轮单文件建议，但多个最近附件时绝不能猜测要改哪一份。
            # 用户必须写出文件名或重新附加文件，避免把一句短确认扩展成批量物理操作。
            latest_attachments = self.repository.get_latest_attachment_batch_references(
                conversation_id=conversation_id,
                user_id=user_id,
            )
            if not latest_attachments:
                # 上传接口在消息创建前就可能先登记生命周期；此时从同会话上传审计恢复
                # 最近一份文件，仍保持“一次短确认只能指向一份文件”的安全边界。
                latest_attachments = self.repository.get_latest_upload_lifecycle_attachment_references(
                    conversation_id=conversation_id,
                    user_id=user_id,
                )
            if len(latest_attachments) == 1:
                return ResolvedAttachmentContext(
                    attachments=latest_attachments,
                    source="inferred_context",
                    scope="single_latest_rename_confirmation",
                )
        if (
            has_trash_working_copy_intent(content)
            and not has_contextual_file_removal_reference(content)
        ):
            # “删除整个工作簿文件”等表达没有“这个/刚才”，但当前会话只有一个不同文件时，
            # 后端可以确定唯一对象；存在多个文件则保持空范围并要求用户选择，绝不批量猜测。
            recent_attachments = self.repository.get_recent_attachment_references(
                conversation_id=conversation_id,
                user_id=user_id,
            )
            if len(recent_attachments) == 1:
                return ResolvedAttachmentContext(
                    attachments=recent_attachments,
                    source="inferred_context",
                    scope="single_conversation_file_removal",
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
            attachments=_deduplicate_attachments(
                _select_referenced_attachments(
                    content=content,
                    recent_attachments=recent_attachments,
                )
            ),
            source="inferred_context",
            scope=scope,
        )


def _deduplicate_attachments(attachments: list[MessageAttachment]) -> list[MessageAttachment]:
    """按 document_id 保序去重，不合并同名或同内容的不同文档。"""

    unique: list[MessageAttachment] = []
    seen: set[str] = set()
    for attachment in attachments:
        if attachment.document_id in seen:
            continue
        seen.add(attachment.document_id)
        unique.append(attachment)
    return unique


def _should_infer_recent_attachments(content: str) -> bool:
    """判断用户是否在无附件消息中引用了当前会话上文文件。"""

    reference_keywords = [
        "上面",
        "上文",
        "前面",
        "刚才",
        "刚刚",
        "刚上传",
        "之前",
        "已上传",
        "上传的",
        "上一个",
        "上个",
    ]
    has_file_task = _has_file_task_intent(content)
    has_history_reference = any(keyword in content for keyword in reference_keywords)
    has_removal_reference = has_contextual_file_removal_reference(content)
    return has_file_task and (
        has_history_reference
        or has_removal_reference
        or _extract_file_ordinal(content) is not None
    )


def _has_file_task_intent(content: str) -> bool:
    """判断文本是否像文件任务，用于决定是否尝试解析历史附件引用。"""

    if has_trash_working_copy_intent(content):
        return True
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
        "重命名",
        "改名",
        "命名",
        "删除",
        "删掉",
        "回收站",
        "恢复",
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


def _is_single_file_rename_confirmation(content: str) -> bool:
    """识别不带文件名的单文件改名确认，且只允许绑定最近一批唯一附件。"""

    normalized = re.sub(r"\s+", "", content).strip("。！!")
    return normalized in {"改名", "重命名", "确认改名", "按建议改名", "需要改名"}


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

    all_history_keywords = [
        "之前所有",
        "之前全部",
        "历史全部",
        "所有上传",
        "全部上传",
        "所有已上传",
        "上传的所有",
        "所有上传的",
        "全部上传的",
        "已上传的所有",
    ]
    return any(keyword in content for keyword in all_history_keywords)


def _extract_file_ordinal(content: str) -> int | None:
    """从“第二个文件 / 第2个文件 / 2号文件”中解析一基序号。"""

    if re.search(r"(?:上一个|上个)\s*(?:文件|附件)", content):
        return 1

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
