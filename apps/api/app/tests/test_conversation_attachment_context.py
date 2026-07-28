"""会话附件上下文解析测试。

这些测试保护“刚刚上传”“之前所有”等自然语言附件范围不会被 LLM 或 Graph 节点猜测，
而是先由后端上下文解析服务转换为确定的 document_id 列表。
"""

import pytest

from app.modules.conversations.context import ConversationAttachmentContextService
from app.modules.conversations.repository import _explicit_filename_from_content
from app.modules.conversations.schemas import MessageAttachment


class FakeConversationRepository:
    """用于附件上下文单元测试的轻量仓库替身。"""

    def __init__(self) -> None:
        """初始化调用记录和固定返回值。"""

        self.calls: list[str] = []

    def get_latest_attachment_batch_references(self, **_: object) -> list[MessageAttachment]:
        """模拟最近真实上传批次。"""

        self.calls.append("latest")
        return [MessageAttachment(document_id="latest-doc")]

    def get_recent_attachment_references(self, **_: object) -> list[MessageAttachment]:
        """模拟最近上下文附件。"""

        self.calls.append("recent")
        return [
            MessageAttachment(document_id="latest-doc"),
            MessageAttachment(document_id="old-doc"),
        ]

    def get_all_attachment_references(self, **_: object) -> list[MessageAttachment]:
        """模拟当前会话全部附件。"""

        self.calls.append("all")
        return [
            MessageAttachment(document_id="old-doc"),
            MessageAttachment(document_id="latest-doc"),
        ]

    def get_filename_matched_attachment_references(self, **kwargs: object) -> list[MessageAttachment]:
        """模拟按文件名片段匹配历史附件。"""

        self.calls.append("filename")
        content = str(kwargs.get("content") or "")
        if "2019年学院科研成果资助表" not in content:
            return []
        return [MessageAttachment(document_id="named-doc")]


class SingleFileConversationRepository(FakeConversationRepository):
    """模拟当前会话只有一个不同文件，允许泛指删除唯一解析。"""

    def get_recent_attachment_references(self, **_: object) -> list[MessageAttachment]:
        """只返回当前会话唯一文件。"""

        self.calls.append("recent")
        return [MessageAttachment(document_id="latest-doc")]


def test_context_resolver_deduplicates_same_explicit_document_id_only():
    """显式附件重复引用同一 ID 时只处理一次，但不同 ID 的同名文件不在此层合并。"""

    repository = FakeConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-1",
        user_id="user-1",
        content="汇总附件数据",
        explicit_attachments=[
            MessageAttachment(document_id="same-doc"),
            MessageAttachment(document_id="same-doc"),
            MessageAttachment(document_id="different-doc"),
        ],
    )

    assert repository.calls == []
    assert context.scope == "current_message"
    assert [attachment.document_id for attachment in context.attachments] == [
        "same-doc",
        "different-doc",
    ]


def test_context_resolver_uses_all_conversation_scope_for_history_all_request():
    """“之前所有/历史全部”必须解析为当前会话全部文件，而不是最近几条消息。"""

    repository = FakeConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-1",
        user_id="user-1",
        content="帮我总结一下之前所有上传文件的分类",
        explicit_attachments=[],
    )

    assert repository.calls == ["filename", "all"]
    assert context.scope == "all_conversation"
    assert [attachment.document_id for attachment in context.attachments] == ["old-doc", "latest-doc"]


def test_context_resolver_uses_all_conversation_scope_for_uploaded_all_request():
    """“上传的所有文件”必须解析为当前会话全部附件，不能退到最近上下文。"""

    repository = FakeConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-1",
        user_id="user-1",
        content="帮我总结上传的所有文件分类",
        explicit_attachments=[],
    )

    assert repository.calls == ["filename", "all"]
    assert context.scope == "all_conversation"
    assert [attachment.document_id for attachment in context.attachments] == ["old-doc", "latest-doc"]


