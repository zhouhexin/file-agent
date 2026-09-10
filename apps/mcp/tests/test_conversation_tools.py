"""WorkBuddy 对话搜索、读取、证据问答和明确重命名工具测试。"""

from __future__ import annotations

import asyncio

import pytest

from file_agent_mcp.conversation_tools import (
    WorkBuddyConversationService,
    conversation_id_for_workbuddy,
)


class FakeConversationClient:
    """记录发送给 File Agent 的受控聊天请求。"""

    def __init__(self) -> None:
        """初始化调用记录。"""

        self.calls: list[dict] = []

    async def conversation_task(self, **kwargs) -> dict:
        """记录普通聊天任务。"""

        self.calls.append({"method": "conversation_task", **kwargs})
        return {"task_result": {"task_status": "completed"}}

    async def file_search(self, **kwargs) -> dict:
        """记录后端只读搜索调用。"""

        self.calls.append({"method": "file_search", **kwargs})
        return {"files": [], "total_returned": 0}

    async def evidence_answer(self, **kwargs) -> dict:
        """记录证据回答任务。"""

        self.calls.append({"method": "evidence_answer", **kwargs})
        return {"task_result": {"response_type": "evidence_answer"}}

    async def classification_placement_submit(self, **kwargs) -> dict:
        """记录分类落位提交，不执行任何真实网络请求。"""

        self.calls.append({"method": "classification_placement_submit", **kwargs})
        return {"operation_id": "placement-1", "status": "PREPARED"}

    async def classification_placement_status(self, **kwargs) -> dict:
        """记录分类落位状态查询。"""

        self.calls.append({"method": "classification_placement_status", **kwargs})
        return {"operation_id": kwargs["operation_id"], "status": "COMMITTED"}


def test_workbuddy_conversation_ref_maps_to_stable_bounded_internal_id() -> None:
    """外部会话引用必须稳定映射，且不能直接成为数据库主键。"""

    first = conversation_id_for_workbuddy("workspace/message/thread-1")
    second = conversation_id_for_workbuddy("workspace/message/thread-1")

    assert first == second
    assert first.startswith("wb-")
    assert len(first) == 35
    assert "workspace" not in first


def test_file_search_uses_chat_entry_and_preserves_query() -> None:
    """聊天搜索必须复用 Agent 主入口，保留后端范围澄清能力。"""

    client = FakeConversationClient()
    result = asyncio.run(
        WorkBuddyConversationService(client).search(
            conversation_ref="thread-1",
            query="找去年奖学金材料",
        )
    )

    assert result["total_returned"] == 0
    assert client.calls[0]["method"] == "file_search"
    assert client.calls[0]["query"] == "找去年奖学金材料"
    assert client.calls[0]["conversation_id"] == conversation_id_for_workbuddy("thread-1")


def test_file_read_requires_stable_document_scope() -> None:
    """文件读取不能退化为让 WorkBuddy 传路径或让模型猜目标文件。"""

    service = WorkBuddyConversationService(FakeConversationClient())

    with pytest.raises(ValueError, match="document_id"):
        asyncio.run(
            service.read(
                conversation_ref="thread-1",
                document_ids=[],
                read_mode="READ",
            )
        )


def test_file_read_uses_fixed_read_only_prompt() -> None:
    """读取工具只允许固定模式，不能把任意写操作文字送入通用 Agent。"""

    client = FakeConversationClient()
    service = WorkBuddyConversationService(client)
    asyncio.run(
        service.read(
            conversation_ref="thread-read",
            document_ids=["doc-1"],
            read_mode="SUMMARY",
        )
    )
    assert client.calls[0]["content"] == "总结这些文件"

    with pytest.raises(ValueError, match="read_mode"):
        asyncio.run(
            service.read(
                conversation_ref="thread-read",
                document_ids=["doc-1"],
                read_mode="删除这些文件",
            )
        )


def test_evidence_answer_forwards_question_and_ids_without_accepting_evidence() -> None:
    """WorkBuddy 只能提交问题和文件范围，不能提交自造证据。"""

    client = FakeConversationClient()
    asyncio.run(
        WorkBuddyConversationService(client).answer(
            conversation_ref="thread-2",
            question="申请截止日期是什么？",
            document_ids=["doc-1", "doc-1", "doc-2"],
        )
    )

    assert client.calls[0]["method"] == "evidence_answer"
    assert client.calls[0]["question"] == "申请截止日期是什么？"
    assert client.calls[0]["document_ids"] == ["doc-1", "doc-2"]


