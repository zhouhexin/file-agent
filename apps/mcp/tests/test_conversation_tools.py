"""WorkBuddy 对话搜索、读取、证据问答和明确重命名工具测试。"""

from __future__ import annotations

import asyncio

import pytest

from file_agent_mcp.conversation_tools import (
    WorkBuddyConversationService,
    conversation_id_for_workbuddy,
    project_classification_files,
    project_classification_overview,
    project_file_search_result,
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

    async def classification_overview(self) -> dict:
        """返回只读分类总览。"""

        self.calls.append({"method": "classification_overview"})
        return {"total_active_files": 0, "nodes": []}

    async def classification_files(self, **kwargs) -> dict:
        """返回只读分类分页。"""

        self.calls.append({"method": "classification_files", **kwargs})
        return {"page": kwargs["page"], "page_size": kwargs["page_size"], "files": []}

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


def test_file_search_with_move_words_never_routes_to_placement_write() -> None:
    """搜索文字即使包含“移动”也只能调用只读搜索接口。"""

    client = FakeConversationClient()
    asyncio.run(
        WorkBuddyConversationService(client).search(
            conversation_ref="thread-read-only",
            query="找出奖学金材料后把它们移动到财务目录",
        )
    )

    assert [call["method"] for call in client.calls] == ["file_search"]


def test_classification_service_calls_only_read_endpoints() -> None:
    """分类浏览不得经过聊天写路由或分类落位接口。"""

    client = FakeConversationClient()
    service = WorkBuddyConversationService(client)
    asyncio.run(service.classification_overview())
    asyncio.run(
        service.classification_files(category_id="school.hr", page=2, page_size=20)
    )

    assert [call["method"] for call in client.calls] == [
        "classification_overview",
        "classification_files",
    ]
    assert client.calls[1]["category_id"] == "school.hr"


def test_classification_overview_projects_counts_and_page_links() -> None:
    """总览只展示计数和 Web 入口，稳定 ID 留在工具上下文。"""

    result = project_classification_overview(
        {
            "total_active_files": 12,
            "business_classified_file_count": 9,
            "other_file_count": 3,
            "taxonomy_version": "v20",
            "browser_url": "http://file-agent.test/files",
            "nodes": [
                {
                    "category_id": "school",
                    "name": "学校",
                    "category_path": ["学校"],
                    "subtree_file_count": 7,
                    "browser_url": "http://file-agent.test/files?category_id=school",
                    "children": [
                        {
                            "category_id": "school.hr",
                            "name": "人事师资",
                            "category_path": ["学校", "人事师资"],
                            "subtree_file_count": 4,
                            "browser_url": "http://file-agent.test/files?category_id=school.hr",
                            "children": [],
                        }
                    ],
                }
            ],
        }
    )

    assert "当前共有 12 个活动文件" in result["display_text"]
    assert "[打开完整分类页面](http://file-agent.test/files)" in result["display_text"]
    assert "| 学校 | 7 |" in result["display_text"]
    assert result["categories"][0]["tool_context"] == {"category_id": "school"}
    assert result["category_options"][1]["label"] == "学校 / 人事师资"
    assert "人事师资" not in result["display_text"]


def test_classification_files_projects_clickable_read_only_table() -> None:
    """分类文件页展示主分类、状态和公开链接，不显示相对路径与稳定 ID。"""

    result = project_classification_files(
        {
            "page": 1,
            "page_size": 20,
            "total": 1,
            "total_pages": 1,
            "category_id": "school.hr",
            "browser_url": "http://file-agent.test/files?category_id=school.hr",
            "files": [
                {
                    "filename": "推荐意见.docx",
                    "relative_path": "学校/人事师资/推荐意见.docx",
                    "working_copy_id": "copy-1",
                    "document_id": "doc-1",
                    "document_version_id": "version-1",
                    "classification_outcome": "CLASSIFIED",
                    "primary_category_status": "AUTO_APPLIED",
                    "effective_primary": {"category_path": ["学校", "人事师资", "人才工作"]},
                    "preview_url": "http://file-agent.test/api/public/file-access/t/preview",
                    "download_url": "http://file-agent.test/api/public/file-access/t/download",
                }
            ],
        }
    )

    visible = result["display_text"]
    assert "| 文件名 | 主分类 | 状态 | 操作 |" in visible
    assert "学校 / 人事师资 / 人才工作" in visible
    assert "自动归类" in visible
    assert "[预览](http://file-agent.test/api/public/file-access/t/preview)" in visible
    assert "学校/人事师资/推荐意见.docx" not in visible
    assert "copy-1" not in visible
    assert result["files"][0]["tool_context"]["working_copy_id"] == "copy-1"


def test_file_search_projects_only_filename_and_basis_for_user_display() -> None:
    """WorkBuddy 搜索展示不能包含类型、路径、分类和相关度，但须保留机器引用。"""

    result = project_file_search_result(
        {
            "query": "人才推荐",
            "files": [
                {
                    "filename": "推荐意见-[测试].docx",
                    "document_id": "doc-1",
                    "working_copy_id": "copy-1",
                    "document_version_id": "version-1",
                    "revision": 3,
                    "resource_type": "WORKING_COPY",
                    "relative_path": "人事处/人才工程科/推荐意见.docx",
                    "category_path": ["学校", "人事处", "人才工作"],
                    "relevance_tier": "SUPPORTED",
                    "match_reasons": ["正文主题命中：人才推荐"],
                    "match_location": {"page_number": 2},
                    "evidence_preview": "经学院研究，同意推荐该同志申报人才项目。",
                    "preview_url": "http://file-agent.test/api/public/file-access/read-token/preview",
                    "download_url": "http://file-agent.test/api/public/file-access/read-token/download",
                }
            ],
        }
    )

    assert result["files"] == [
        {
            "filename": "推荐意见-[测试].docx",
            "basis": [
                "正文主题命中：人才推荐",
                "原文依据（第 2 页）：经学院研究，同意推荐该同志申报人才项目。",
            ],
            "preview_url": "http://file-agent.test/api/public/file-access/read-token/preview",
            "download_url": "http://file-agent.test/api/public/file-access/read-token/download",
            "tool_context": {
                "document_id": "doc-1",
                "working_copy_id": "copy-1",
                "document_version_id": "version-1",
                "revision": 3,
            },
            "available_actions": {"preview": True, "download": True},
        }
    ]
    display = result["display_text"]
    assert display.startswith("<!-- FILE_AGENT_DISPLAY_CONTRACT:")
    assert "推荐意见" in display
    assert "正文主题命中" in display
    assert "第 2 页" in display
    assert "人事处/人才工程科" not in display
    assert "WORKING_COPY" not in display
    assert "SUPPORTED" not in display
    assert "doc-1" not in display
    assert "\\[测试\\]" in display
    assert "| 文件名 | 依据 | 操作 |" in display
    assert "[预览](http://file-agent.test/api/public/file-access/read-token/preview)" in display
    assert "[下载](http://file-agent.test/api/public/file-access/read-token/download)" in display
    assert "逐字原样展示" in result["display_policy"]
    assert "不得增加文件类型" in result["display_policy"]
    assert result["response_contract"] == {
        "mode": "VERBATIM_USER_DISPLAY",
        "source": "content.text",
        "allow_rewrite": False,
        "allow_column_changes": False,
        "required_columns": ["文件名", "依据", "操作"],
        "preserve_markdown_links": True,
    }


def test_file_search_rejects_non_http_action_links() -> None:
    """搜索结果中的链接只能来自后端 HTTP(S) 地址，不能被文件数据伪造成脚本链接。"""

    result = project_file_search_result(
        {
            "files": [
                {
                    "filename": "测试.txt",
                    "document_id": "doc-1",
                    "working_copy_id": "copy-1",
                    "preview_url": "javascript:alert(1)",
                    "download_url": "file:///server/secret.txt",
                }
            ]
        }
    )

    assert result["files"][0]["preview_url"] is None
    assert result["files"][0]["download_url"] is None
    assert result["files"][0]["available_actions"] == {
        "preview": False,
        "download": False,
    }
    assert result["display_text"].startswith("<!-- FILE_AGENT_DISPLAY_CONTRACT:")
    assert "工作副本生成中" in result["display_text"]


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