def test_context_resolver_uses_latest_batch_for_just_uploaded_request():
    """“刚刚上传”必须解析为最近真实上传批次。"""

    repository = FakeConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-1",
        user_id="user-1",
        content="帮我总结一下刚刚上传的所有文件分类",
        explicit_attachments=[],
    )

    assert repository.calls == ["filename", "latest"]
    assert context.scope == "latest_upload_batch"
    assert [attachment.document_id for attachment in context.attachments] == ["latest-doc"]


@pytest.mark.parametrize(
    ("content", "expected_call", "expected_scope"),
    [
        ("删除刚刚上传的文件", "latest", "latest_upload_batch"),
        ("把刚才上传的附件删掉", "latest", "latest_upload_batch"),
        ("这个文件我不要了", "recent", "all_recent_context"),
        ("把它删了", "recent", "all_recent_context"),
    ],
)
def test_context_resolver_infers_attachments_for_colloquial_file_removal(
    content,
    expected_call,
    expected_scope,
):
    """无显式附件的删除口语必须先由后端解析上文文件，不能让 Planner 猜测对象。"""

    repository = FakeConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-removal",
        user_id="user-1",
        content=content,
        explicit_attachments=[],
    )

    assert repository.calls == ["filename", expected_call]
    assert context.scope == expected_scope
    assert context.attachments


def test_context_resolver_uses_only_conversation_file_for_whole_workbook_removal():
    """“删除整个工作簿文件”仅在会话只有一个文件时才能确定对象。"""

    repository = SingleFileConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-single-workbook",
        user_id="user-1",
        content="删除整个工作簿文件",
        explicit_attachments=[],
    )

    assert repository.calls == ["filename", "recent"]
    assert context.scope == "single_conversation_file_removal"
    assert [item.document_id for item in context.attachments] == ["latest-doc"]


@pytest.mark.parametrize(
    "content",
    [
        "删除这个对话",
        "删除这个文件中的空行",
        "不要删除这个文件",
    ],
)
def test_context_resolver_does_not_infer_files_for_non_file_removal(content):
    """非文件删除或否定表达不得从历史消息补入附件，避免后续计划作用于错误对象。"""

    repository = FakeConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-removal-negative",
        user_id="user-1",
        content=content,
        explicit_attachments=[],
    )

    assert context.scope == "none"
    assert context.attachments == []


def test_context_resolver_uses_recent_first_for_previous_single_file_request():
    """“上一个文件”必须解析为最近上下文中的第一个附件。"""

    repository = FakeConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-1",
        user_id="user-1",
        content="重新对上一个文件分类",
        explicit_attachments=[],
    )

    assert repository.calls == ["filename", "recent"]
    assert context.scope == "all_recent_context"
    assert [attachment.document_id for attachment in context.attachments] == ["latest-doc"]


def test_context_resolver_uses_filename_reference_before_recent_scope():
    """用户按文件名片段提问时，应优先解析为对应历史附件。"""

    repository = FakeConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-1",
        user_id="user-1",
        content="汇总2019年学院科研成果资助表中的金额",
        explicit_attachments=[],
    )

    assert repository.calls == ["filename"]
    assert context.scope == "filename_reference"
    assert [attachment.document_id for attachment in context.attachments] == ["named-doc"]


def test_explicit_filename_parser_preserves_full_name_for_exact_context_scope():
    """完整文件名不能被拆成“西安理工大学”等短词后参与历史附件模糊匹配。"""

    assert _explicit_filename_from_content(
        "请完整总结西安理工大学用印申请单.docx，覆盖每个章节"
    ) == "西安理工大学用印申请单.docx"


def test_target_rename_filename_is_not_used_as_historical_source_reference():
    """“重命名为 B”里的 B 是目标名称，不能碰巧匹配到历史中的另一份文件。"""

    repository = FakeConversationRepository()
    context = ConversationAttachmentContextService(repository).resolve(
        conversation_id="chat-rename-target",
        user_id="user-1",
        content="重命名为 2019年学院科研成果资助表.xlsx",
        explicit_attachments=[],
    )

    assert repository.calls == []
    assert context.scope == "none"
    assert context.attachments == []
