"""阶段四只读文件检索兼容 API。

普通用户仍以聊天消息为主入口；本路由复用同一两阶段检索、范围解析和权限校验，
不能接受路径、任意 user_id 或未校验的内容版本 ID。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.agent.user_receipt import build_user_task_receipt
from app.modules.conversations.schemas import SendMessageResponse
from app.modules.conversations.schemas import MessageAttachment, SendMessageRequest
from app.modules.conversations.service import ConversationMessageService
from app.modules.chunks.tokenizer import ChineseLexicalTokenizer, load_default_business_terms
from app.modules.retrieval.query_parser import FileSearchQueryParser
from app.modules.retrieval.phrase_strategy import FileSearchPhraseStrategyService
from app.modules.retrieval.scope_resolver import (
    ConversationFileSearchContextService,
    FileSearchScopeResolver,
)
from app.modules.retrieval.two_stage_search import TwoStageFileSearchService
from app.modules.retrieval.completeness import SearchCompletenessService
from app.modules.retrieval.readiness import WorkingCopySearchReadinessService
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.retrieval.clarification_service import (
    FileSearchClarificationError,
    FileSearchClarificationService,
)
from app.modules.retrieval.relevant_file_sets import RelevantFileSetService


router = APIRouter(prefix="/api", tags=["search"])


class FileSearchRequest(BaseModel):
    """只允许普通用户提交查询、会话与当前附件稳定 ID。"""

    query: str = Field(min_length=1, max_length=500)
    conversation_id: str | None = Field(default=None, max_length=36)
    attachment_document_ids: list[str] = Field(default_factory=list, max_length=50)
    # API 最多返回 20 个已收敛候选；聊天页默认展示 10 个并在本地展开更多。
    top_k: int = Field(default=10, ge=1, le=20)


class FileSearchClarificationResolveRequest(BaseModel):
    """选择卡只提交服务端选项 ID；文件卡允许提交一个或多个 ID。"""

    option_id: str | None = Field(default=None, min_length=1, max_length=80)
    option_ids: list[str] = Field(default_factory=list, max_length=50)
    custom_phrase: str | None = Field(default=None, max_length=30)


class EvidenceAnswerRequest(BaseModel):
    """专用兼容接口仍复用普通消息链路，不允许客户端提交 Evidence 或模型参数。"""

    question: str = Field(min_length=1, max_length=4000)
    attachment_document_ids: list[str] = Field(default_factory=list, max_length=50)


@router.get("/file-search/clarifications/{clarification_id}")
def get_file_search_clarification(
    clarification_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """读取选择卡最新状态，使页面刷新后不再显示过期的待选择状态。"""

    try:
        payload = FileSearchClarificationService(db).get_public(
            clarification_id=clarification_id,
            user_id=current_user.id,
        )
        db.commit()
        return payload
    except FileSearchClarificationError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/search")
def search_files(
    request: FileSearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """执行与聊天一致的低耗两阶段文件检索并返回安全用户投影。"""

    # 检索范围是唯一共享工作目录；默认工作区只保存用户会话与上传来源。
    workspace_id = get_shared_workspace_id(db)
    tokenizer = ChineseLexicalTokenizer(load_default_business_terms())
    parsed = FileSearchQueryParser(tokenizer=tokenizer).parse(request.query)
    # 兼容 API 与聊天 Tool 一样先把上传附件映射为活动工作副本，避免严格范围下
    # 因临时上传 Document ID 而错误报告“没有文件”或“已找全”。
    canonical_scope = WorkingCopySearchReadinessService(
        db=db,
        user_id=current_user.id,
        workspace_id=workspace_id,
    ).canonicalize_document_ids(request.attachment_document_ids)
    scope = FileSearchScopeResolver(
        session_file_service=ConversationFileSearchContextService(
            db=db,
            user_id=current_user.id,
        )
    ).resolve(
        query=request.query,
        explicit_attachment_ids=canonical_scope.document_ids,
        conversation_id=request.conversation_id,
    )
    search_service = TwoStageFileSearchService(
        db=db,
        user_id=current_user.id,
        workspace_id=workspace_id,
        config=get_settings(),
        tokenizer=tokenizer,
    )
    # 兼容 API 与聊天主入口必须使用相同的证据边界。对于“涉及核心主题 +
    # 宽泛动作词”的请求，不能直接暴露两阶段 OR 召回结果为已验证相关。
    if (
        parsed.relation_mode == "LITERAL"
        and parsed.required_topic_terms
        and parsed.supporting_topic_terms
    ):
        result = FileSearchPhraseStrategyService(
            search_service=search_service,
            tokenizer=tokenizer,
        ).search_with_topic_tiers(
            original_query=request.query,
            parsed_query=parsed,
            scope=scope,
            exact_phrase=parsed.cleaned,
            required_topic_terms=parsed.required_topic_terms,
            supporting_topic_terms=parsed.supporting_topic_terms,
        )
    else:
        result = search_service.search(
            query=request.query,
            parsed_query=parsed,
            scope=scope,
        )
    result = SearchCompletenessService(
        db=db,
        workspace_id=workspace_id,
    ).attach_safely(
        result=result,
        scope=scope,
        unresolved_document_count=len(canonical_scope.unresolved_document_ids),
    )
    # 兼容检索 API 与聊天主入口使用同一相关文件集合规则；只对最终相关文件
    # 入队物化，不能因当前页面的 top_k 截断而遗漏后续结果。
    RelevantFileSetService(db=db, settings=get_settings()).persist_and_enqueue(
        workspace_id=workspace_id,
        user_id=current_user.id,
        conversation_id=request.conversation_id,
        agent_run_id=None,
        query=request.query,
        results=list(result.get("results") or []),
    )
    files = list(result.get("results") or [])[: request.top_k]
    return {
        "query": result.get("query", request.query),
        "total_returned": int(result.get("total_returned") or 0),
        "supported_count": result.get("supported_count"),
        "possible_count": result.get("possible_count"),
        "partial": bool(result.get("partial", False)),
        "user_message": str(result.get("user_message") or ""),
        "search_completeness": result.get("search_completeness"),
        "files": files,
    }


@router.post(
    "/conversations/{conversation_id}/evidence-answer",
    response_model=SendMessageResponse,
)
def answer_from_evidence(
    conversation_id: str,
    request: EvidenceAnswerRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SendMessageResponse:
    """通过与聊天完全相同的 AgentRun 执行证据回答，避免形成第二套权限和回执逻辑。"""

    execution = ConversationMessageService(db=db).send_user_message(
        conversation_id=conversation_id,
        request=SendMessageRequest(
            content=request.question,
            attachments=[
                MessageAttachment(document_id=document_id)
                for document_id in request.attachment_document_ids
            ],
        ),
        user_id=current_user.id,
    )
    return SendMessageResponse(
        message=execution.message,
        task_result=build_user_task_receipt(execution.agent_run),
    )


@router.post(
    "/file-search/clarifications/{clarification_id}/resolve",
    response_model=SendMessageResponse,
)
def resolve_file_search_clarification(
    clarification_id: str,
    request: FileSearchClarificationResolveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SendMessageResponse:
    """解决文件检索范围歧义，并通过新的 AgentRun 继续执行所选范围。"""

    try:
        execution = ConversationMessageService(
            db=db
        ).resolve_file_search_clarification(
            clarification_id=clarification_id,
            option_id=request.option_id,
            option_ids=request.option_ids,
            custom_phrase=request.custom_phrase,
            user_id=current_user.id,
        )
    except FileSearchClarificationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SendMessageResponse(
        message=execution.message,
        task_result=build_user_task_receipt(execution.agent_run),
    )
