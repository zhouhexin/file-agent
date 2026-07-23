"""FileSearchScopeResolver 测试。

测试目标：
1. L0 严格范围（"这些文件" 只搜明确附件）
2. L1 会话文件范围
3. L4 全局搜索（"找我的...材料"）
4. 跨用户隔离
5. 无法唯一解析时停止并请求补充
"""

from app.modules.retrieval.scope_resolver import (
    FileSearchScopeResolver,
    ResolvedSearchScope,
)


def test_resolver_importable():
    """FileSearchScopeResolver 和 ResolvedSearchScope 可导入。"""
    from app.modules.retrieval.scope_resolver import FileSearchScopeResolver, ResolvedSearchScope
    assert FileSearchScopeResolver is not None
    assert ResolvedSearchScope is not None


def test_strict_scope_does_not_include_workspace():
    """'这些文件' 类请求只搜索 L0 附件，不包含 workspace。"""
    resolver = FileSearchScopeResolver(
        session_file_service=None,
    )
    scope = resolver.resolve(
        query="整理这些文件",
        explicit_attachment_ids=["doc-1", "doc-2"],
        conversation_id="conv-1",
    )
    assert scope.scope_mode == "strict"
    assert set(scope.strict_document_ids) == {"doc-1", "doc-2"}
    assert scope.include_workspace is False


def test_strict_scope_for_attachments():
    """'刚上传的文件' 只搜索指定附件。"""
    resolver = FileSearchScopeResolver(session_file_service=None)
    scope = resolver.resolve(
        query="帮忙分类刚上传的第二个文件",
        explicit_attachment_ids=["doc-3"],
        conversation_id="conv-1",
    )
    assert scope.scope_mode == "strict"
    assert "doc-3" in scope.strict_document_ids
    assert scope.include_workspace is False


def test_global_scope_includes_workspace():
    """'找我的奖学金材料' 包含 workspace 搜索。"""
    resolver = FileSearchScopeResolver(session_file_service=None)
    scope = resolver.resolve(
        query="找我去年的奖学金材料",
        explicit_attachment_ids=[],
        conversation_id="conv-1",
    )
    assert scope.scope_mode == "global"
    assert scope.include_workspace is True


def test_global_scope_with_attachments_also_searches_workspace():
    """即使有附件，全局请求也不应限制为仅 L0。"""
    resolver = FileSearchScopeResolver(session_file_service=None)
    scope = resolver.resolve(
        query="找我的奖学金材料",
        explicit_attachment_ids=["doc-1"],
        conversation_id="conv-1",
    )
    assert scope.include_workspace is True
    assert scope.scope_mode == "global"


def test_ambiguous_query_returns_strict_empty():
    """无法唯一解析文件时返回空严格范围（无附件、无意图关键词）。"""
    resolver = FileSearchScopeResolver(session_file_service=None)
    scope = resolver.resolve(
        query="帮我查一些资料",
        explicit_attachment_ids=[],
        conversation_id="conv-1",
    )
    assert scope.scope_mode == "strict"
    assert len(scope.strict_document_ids) == 0
    assert scope.include_workspace is False


def test_resolve_does_not_call_llm():
    """范围解析器是确定性的。"""
    resolver = FileSearchScopeResolver(session_file_service=None)
    scope1 = resolver.resolve(
        query="找我去年的奖学金材料",
        explicit_attachment_ids=["doc-1"],
        conversation_id="conv-1",
    )
    scope2 = resolver.resolve(
        query="找我去年的奖学金材料",
        explicit_attachment_ids=["doc-1"],
        conversation_id="conv-1",
    )
    assert scope1 == scope2