def test_explicit_rename_builds_exact_commands_and_attachment_scope() -> None:
    """明确重命名必须同时固定 document_id 与 before/after 名称。"""

    client = FakeConversationClient()
    asyncio.run(
        WorkBuddyConversationService(client).rename(
            conversation_ref="thread-rename",
            renames=[
                {
                    "document_id": "doc-1",
                    "source_filename": "旧文件.docx",
                    "target_filename": "新文件.docx",
                },
                {
                    "document_id": "doc-2",
                    "source_filename": "旧表格.xlsx",
                    "target_filename": "新表格.xlsx",
                },
            ],
        )
    )

    call = client.calls[0]
    assert call["document_ids"] == ["doc-1", "doc-2"]
    assert call["content"] == (
        "把“旧文件.docx”重命名为“新文件.docx”\n"
        "把“旧表格.xlsx”重命名为“新表格.xlsx”"
    )


def test_explicit_rename_rejects_paths_and_duplicate_targets() -> None:
    """MCP 重命名只允许 basename，且同一文件不能在一轮中出现两次。"""

    service = WorkBuddyConversationService(FakeConversationClient())
    with pytest.raises(ValueError, match="安全文件名"):
        asyncio.run(
            service.rename(
                conversation_ref="thread-rename",
                renames=[
                    {
                        "document_id": "doc-1",
                        "source_filename": "目录/旧文件.docx",
                        "target_filename": "新文件.docx",
                    }
                ],
            )
        )
    with pytest.raises(ValueError, match="重复重命名"):
        asyncio.run(
            service.rename(
                conversation_ref="thread-rename",
                renames=[
                    {
                        "document_id": "doc-1",
                        "source_filename": "旧文件.docx",
                        "target_filename": "新文件.docx",
                    },
                    {
                        "document_id": "doc-1",
                        "source_filename": "新文件.docx",
                        "target_filename": "再次改名.docx",
                    },
                ],
            )
        )


def test_classification_placement_forwards_only_stable_ids_and_revision() -> None:
    """MCP 分类移动只转发冻结对象事实，不能夹带本机路径或客户端授权字段。"""

    client = FakeConversationClient()
    service = WorkBuddyConversationService(client)
    result = asyncio.run(
        service.submit_classification_placement(
            request_id="workbuddy-placement-1",
            command={
                "working_copy_id": "11111111-1111-4111-8111-111111111111",
                "action": "SET_PRIMARY",
                "expected_revision": 3,
                "expected_document_version_id": "22222222-2222-4222-8222-222222222222",
                "target_category_id": "college.finance",
                "taxonomy_version": "2026-09-v10",
                "idempotency_key": "placement-workbuddy-1",
            },
        )
    )

    assert result["operation_id"] == "placement-1"
    assert client.calls[0] == {
        "method": "classification_placement_submit",
        "request_id": "workbuddy-placement-1",
        "command": {
            "working_copy_id": "11111111-1111-4111-8111-111111111111",
            "action": "SET_PRIMARY",
            "expected_revision": 3,
            "expected_document_version_id": "22222222-2222-4222-8222-222222222222",
            "target_category_id": "college.finance",
            "taxonomy_version": "2026-09-v10",
            "container_segments": [],
            "target_root_key": None,
            "target_directory_segments": [],
            "idempotency_key": "placement-workbuddy-1",
        },
    }

    status = asyncio.run(service.get_classification_placement_status(operation_id="placement-1"))
    assert status["status"] == "COMMITTED"
    assert client.calls[1] == {
        "method": "classification_placement_status",
        "operation_id": "placement-1",
    }

    with pytest.raises(ValueError, match="目录段"):
        asyncio.run(
            service.submit_classification_placement(
                request_id="workbuddy-placement-2",
                command={
                    "working_copy_id": "11111111-1111-4111-8111-111111111111",
                    "action": "MOVE",
                    "expected_revision": 3,
                    "expected_document_version_id": "22222222-2222-4222-8222-222222222222",
                    "target_root_key": "shared-working",
                    "target_directory_segments": ["../outside"],
                    "taxonomy_version": "2026-09-v10",
                    "idempotency_key": "placement-workbuddy-2",
                },
            )
        )
