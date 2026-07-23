"""阶段四只读文件检索兼容 API。

普通用户仍以聊天消息为主入口；本路由复用同一两阶段检索、范围解析和权限校验，
不能接受路径、任意 user_id 或未校验的内容版本 ID。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.chunks.tokenizer import ChineseLexicalTokenizer, load_default_business_terms
from app.modules.retrieval.query_parser import FileSearchQueryParser
from app.modules.retrieval.scope_resolver import (
    ConversationFileSearchContextService,
    FileSearchScopeResolver,
)
from app.modules.retrieval.two_stage_search import TwoStageFileSearchService


router = APIRouter(prefix="/api", tags=["search"])


class FileSearchRequest(BaseModel):
    """只允许普通用户提交查询、会话与当前附件稳定 ID。"""

    query: str = Field(min_length=1, max_length=500)
    conversation_id: str | None = Field(default=None, max_length=36)
    attachment_document_ids: list[str] = Field(default_factory=list, max_length=50)
    # API 最多返回 20 个已收敛候选；聊天页默认展示 10 个并在本地展开更多。
    top_k: int = Field(default=10, ge=1, le=20)


@router.post("/search")
def search_files(
    request: FileSearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """执行与聊天一致的低耗两阶段文件检索并返回安全用户投影。"""

    workspace_id = current_user.default_workspace_id
    if not workspace_id:
        return {
            "query": request.query,
            "total_returned": 0,
            "partial": False,
            "user_message": "当前用户尚未配置默认工作区，暂时无法搜索文件。",
            "files": [],
        }
    tokenizer = ChineseLexicalTokenizer(load_default_business_terms())
    parsed = FileSearchQueryParser(tokenizer=tokenizer).parse(request.query)
    scope = FileSearchScopeResolver(
        session_file_service=ConversationFileSearchContextService(
            db=db,
            user_id=current_user.id,
        )
    ).resolve(
        query=request.query,
        explicit_attachment_ids=request.attachment_document_ids,
        conversation_id=request.conversation_id,
    )
    result = TwoStageFileSearchService(
        db=db,
        user_id=current_user.id,
        workspace_id=workspace_id,
        config=get_settings(),
        tokenizer=tokenizer,
    ).search(query=request.query, parsed_query=parsed, scope=scope)
    files = list(result.get("results") or [])[: request.top_k]
    return {
        "query": result.get("query", request.query),
        "total_returned": int(result.get("total_returned") or 0),
        "partial": bool(result.get("partial", False)),
        "user_message": str(result.get("user_message") or ""),
        "files": files,
    }
