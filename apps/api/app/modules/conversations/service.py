"""会话消息服务。

该服务负责把 HTTP 消息持久化为 message，并启动对应的 AgentRun。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import AgentRun, FileRenameReviewItem, Message
from app.modules.agent.repository import AgentRunRepository
from app.modules.agent.planner import FileScopeClarificationPlanner
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.state import AgentRunResult
from app.modules.conversations.context import ConversationAttachmentContextService
from app.modules.conversations.repository import ConversationRepository
from app.modules.conversations.schemas import (
    ClearConversationResponse,
    ConversationDetailResponse,
    ConversationMessage,
    SendMessageRequest,
)
from app.modules.file_lifecycle.shared_access import (
    CanonicalWorkingFileError,
    CanonicalWorkingFileResolver,
)
from app.modules.files.repository import FileRepository
from app.modules.retrieval.clarification_planner import (
    FileSearchClarificationPlanner,
)
from app.modules.retrieval.clarification_service import (
    FileSearchClarificationError,
    FileSearchClarificationService,
    ResolvedSearchSelection,
)


@dataclass(frozen=True)
class ConversationExecutionResult:
    """消息服务内部执行结果，供路由投影和服务层测试使用。

    `agent_run` 只在后端进程内流转，HTTP 路由必须显式转换成不含内部载荷的
    `SendMessageResponse`。
    """

    message: ConversationMessage
    agent_run: AgentRunResult


class ConversationMessageService:
    """负责创建用户 message，并启动对应的 LangGraph AgentRun。"""

    def __init__(self, db: Session, agent_service: AgentRuntimeService | None = None) -> None:
        """注入数据库会话和 AgentRuntimeService。"""

        self.db = db
        self.agent_service = agent_service or AgentRuntimeService()
        self.repository = ConversationRepository(db)

    def send_user_message(
        self,
        conversation_id: str,
        request: SendMessageRequest,
        user_id: str = "user-memory",
    ) -> ConversationExecutionResult:
        """创建持久化用户消息，并把消息交给 Agent Runtime 执行。

        HTTP 调用必须传入认证用户 ID；默认值只保留给不经过 HTTP 的最小服务测试。
        """

        selection = None
        if not request.attachments:
            selection = FileSearchClarificationService(self.db).resolve_from_text(
                conversation_id=conversation_id,
                user_id=user_id,
                message=request.content,
            )
        return self._execute_message(
            conversation_id=conversation_id,
            request=request,
            user_id=user_id,
            clarification_selection=selection,
        )

    def resolve_file_search_clarification(
        self,
        *,
        clarification_id: str,
        option_id: str | None,
        option_ids: list[str] | None,
        custom_phrase: str | None,
        user_id: str,
    ) -> ConversationExecutionResult:
        """根据单选或文件多选结果创建可见消息，并续跑原始文件任务。"""

        selection = FileSearchClarificationService(self.db).resolve(
            clarification_id=clarification_id,
            user_id=user_id,
            option_id=option_id,
            option_ids=option_ids,
            custom_phrase=custom_phrase,
        )
        return self._execute_message(
            conversation_id=selection.conversation_id,
            request=SendMessageRequest(
                content=selection.display_content,
                attachments=[],
            ),
            user_id=user_id,
            clarification_selection=selection,
        )

    def _execute_message(
        self,
        *,
        conversation_id: str,
        request: SendMessageRequest,
        user_id: str,
        clarification_selection: ResolvedSearchSelection | None = None,
    ) -> ConversationExecutionResult:
        """执行普通消息或已校验检索选择，共用同一 AgentRun 审计链路。"""

        if clarification_selection is not None:
            existing = self._existing_clarification_execution(
                clarification_selection
            )
            if existing is not None:
                return existing

        attachment_context = ConversationAttachmentContextService(self.repository).resolve(
            conversation_id=conversation_id,
            user_id=user_id,
            content=request.content,
            explicit_attachments=list(request.attachments),
        )
        attachments = attachment_context.attachments

        message = self.repository.create_user_message(
            conversation_id=conversation_id,
            user_id=user_id,
            content=request.content,
            attachments=attachments,
            attachment_source=attachment_context.source,
        )
        FileRepository(self.db).lock_documents_for_message(
            document_ids=[attachment.document_id for attachment in attachments],
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message.id,
        )
        runtime_attachments = self._canonicalize_agent_attachments(
            attachments=[dict(item) for item in (message.attachments_json or [])],
        )
        agent_message = self._normalize_unique_filename_conflict_reply(
            conversation_id=conversation_id,
            user_id=user_id,
            content=request.content,
            has_explicit_attachments=bool(request.attachments),
        )
        agent_run = self.agent_service.run_message(
            conversation_id=conversation_id,
            user_id=user_id,
            message_id=message.id,
            message=agent_message,
            attachments=[
                {
                    **attachment,
                    "context_scope": attachment_context.scope,
                }
                for attachment in runtime_attachments
            ],
            planner=(
                FileSearchClarificationPlanner(clarification_selection)
                if clarification_selection is not None
                else (
                    FileScopeClarificationPlanner(
                        question=attachment_context.clarification_question
                    )
                    if attachment_context.clarification_question
                    else None
                )
            ),
            db=self.db,
        )
        if clarification_selection is not None:
            FileSearchClarificationService(self.db).mark_execution_result(
                clarification_id=clarification_selection.clarification_id,
                user_id=user_id,
                message_id=message.id,
                agent_run_id=agent_run.agent_run_id,
            )
        self.db.commit()
        self.db.refresh(message)
        return ConversationExecutionResult(
            message=self.repository.to_schema(message),
            agent_run=agent_run,
        )

    def _canonicalize_agent_attachments(
        self,
        *,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """把持久化上传引用映射为 Agent 使用的唯一活动工作副本。

        用户消息继续保存原上传 ``document_id`` 作为不可变审计引用；这里只改写运行时附件，
        且必须沿 ``UploadArchiveRecord -> ManagedFile -> WorkingCopy`` 血缘映射，不能按文件名
        或内容哈希猜测、合并文件。
        """

        resolver = CanonicalWorkingFileResolver(self.db)
        canonical: list[dict[str, Any]] = []
        index_by_document_id: dict[str, int] = {}
        for attachment in attachments:
            source_document_id = str(attachment.get("document_id") or "")
            if not source_document_id:
                continue
            runtime_attachment = dict(attachment)
            try:
                resolved = resolver.resolve_document(document_id=source_document_id)
            except CanonicalWorkingFileError as exc:
                # 用户可能在异步归档/导入尚未生成活动工作副本前就发送任务。当前轮保持既有上传
                # 来源读取能力；一旦血缘就绪，后续每次 AgentRun 都会确定性改用工作副本 Document。
                log_event(
                    "conversation.attachment.canonicalization_deferred",
                    level="INFO",
                    document_id=source_document_id,
                    status="SKIPPED",
                    error_code=exc.code,
                    message="Agent 附件尚无可用工作副本，暂时保留上传来源引用",
                )
                target_document_id = source_document_id
            else:
                target_document_id = str(resolved.working_copy.document_id)
                runtime_attachment.update(
                    {
                        "document_id": target_document_id,
                        "document_version_id": str(resolved.document_version.id),
                        "working_copy_id": str(resolved.working_copy.id),
                        "mapped_from_upload": bool(resolved.mapped_from_upload),
                    }
                )
                if resolved.mapped_from_upload:
                    runtime_attachment.update(
                        {
                            "source_document_id": source_document_id,
                            "source_document_version_id": str(
                                resolved.source_document_version_id
                            ),
                        }
                    )
                log_event(
                    "conversation.attachment.canonicalized",
                    document_id=target_document_id,
                    status="COMPLETED",
                    source_document_id=source_document_id,
                    working_copy_id=str(resolved.working_copy.id),
                    mapped_from_upload=bool(resolved.mapped_from_upload),
                    message="Agent 附件已映射到唯一活动工作副本",
                )

            existing_index = index_by_document_id.get(target_document_id)
            if existing_index is not None:
                existing = canonical[existing_index]
                source_ids = list(existing.get("source_document_ids") or [])
                if source_document_id not in source_ids:
                    source_ids.append(source_document_id)
                existing["source_document_ids"] = source_ids
                continue
            runtime_attachment["source_document_ids"] = [source_document_id]
            index_by_document_id[target_document_id] = len(canonical)
            canonical.append(runtime_attachment)
        return canonical

    def _normalize_unique_filename_conflict_reply(
        self,
        *,
        conversation_id: str,
        user_id: str,
        content: str,
        has_explicit_attachments: bool,
    ) -> str:
        """只在唯一待决同名冲突中解释“是/取消”等短回复。

        用户消息仍按原文持久化；这里只为 Planner 生成明确动作。没有冲突、
        存在多个冲突或本轮重新附加文件时绝不扩展短回复，避免误触覆盖。
        """

        if has_explicit_attachments:
            return content
        compact = re.sub(r"\s+", "", content).strip("。！!")
        normalized_action = {
            "是": "覆盖已有文件",
            "是的": "覆盖已有文件",
            "确认": "覆盖已有文件",
            "取消": "取消同名处理",
            "算了": "取消同名处理",
            "不处理": "取消同名处理",
        }.get(compact)
        if normalized_action is None:
            return content
        reviews = (
            self.db.query(FileRenameReviewItem)
            .filter(
                FileRenameReviewItem.user_id == user_id,
                FileRenameReviewItem.conversation_id == conversation_id,
                FileRenameReviewItem.status == "NEEDS_REVIEW",
            )
            .order_by(FileRenameReviewItem.created_at.desc())
            .all()
        )
        conflicts = [
            item
            for item in reviews
            if dict(item.review_context_json or {}).get("reason")
            == "FILENAME_CONFLICT"
        ]
        return normalized_action if len(conflicts) == 1 else content

    def _existing_clarification_execution(
        self,
        selection: ResolvedSearchSelection,
    ) -> ConversationExecutionResult | None:
        """重复提交选择时返回首次执行结果，不创建第二条消息或 AgentRun。"""

        if not selection.result_message_id or not selection.result_agent_run_id:
            return None
        message = self.db.get(Message, selection.result_message_id)
        agent_run = self.db.get(AgentRun, selection.result_agent_run_id)
        if (
            message is None
            or agent_run is None
            or message.id != agent_run.message_id
            or message.conversation_id != selection.conversation_id
            or agent_run.conversation_id != selection.conversation_id
            or message.user_id != agent_run.user_id
        ):
            # 结果引用异常时不能猜测或复用其他会话数据。
            raise FileSearchClarificationError("已处理检索的结果引用无效")
        return ConversationExecutionResult(
            message=self.repository.to_schema(message),
            agent_run=AgentRunRepository(self.db).to_result(agent_run),
        )

    def get_conversation_detail(
        self,
        conversation_id: str,
        user_id: str,
        limit: int = 10,
        before_message_id: str | None = None,
    ) -> ConversationDetailResponse:
        """读取会话详情，供前端刷新后恢复历史聊天记录。"""

        return self.repository.get_detail(
            conversation_id=conversation_id,
            user_id=user_id,
            limit=limit,
            before_message_id=before_message_id,
        )

    def clear_conversation_history(self, *, conversation_id: str, user_id: str) -> ClearConversationResponse:
        """清空当前用户的聊天显示历史，保留文件和运行审计。"""

        cleared_count = self.repository.clear_visible_history(
            conversation_id=conversation_id,
            user_id=user_id,
        )
        self.db.commit()
        return ClearConversationResponse(
            conversation_id=conversation_id,
            cleared_message_count=cleared_count,
        )
