"""会话消息持久化仓库。

Service 通过仓库写入 message，避免 HTTP 路由或 AgentRuntimeService 直接操作 ORM。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    Conversation,
    Document,
    DocumentVersion,
    Message,
    TrashEntry,
    UploadArchiveRecord,
    UploadDuplicateReview,
    WorkingCopy,
    WorkingCopyRoot,
)
from app.modules.agent.repository import AgentRunRepository
from app.modules.agent.user_receipt import UserTaskReceipt, build_user_task_receipt
from app.modules.conversations.schemas import (
    ConversationAttachmentSummary,
    ConversationDetailResponse,
    ConversationHistoryMessage,
    ConversationMessage,
    ConversationPagination,
    MessageAttachment,
)
from app.modules.file_lifecycle.storage import FileLifecycleStorageService


HIDDEN_CONVERSATION_MESSAGE_ROLES = ("CLEARED", "SYSTEM_AUDIT")
LEGACY_INTERNAL_MESSAGE_PREFIXES = (
    "重复上传处理：",
    "已记录重复上传决策：",
)
LEGACY_INTERNAL_MESSAGE_SUFFIXES = (
    "的原件已归档，正在创建工作副本。",
)


@dataclass(frozen=True)
class AttachmentAvailabilityProjection:
    """聊天历史附件对应的当前工作副本可用状态。"""

    working_copy_id: str | None
    working_copy_status: str | None
    file_availability: str
    availability_message: str
    can_open: bool
    can_restore: bool


class ConversationRepository:
    """封装 conversation 和 message 的最小持久化操作。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def ensure_conversation(self, conversation_id: str, user_id: str) -> Conversation:
        """确保会话存在。

        当前阶段没有 workspace 和认证，允许按 URL 中的 conversation_id 自动创建占位会话。
        """

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is not None:
            if conversation.user_id != user_id:
                raise HTTPException(status_code=403, detail="Conversation belongs to another user")
            return conversation
        conversation = Conversation(id=conversation_id, user_id=user_id, title="")
        self.db.add(conversation)
        self.db.flush()
        return conversation

    def create_user_message(
        self,
        conversation_id: str,
        user_id: str,
        content: str,
        attachments: list[MessageAttachment],
        attachment_source: str = "uploaded",
    ) -> Message:
        """创建用户消息并保存附件引用 JSON。"""

        self.ensure_conversation(conversation_id=conversation_id, user_id=user_id)
        unique_attachments = _deduplicate_message_attachments(attachments)
        batch_id = str(uuid4()) if unique_attachments and attachment_source == "uploaded" else None
        message = Message(
            conversation_id=conversation_id,
            user_id=user_id,
            role="user",
            content=content,
            # attachments_json 是消息上下文的一部分，source/batch_id 用于区分真实上传批次和后端自动补齐。
            attachments_json=[
                {
                    **attachment.model_dump(),
                    "source": attachment_source,
                    **({"batch_id": batch_id} if batch_id else {}),
                }
                for attachment in unique_attachments
            ],
        )
        self.db.add(message)
        self.db.flush()
        return message

    def get_recent_attachment_references(
        self,
        *,
        conversation_id: str,
        user_id: str,
        limit: int = 10,
    ) -> list[MessageAttachment]:
        """读取当前会话最近消息中的附件引用，供“上面上传的文件”这类表达复用。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return []
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")

        messages = (
            self.db.query(Message)
            .filter(
                Message.conversation_id == conversation_id,
                Message.role.notin_(HIDDEN_CONVERSATION_MESSAGE_ROLES),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
            .all()
        )
        document_ids: list[str] = []
        seen: set[str] = set()
        for message in messages:
            for item in message.attachments_json:
                document_id = item.get("document_id") if isinstance(item, dict) else None
                if document_id and document_id not in seen:
                    seen.add(document_id)
                    document_ids.append(document_id)
        if not document_ids:
            return []

        owned_documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        owned_ids = {document.id for document in owned_documents}
        return [
            MessageAttachment(document_id=document_id)
            for document_id in document_ids
            if document_id in owned_ids
        ]

    def get_all_attachment_references(
        self,
        *,
        conversation_id: str,
        user_id: str,
    ) -> list[MessageAttachment]:
        """读取当前会话全部真实或上下文附件，用于“之前所有文件”这类表达。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return []
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")

        messages = (
            self.db.query(Message)
            .filter(
                Message.conversation_id == conversation_id,
                Message.role.notin_(HIDDEN_CONVERSATION_MESSAGE_ROLES),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
        document_ids = _collect_unique_document_ids(messages=messages)
        if not document_ids:
            return []
        return self._filter_owned_attachment_references(document_ids=document_ids, user_id=user_id)

    def get_filename_matched_attachment_references(
        self,
        *,
        conversation_id: str,
        user_id: str,
        content: str,
    ) -> list[MessageAttachment]:
        """按用户消息里的文件名片段匹配当前会话历史附件。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return []
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")

        messages = (
            self.db.query(Message)
            .filter(
                Message.conversation_id == conversation_id,
                Message.role.notin_(HIDDEN_CONVERSATION_MESSAGE_ROLES),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
        document_ids = _collect_unique_document_ids(messages=messages)
        if not document_ids:
            return []

        documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        documents_by_id = {document.id: document for document in documents}
        normalized_content = _normalize_filename_match_text(content)
        matched_ids: list[str] = []
        for document_id in document_ids:
            document = documents_by_id.get(document_id)
            if document is None:
                continue
            if _filename_matches_content(
                filename=document.original_filename,
                content=content,
                normalized_content=normalized_content,
            ):
                matched_ids.append(document_id)
        return [MessageAttachment(document_id=document_id) for document_id in matched_ids]

    def get_latest_attachment_batch_references(
        self,
        *,
        conversation_id: str,
        user_id: str,
        limit: int = 20,
    ) -> list[MessageAttachment]:
        """读取当前会话最近一条带附件消息中的整批附件，用于“刚刚上传的文件”。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return []
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")

        messages = (
            self.db.query(Message)
            .filter(
                Message.conversation_id == conversation_id,
                Message.role.notin_(HIDDEN_CONVERSATION_MESSAGE_ROLES),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
            .all()
        )
        document_ids: list[str] = []
        seen: set[str] = set()
        for message in messages:
            if not message.attachments_json:
                continue
            uploaded_items = _uploaded_attachment_items(message)
            if not uploaded_items:
                continue
            batch_id = uploaded_items[0].get("batch_id")
            if batch_id:
                uploaded_items = [item for item in uploaded_items if item.get("batch_id") == batch_id]
            for item in uploaded_items:
                document_id = item.get("document_id") if isinstance(item, dict) else None
                if document_id and document_id not in seen:
                    seen.add(document_id)
                    document_ids.append(document_id)
            break
        if not document_ids:
            return []
        return self._filter_owned_attachment_references(document_ids=document_ids, user_id=user_id)

    def get_latest_upload_lifecycle_attachment_references(
        self,
        *,
        conversation_id: str,
        user_id: str,
    ) -> list[MessageAttachment]:
        """从上传生命周期记录恢复最近一份会话文件。

        上传接口与发送消息是两个独立请求：当用户先上传、等待异步回执后只回复“改名”时，
        会话消息可能尚未保存附件。这里仅从同一用户、同一会话的最近上传审计关系恢复一份
        稳定 document_id，不能扫描用户其他文件，也不能在存在多目标时扩展为批量操作。
        """

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return []
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")
        row = (
            self.db.query(DocumentVersion.document_id)
            .join(
                UploadDuplicateReview,
                UploadDuplicateReview.upload_document_version_id == DocumentVersion.id,
            )
            .filter(
                UploadDuplicateReview.conversation_id == conversation_id,
                UploadDuplicateReview.user_id == user_id,
            )
            .order_by(UploadDuplicateReview.created_at.desc(), DocumentVersion.created_at.desc())
            .first()
        )
        if row is None:
            return []
        return self._filter_owned_attachment_references(
            document_ids=[str(row.document_id)],
            user_id=user_id,
        )

    def _filter_owned_attachment_references(
        self,
        *,
        document_ids: list[str],
        user_id: str,
    ) -> list[MessageAttachment]:
        """按当前用户过滤附件引用，防止越权文档进入 Agent 上下文。"""

        owned_documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        owned_ids = {document.id for document in owned_documents}
        return [
            MessageAttachment(document_id=document_id)
            for document_id in document_ids
            if document_id in owned_ids
        ]

    def get_conversation_for_user(self, conversation_id: str, user_id: str) -> Conversation:
        """读取当前用户自己的会话，不存在或越权时返回明确错误。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")
        return conversation

    def get_detail(
        self,
        conversation_id: str,
        user_id: str,
        limit: int = 10,
        before_message_id: str | None = None,
    ) -> ConversationDetailResponse:
        """组装会话详情，包含消息、附件摘要和每条消息对应的 AgentRun。"""

        conversation = self.get_conversation_for_user(conversation_id=conversation_id, user_id=user_id)
        messages, has_more = self._load_message_page(
            conversation_id=conversation_id,
            limit=limit,
            before_message_id=before_message_id,
        )
        # 兼容修复前已经写入普通 assistant 消息的重复上传确认卡：
        # 决策完成后卡片本身也不再属于用户对话内容，但审计行仍保留在数据库。
        messages = self._exclude_resolved_duplicate_notifications(messages=messages)
        document_map = self._load_document_map(messages=messages, user_id=user_id)
        agent_run_map = self._load_agent_run_map(messages=messages)
        agent_repository = AgentRunRepository(self.db)
        # 同一 AgentRun 只组装一次完整审计结果，再生成普通用户投影；完整结果不进入会话响应。
        agent_results = {
            message_id: agent_repository.to_result(run)
            for message_id, run in agent_run_map.items()
        }
        # 阶段五回答可能引用不在当前消息附件里的共享文件。历史回答恢复时也要重新查询这些
        # 文件的当前状态，不能继续沿用回答生成时写入 Tool 输出的 AVAILABLE。
        reference_document_ids = _evidence_reference_document_ids(agent_results.values())
        missing_reference_ids = reference_document_ids.difference(document_map)
        if missing_reference_ids:
            referenced_documents = (
                self.db.query(Document)
                .filter(Document.id.in_(missing_reference_ids))
                .all()
            )
            document_map.update(
                {document.id: document for document in referenced_documents}
            )
        availability_map = self._load_attachment_availability_map(document_map=document_map)
        task_receipts = {
            message_id: self._refresh_evidence_file_availability(
                receipt=build_user_task_receipt(agent_result),
                availability_map=availability_map,
            )
            for message_id, agent_result in agent_results.items()
        }
        return ConversationDetailResponse(
            id=conversation.id,
            user_id=conversation.user_id,
            title=conversation.title,
            status=conversation.status,
            messages=[
                ConversationHistoryMessage(
                    id=message.id,
                    conversation_id=message.conversation_id,
                    user_id=message.user_id,
                    role=message.role,
                    content=message.content,
                    attachments=[
                        self._attachment_to_summary(
                            item=item,
                            document_map=document_map,
                            availability_map=availability_map,
                        )
                        for item in _deduplicate_document_attachment_items(message.attachments_json)
                        if isinstance(item, dict) and item.get("document_id")
                    ],
                    metadata=[
                        dict(item)
                        for item in message.attachments_json
                        if isinstance(item, dict) and not item.get("document_id")
                    ],
                    task_result=(
                        task_receipts[message.id]
                        if message.id in task_receipts
                        else None
                    ),
                )
                for message in messages
            ],
            pagination=ConversationPagination(
                has_more=has_more,
                oldest_message_id=messages[0].id if messages else None,
                limit=limit,
            ),
        )

    @staticmethod
    def _refresh_evidence_file_availability(
        *,
        receipt: UserTaskReceipt,
        availability_map: dict[str, AttachmentAvailabilityProjection],
    ) -> UserTaskReceipt:
        """用当前工作副本状态刷新历史证据文件框，回收站文件不得继续打开正文。"""

        result = receipt.evidence_answer_result
        if not isinstance(result, dict):
            return receipt
        for file in result.get("files", []):
            if not isinstance(file, dict):
                continue
            document_id = str(file.get("document_id") or "")
            availability = availability_map.get(document_id)
            if availability is None:
                file.update(
                    {
                        "availability": "UNAVAILABLE",
                        "availability_message": "当前文件状态不可用",
                        "can_open": False,
                        "can_restore": False,
                    }
                )
                continue
            file.update(
                {
                    "availability": availability.file_availability,
                    "availability_message": availability.availability_message,
                    "can_open": availability.can_open,
                    "can_restore": availability.can_restore,
                }
            )
        return receipt

    def clear_visible_history(self, *, conversation_id: str, user_id: str) -> int:
        """逻辑清空当前用户会话中的可见消息，不删除文件或 Agent 审计。

        AgentRun 的 `message_id` 是非空审计外键，直接物理删除消息会破坏
        可追溯性。因此以 `CLEARED` 标记隐藏消息并清除其附件引用；读取、
        附件上下文和分页查询都排除该标记，用户看到的效果等同于新对话。
        """

        self.get_conversation_for_user(conversation_id=conversation_id, user_id=user_id)
        query = self.db.query(Message).filter(
            Message.conversation_id == conversation_id,
            Message.role.notin_(HIDDEN_CONVERSATION_MESSAGE_ROLES),
            *self._legacy_internal_message_filters(),
        )
        cleared_count = query.count()
        if cleared_count:
            query.update(
                {
                    Message.role: "CLEARED",
                    Message.content: "",
                    Message.attachments_json: [],
                },
                synchronize_session=False,
            )
            self.db.flush()
        return cleared_count

    def _load_message_page(
        self,
        *,
        conversation_id: str,
        limit: int,
        before_message_id: str | None,
    ) -> tuple[list[Message], bool]:
        """读取一页消息。

        数据库查询用倒序拿最近记录，返回前再恢复时间正序，前端可直接追加渲染。
        """

        # 已清空的消息仍保留为审计锚点，但不能再次出现在聊天历史中。
        query = self.db.query(Message).filter(
            Message.conversation_id == conversation_id,
            Message.role.notin_(HIDDEN_CONVERSATION_MESSAGE_ROLES),
            *self._legacy_internal_message_filters(),
        )
        if before_message_id:
            before_message = (
                self.db.query(Message)
                .filter(
                    Message.conversation_id == conversation_id,
                    Message.id == before_message_id,
                    Message.role.notin_(HIDDEN_CONVERSATION_MESSAGE_ROLES),
                    *self._legacy_internal_message_filters(),
                )
                .one_or_none()
            )
            if before_message is None:
                raise HTTPException(status_code=404, detail="Message not found")
            query = query.filter(
                or_(
                    Message.created_at < before_message.created_at,
                    (Message.created_at == before_message.created_at) & (Message.id < before_message.id),
                )
            )
        rows = (
            query.order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit + 1)
            .all()
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        page.reverse()
        return page, has_more

    def _exclude_resolved_duplicate_notifications(self, *, messages: list[Message]) -> list[Message]:
        """隐藏已经完成决策的历史重复上传确认卡。

        这里只调整普通用户会话投影，不删除 Message、UploadDuplicateReview 或其审计链。
        新数据会在决策事务中改为 ``SYSTEM_AUDIT``；该兼容分支用于立即隐藏旧数据库数据。
        """

        message_ids = [message.id for message in messages]
        if not message_ids:
            return messages
        resolved_notification_ids = {
            str(notification_message_id)
            for (notification_message_id,) in (
                self.db.query(UploadDuplicateReview.notification_message_id)
                .filter(
                    UploadDuplicateReview.notification_message_id.in_(message_ids),
                    UploadDuplicateReview.status != "WAITING_CONFIRMATION",
                )
                .all()
            )
            if notification_message_id
        }
        if not resolved_notification_ids:
            return messages
        return [
            message
            for message in messages
            if message.id not in resolved_notification_ids
        ]

    @staticmethod
    def _legacy_internal_message_filters() -> tuple:
        """返回历史内部决策文本过滤条件，避免升级后要求清库。

        这些固定前缀只由旧版生命周期代码生成；真实决策仍由 Review、AgentRun、
        ToolInvocation 和 ChangeSet 保存，普通会话接口不得继续暴露内部枚举。
        """

        return tuple(
            [
                *(Message.content.notlike(f"{prefix}%") for prefix in LEGACY_INTERNAL_MESSAGE_PREFIXES),
                *(Message.content.notlike(f"%{suffix}") for suffix in LEGACY_INTERNAL_MESSAGE_SUFFIXES),
                Message.content.notlike("工作副本操作完成：%"),
            ]
        )

    def _load_document_map(self, *, messages: list[Message], user_id: str) -> dict[str, Document]:
        """批量加载历史消息引用的文档，避免逐条消息查询。"""

        document_ids = {
            item.get("document_id")
            for message in messages
            for item in message.attachments_json
            if isinstance(item, dict) and item.get("document_id")
        }
        if not document_ids:
            return {}
        documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        return {document.id: document for document in documents}

    def _load_agent_run_map(self, *, messages: list[Message]) -> dict[str, AgentRun]:
        """按 message_id 取最新 AgentRun，供历史会话恢复助手回复。"""

        message_ids = [message.id for message in messages]
        if not message_ids:
            return {}
        runs = (
            self.db.query(AgentRun)
            .filter(AgentRun.message_id.in_(message_ids))
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
            .all()
        )
        return {run.message_id: run for run in runs}

    def _load_attachment_availability_map(
        self,
        *,
        document_map: dict[str, Document],
    ) -> dict[str, AttachmentAvailabilityProjection]:
        """批量解析历史 Document 到当前 WorkingCopy，并核对受控物理文件状态。

        会话附件必须保留历史引用，但是否可查看、是否已进入回收站必须以当前
        WorkingCopy 和 TrashEntry 为准，不能继续沿用上传时的 Document.status。
        """

        document_ids = list(document_map)
        if not document_ids:
            return {}
        direct_copies = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.document_id.in_(document_ids))
            .all()
        )
        copies_by_document: dict[str, list[WorkingCopy]] = {}
        for copy in direct_copies:
            copies_by_document.setdefault(copy.document_id, []).append(copy)

        upload_versions = (
            self.db.query(DocumentVersion)
            .filter(
                DocumentVersion.document_id.in_(document_ids),
                DocumentVersion.storage_tier == "UPLOAD",
            )
            .order_by(DocumentVersion.version_number.desc(), DocumentVersion.created_at.desc())
            .all()
        )
        latest_upload_by_document: dict[str, DocumentVersion] = {}
        for version in upload_versions:
            latest_upload_by_document.setdefault(version.document_id, version)
        version_ids = [version.id for version in latest_upload_by_document.values()]
        archives = (
            self.db.query(UploadArchiveRecord)
            .filter(UploadArchiveRecord.upload_document_version_id.in_(version_ids))
            .all()
            if version_ids
            else []
        )
        archive_by_version = {archive.upload_document_version_id: archive for archive in archives}
        managed_file_ids = {
            archive.managed_file_id
            for archive in archives
            if archive.managed_file_id
        }
        managed_copies = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.managed_file_id.in_(managed_file_ids))
            .all()
            if managed_file_ids
            else []
        )
        copies_by_managed_file: dict[str, list[WorkingCopy]] = {}
        for copy in managed_copies:
            copies_by_managed_file.setdefault(copy.managed_file_id, []).append(copy)

        selected_by_document: dict[str, WorkingCopy] = {}
        archive_by_document: dict[str, UploadArchiveRecord] = {}
        for document_id in document_ids:
            candidates = list(copies_by_document.get(document_id, []))
            upload_version = latest_upload_by_document.get(document_id)
            archive = archive_by_version.get(upload_version.id) if upload_version else None
            if archive is not None:
                archive_by_document[document_id] = archive
                if archive.managed_file_id:
                    candidates.extend(copies_by_managed_file.get(archive.managed_file_id, []))
            selected = _select_current_working_copy(candidates)
            if selected is not None:
                selected_by_document[document_id] = selected

        copy_ids = [copy.id for copy in selected_by_document.values()]
        root_ids = {copy.working_copy_root_id for copy in selected_by_document.values()}
        roots = (
            self.db.query(WorkingCopyRoot)
            .filter(WorkingCopyRoot.id.in_(root_ids))
            .all()
            if root_ids
            else []
        )
        root_map = {root.id: root for root in roots}
        trash_entries = (
            self.db.query(TrashEntry)
            .filter(
                TrashEntry.working_copy_id.in_(copy_ids),
                TrashEntry.status == "ACTIVE",
            )
            .all()
            if copy_ids
            else []
        )
        trash_map = {entry.working_copy_id: entry for entry in trash_entries}
        storage = FileLifecycleStorageService()
        return {
            document_id: _project_attachment_availability(
                document=document_map[document_id],
                working_copy=selected_by_document.get(document_id),
                working_root=(
                    root_map.get(selected_by_document[document_id].working_copy_root_id)
                    if document_id in selected_by_document
                    else None
                ),
                trash_entry=(
                    trash_map.get(selected_by_document[document_id].id)
                    if document_id in selected_by_document
                    else None
                ),
                archive=archive_by_document.get(document_id),
                storage=storage,
            )
            for document_id in document_ids
        }

    @staticmethod
    def _attachment_to_summary(
        *,
        item: dict,
        document_map: dict[str, Document],
        availability_map: dict[str, AttachmentAvailabilityProjection],
    ) -> ConversationAttachmentSummary:
        """把消息中的 document_id 引用扩展为前端可展示的附件摘要。"""

        document_id = item.get("document_id", "")
        document = document_map.get(document_id)
        if document is None:
            return ConversationAttachmentSummary(
                document_id=document_id,
                filename=document_id or "未知文件",
                content_type="application/octet-stream",
                size_bytes=0,
                sha256="",
                status="MISSING",
                ingest_status="FAILED",
                file_availability="MISSING",
                availability_message="文件记录已不存在",
            )
        availability = availability_map.get(document.id)
        return ConversationAttachmentSummary(
            document_id=document.id,
            filename=document.original_filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            status=document.status,
            ingest_status=document.ingest_status,
            working_copy_id=availability.working_copy_id if availability else None,
            working_copy_status=availability.working_copy_status if availability else None,
            file_availability=availability.file_availability if availability else "UNAVAILABLE",
            availability_message=availability.availability_message if availability else "工作副本状态不可用",
            can_open=availability.can_open if availability else False,
            can_restore=availability.can_restore if availability else False,
        )

    @staticmethod
    def to_schema(message: Message) -> ConversationMessage:
        """把 ORM Message 转为 API 响应 schema。"""

        return ConversationMessage(
            id=message.id,
            conversation_id=message.conversation_id,
            user_id=message.user_id,
            role=message.role,
            content=message.content,
            attachments=[
                MessageAttachment.model_validate(item)
                for item in _deduplicate_document_attachment_items(message.attachments_json)
            ],
        )


def _evidence_reference_document_ids(agent_results: Iterable[object]) -> set[str]:
    """从阶段五 Tool 审计投影中收集文件 ID，只用于刷新历史文件框当前状态。"""

    document_ids: set[str] = set()
    for result in agent_results:
        for invocation in getattr(result, "tool_invocations", []):
            if getattr(invocation, "tool_name", "") != "evidence-answer":
                continue
            output = getattr(invocation, "output_json", {})
            if not isinstance(output, dict):
                continue
            for reference in output.get("references", []):
                if not isinstance(reference, dict):
                    continue
                document_id = str(reference.get("document_id") or "")
                if document_id:
                    document_ids.add(document_id)
    return document_ids


def _deduplicate_message_attachments(
    attachments: list[MessageAttachment],
) -> list[MessageAttachment]:
    """按 document_id 保序去重消息附件，不合并同名但不同 ID 的文件。"""

    unique: list[MessageAttachment] = []
    seen: set[str] = set()
    for attachment in attachments:
        if attachment.document_id in seen:
            continue
        seen.add(attachment.document_id)
        unique.append(attachment)
    return unique


def _deduplicate_document_attachment_items(items: list[dict]) -> list[dict]:
    """清理历史消息里的重复文档引用，同时保留不同 document_id 的同名文件。"""

    unique: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("document_id") or "")
        if not document_id or document_id in seen:
            continue
        seen.add(document_id)
        unique.append(item)
    return unique


def _select_current_working_copy(candidates: list[WorkingCopy]) -> WorkingCopy | None:
    """从同一历史引用的候选中选择当前状态，优先活动、回收站再到处理中。"""

    if not candidates:
        return None
    unique = {candidate.id: candidate for candidate in candidates}
    status_priority = {
        "ACTIVE": 0,
        "TRASHED": 1,
        "IMPORTING": 2,
        "NEEDS_REVIEW": 3,
        "FAILED": 4,
    }
    return sorted(
        unique.values(),
        key=lambda copy: (
            status_priority.get(copy.status, 5),
            -(copy.updated_at.timestamp() if copy.updated_at else 0),
            copy.id,
        ),
    )[0]


def _project_attachment_availability(
    *,
    document: Document,
    working_copy: WorkingCopy | None,
    working_root: WorkingCopyRoot | None,
    trash_entry: TrashEntry | None,
    archive: UploadArchiveRecord | None,
    storage: FileLifecycleStorageService,
) -> AttachmentAvailabilityProjection:
    """把数据库状态与受控存储事实合并为普通用户可见的附件状态。"""

    if working_copy is None:
        processing_archive_statuses = {
            "DUPLICATE_CHECK_PENDING",
            "WAITING_DUPLICATE_DECISION",
            "PENDING",
            "ARCHIVING",
            "ARCHIVED",
        }
        if archive is not None and archive.status in processing_archive_statuses:
            return AttachmentAvailabilityProjection(
                working_copy_id=None,
                working_copy_status=None,
                file_availability="PROCESSING",
                availability_message="文件正在进入工作目录",
                can_open=False,
                can_restore=False,
            )
        if document.status == "MISSING":
            message = "文件记录已不存在"
            availability = "MISSING"
        else:
            message = "文件尚未形成可用工作副本"
            availability = "UNAVAILABLE"
        return AttachmentAvailabilityProjection(
            working_copy_id=None,
            working_copy_status=None,
            file_availability=availability,
            availability_message=message,
            can_open=False,
            can_restore=False,
        )

    if working_copy.status == "TRASHED":
        if trash_entry is None:
            return AttachmentAvailabilityProjection(
                working_copy_id=working_copy.id,
                working_copy_status=working_copy.status,
                file_availability="MISSING",
                availability_message="回收站记录异常，请联系管理员检查",
                can_open=False,
                can_restore=False,
            )
        try:
            trash_exists = storage.trash_path(trash_entry.trash_relative_path).is_file()
        except (OSError, RuntimeError, ValueError):
            trash_exists = False
        if not trash_exists:
            return AttachmentAvailabilityProjection(
                working_copy_id=working_copy.id,
                working_copy_status=working_copy.status,
                file_availability="MISSING",
                availability_message="回收站文件缺失，请联系管理员检查",
                can_open=False,
                can_restore=False,
            )
        return AttachmentAvailabilityProjection(
            working_copy_id=working_copy.id,
            working_copy_status=working_copy.status,
            file_availability="TRASHED",
            availability_message="已删除（在回收站，可恢复）",
            can_open=False,
            can_restore=True,
        )

    if working_copy.status == "ACTIVE":
        if working_root is None:
            exists = False
        else:
            try:
                exists = storage.working_copy_path(
                    f"{working_root.relative_storage_path}/{working_copy.relative_path}"
                ).is_file()
            except (OSError, RuntimeError, ValueError):
                exists = False
        if not exists:
            return AttachmentAvailabilityProjection(
                working_copy_id=working_copy.id,
                working_copy_status=working_copy.status,
                file_availability="MISSING",
                availability_message="文件状态异常：工作目录文件不存在",
                can_open=False,
                can_restore=False,
            )
        return AttachmentAvailabilityProjection(
            working_copy_id=working_copy.id,
            working_copy_status=working_copy.status,
            file_availability="AVAILABLE",
            availability_message="文件可用",
            can_open=True,
            can_restore=False,
        )

    return AttachmentAvailabilityProjection(
        working_copy_id=working_copy.id,
        working_copy_status=working_copy.status,
        file_availability="PROCESSING",
        availability_message="文件正在后台处理",
        can_open=False,
        can_restore=False,
    )


def _uploaded_attachment_items(message: Message) -> list[dict]:
    """读取消息中的真实上传附件项，跳过后端自动补齐的上下文附件。"""

    attachments = _deduplicate_document_attachment_items(message.attachments_json)
    if not attachments:
        return []
    uploaded_items = [item for item in attachments if item.get("source") == "uploaded"]
    if uploaded_items:
        return uploaded_items
    if any(item.get("source") == "inferred_context" for item in attachments):
        return []
    if _looks_like_context_reference_message(message.content):
        return []
    return attachments


def _collect_unique_document_ids(*, messages: list[Message]) -> list[str]:
    """按消息顺序收集去重后的 document_id，供全会话附件范围使用。"""

    document_ids: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for item in message.attachments_json:
            document_id = item.get("document_id") if isinstance(item, dict) else None
            if document_id and document_id not in seen:
                seen.add(document_id)
                document_ids.append(document_id)
    return document_ids


def _looks_like_context_reference_message(content: str) -> bool:
    """兼容历史数据：旧消息没有 source 时，用文本判断是否是上下文引用消息。"""

    reference_keywords = ["上面", "上文", "前面", "刚才", "刚刚", "刚上传", "之前", "已上传", "上传的"]
    file_task_keywords = [
        "文件", "附件", "文章", "读取", "总结", "讲解", "内容", "分析", "分类", "归类", "重新",
        "删除", "删掉", "回收站", "恢复",
    ]
    return any(keyword in content for keyword in reference_keywords) and any(
        keyword in content for keyword in file_task_keywords
    )


def _filename_matches_content(*, filename: str, content: str, normalized_content: str) -> bool:
    """判断文件名、主干或关键片段是否出现在用户消息中。"""

    normalized_filename = _normalize_filename_match_text(filename)
    stem = _normalize_filename_match_text(re.sub(r"\.[^.]{1,12}$", "", filename))
    candidates = [value for value in {normalized_filename, stem} if len(value) >= 4]
    if any(candidate in normalized_content for candidate in candidates):
        return True

    filename_years = set(re.findall(r"(?:19|20)\d{2}", filename))
    content_years = set(re.findall(r"(?:19|20)\d{2}", content))
    if filename_years and content_years and filename_years.isdisjoint(content_years):
        return False

    tokens = _filename_fuzzy_tokens(stem)
    matched_tokens = [token for token in tokens if token in normalized_content]
    required_matches = 2 if len(tokens) <= 4 else 3
    return len(matched_tokens) >= required_matches


def _normalize_filename_match_text(value: str) -> str:
    """归一化文件名匹配文本，降低空格、括号和分隔符带来的影响。"""

    lowered = value.lower()
    return re.sub(r"[\s\-_—–《》【】\[\]（）()，,。.:：;；/\\]+", "", lowered)


def _filename_fuzzy_tokens(stem: str) -> set[str]:
    """从文件名主干提取用于模糊匹配的中文、数字和英文片段。"""

    stop_tokens = {
        "文件",
        "材料",
        "资料",
        "表格",
        "汇总",
        "汇总表",
        "统计",
        "整理",
        "学院",
        "学校",
        "年度",
    }
    tokens: set[str] = set(re.findall(r"(?:19|20)\d{2}|[a-z]{2,}", stem))
    for chinese_part in re.findall(r"[\u4e00-\u9fff]{2,}", stem):
        for size in (4, 3, 2):
            for index in range(0, max(len(chinese_part) - size + 1, 0)):
                token = chinese_part[index : index + size]
                if token not in stop_tokens:
                    tokens.add(token)
    return tokens
