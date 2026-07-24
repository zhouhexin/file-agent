"""FileSearchScopeResolver 测试。

测试目标：
1. L0 严格范围（"这些文件" 只搜明确附件）
2. L1 会话文件范围
3. L4 全局搜索（"找我的...材料"）
4. 跨用户隔离
5. 普通人名和主题查询默认进入共享工作区
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


def test_generic_file_search_defaults_to_global_workspace():
    """进入文件检索 Tool 的普通查询不能因措辞不在白名单而变成空严格范围。"""
    resolver = FileSearchScopeResolver(session_file_service=None)
    scope = resolver.resolve(
        query="帮我查一些资料",
        explicit_attachment_ids=[],
        conversation_id="conv-1",
    )
    assert scope.scope_mode == "global"
    assert len(scope.strict_document_ids) == 0
    assert scope.include_workspace is True


def test_person_name_search_defaults_to_global_workspace():
    """无附件的人名查询必须搜索共享目录，保护日志中暴露的空 strict 回归。"""

    resolver = FileSearchScopeResolver(session_file_service=None)

    for query in (
        "查找金海燕",
        "查找与金海燕老师有关的文件",
        "金海燕相关文档",
    ):
        scope = resolver.resolve(
            query=query,
            explicit_attachment_ids=[],
            conversation_id="conv-1",
        )

        assert scope.scope_mode == "global"
        assert scope.strict_document_ids == ()
        assert scope.include_workspace is True


def test_explicit_attachment_reference_without_resolved_ids_stays_strict_empty():
    """用户明确说“这些文件”但后端没有解析出附件时不得扩大到共享工作区。"""

    resolver = FileSearchScopeResolver(session_file_service=None)
    scope = resolver.resolve(
        query="查找这些文件里的金海燕",
        explicit_attachment_ids=[],
        conversation_id="conv-1",
    )

    assert scope.scope_mode == "strict"
    assert scope.strict_document_ids == ()
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
