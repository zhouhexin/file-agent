"""会话附件上下文解析测试。

这些测试保护“刚刚上传”“之前所有”等自然语言附件范围不会被 LLM 或 Graph 节点猜测，
而是先由后端上下文解析服务转换为确定的 document_id 列表。
"""

from app.modules.conversations.context import ConversationAttachmentContextService
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
        return [MessageAttachment(document_id="recent-doc")]

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
