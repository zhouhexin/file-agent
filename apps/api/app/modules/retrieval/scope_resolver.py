"""文件搜索范围解析器，确定 L0/L1/L4 范围。

范围规则：
- L0 严格范围：用户说"这些文件""刚上传的文件""第二个附件"时，只搜索明确附件
- L1 排序范围：用户点名会话中某个文件时，精确搜索或明确集合
- L4 全局搜索：进入文件检索 Tool 且没有明确附件指代时，搜索整个共享工作区

范围只能由后端根据真实消息附件、会话记录和所有权解析；
Planner 或 LLM 不能自行猜测文件 ID。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.modules.conversations.repository import ConversationRepository
from app.modules.retrieval.query_parser import is_file_set_selector_question


# 严格范围关键词：用户明确指代当前附件
_STRICT_SCOPE_PATTERNS = [
    r"这些文件",
    r"刚上传",
    r"第[一二三四五六七八九十\d]+个附件",
    r"第[一二三四五六七八九十\d]+个文件",
    r"这个文件",
    r"这个附件",
    r"刚才.*文件",
    r"上面.*文件",
    r"第二个",
]

# 全局搜索关键词：用户明确表达"找我的..."或"找...材料/通知"意愿
_GLOBAL_SCOPE_PATTERNS = [
    r"找我的",
    r"找.*材料",
    r"找.*通知",
    r"找.*证明",
    r"找.*报告",
    r"找.*表格",
    r"有没有.*奖学金",
    r"有没有.*资助",
    r"有哪些.*材料",
    r"有哪些.*通知",
]


@dataclass(frozen=True)
class ResolvedSearchScope:
    """解析后的搜索范围。"""

    strict_document_ids: tuple[str, ...] = field(default_factory=tuple)
    conversation_document_ids: tuple[str, ...] = field(default_factory=tuple)
    include_workspace: bool = False
    scope_mode: str = "strict"


class FileSearchScopeResolver:
    """基于查询文本和附件确定搜索范围的解析器。

    确定性的：不含 LLM 猜测，相同输入总是相同输出。
    """

    def __init__(
        self,
        *,
        session_file_service: Any | None = None,
    ) -> None:
        self.session_file_service = session_file_service

    def resolve(
        self,
        *,
        query: str,
        explicit_attachment_ids: list[str] | None = None,
        conversation_id: str | None = None,
    ) -> ResolvedSearchScope:
        """解析查询文本和附件为搜索范围。"""

        if not query:
            return ResolvedSearchScope()

        attachment_ids = explicit_attachment_ids or []
        file_set_selector = is_file_set_selector_question(query)
        # “这些文件中哪些提到了……”在没有随本条消息绑定附件时是普通文件集合
        # 选择问句，必须与“哪些文件中提到了……”使用同一全局范围。若本条消息确实
        # 携带附件，仍以这些确定 document_id 为严格范围，不能擅自扩大。
        is_strict = self._is_strict_scope(query) and (
            bool(attachment_ids) or not file_set_selector
        )
        is_global = self._is_global_scope(query)

        if is_strict or (not is_global and attachment_ids):
            # 严格范围：只搜索明确附件
            return ResolvedSearchScope(
                strict_document_ids=tuple(attachment_ids),
                scope_mode="strict",
            )

        if is_global:
            # 全局搜索：L0 + L1 加权 + L4
            session_ids = self._get_session_file_ids(conversation_id)
            return ResolvedSearchScope(
                strict_document_ids=tuple(attachment_ids),
                conversation_document_ids=tuple(session_ids),
                include_workspace=True,
                scope_mode="global",
            )

        # 当前方法只会由已经确定为 SEARCH_FILES 的 Tool/API 调用。没有“这些文件”
        # 等严格指代且没有附件时，普通主题、人名、单位和关键词查询必须搜索共享
        # 工作区；不能因为未命中少量措辞白名单而退化成空严格集合。
        session_ids = self._get_session_file_ids(conversation_id)
        return ResolvedSearchScope(
            strict_document_ids=tuple(attachment_ids),
            conversation_document_ids=tuple(session_ids),
            include_workspace=True,
            scope_mode="global",
        )

    def _is_strict_scope(self, query: str) -> bool:
        """判断是否是严格范围请求（"这些文件"类）。"""
        for pattern in _STRICT_SCOPE_PATTERNS:
            if re.search(pattern, query):
                return True
        return False

    def _is_global_scope(self, query: str) -> bool:
        """判断是否是全局搜索请求（"找我的"类）。"""
        for pattern in _GLOBAL_SCOPE_PATTERNS:
            if re.search(pattern, query):
                return True
        return False

    def _get_session_file_ids(
        self, conversation_id: str | None
    ) -> list[str]:
        """获取 L1 会话文件范围（预留接口）。

        后续可通过 SessionFileTracker 读取当前会话已引用文件。
        """
        if not conversation_id or not self.session_file_service:
            return []
        try:
            return self.session_file_service.get_session_document_ids(
                conversation_id
            )
        except Exception:
            return []


class ConversationFileSearchContextService:
    """读取当前用户会话已出现的文件，作为 L1 排序范围。

    复用会话仓库的所有权校验，只返回稳定 document_id；不把消息正文、附件路径或
    数据库对象交给 Planner、Tool 输入或 AgentGraphState。
    """

    def __init__(self, *, db: Any, user_id: str) -> None:
        """保存请求级会话；会话范围读取失败时必须能够回滚独立 savepoint。"""

        self.db = db
        self.repository = ConversationRepository(db)
        self.user_id = user_id

    def get_session_document_ids(self, conversation_id: str) -> list[str]:
        """返回当前用户会话中曾出现过的受权文件 ID。"""
        # L1 只是排序增强，读取失败允许回退为空；savepoint 防止 PostgreSQL 的失败状态
        # 泄漏到后续两阶段检索和 ToolInvocation 审计写入。
        with self.db.begin_nested():
            references = self.repository.get_all_attachment_references(
                conversation_id=conversation_id,
                user_id=self.user_id,
            )
        return [str(item.document_id) for item in references if item.document_id]
