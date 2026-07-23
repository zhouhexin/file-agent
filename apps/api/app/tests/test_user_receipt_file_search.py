"""UserTaskReceipt 扩展 file_search_result 的测试。"""

from app.modules.agent.state import (
    AgentRunResult,
    ToolInvocationRecord,
)
from app.modules.agent.user_receipt import build_user_task_receipt


def _make_result(*, tool_invocations, status="COMPLETED"):
    """构造测试用 AgentRunResult。"""

    return AgentRunResult(
        agent_run_id="run-1",
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        intent="SEARCH_FILES",
        status=status,
        selected_skills=["file-search"],
        tool_plan={},
        tool_results=[],
        tool_invocations=tool_invocations,
        final_response="找到文件",
    )


def test_old_search_result_does_not_activate_new_field():
    """旧链路（无 total_returned）保持 final_response 文本格式，不激活新字段。"""

    invocation = ToolInvocationRecord(
        tool_name="hybrid-search",
        input_json={"query": "奖学金"},
        output_json={
            "kind": "workspace_file_search",
            "ok": True,
            "query": "奖学金",
            "results": [{"document_id": "d1", "filename": "a.docx"}],
        },
        status="COMPLETED",
    )
    receipt = build_user_task_receipt(_make_result(tool_invocations=[invocation]))

    assert receipt.file_search_result is None, (
        "旧链路（无 total_returned）不应激活 file_search_result"
    )
    assert receipt.response_type == "text"


def test_new_search_result_activates_file_search_field():
    """新链路（含 total_returned）激活 file_search_result。"""

    invocation = ToolInvocationRecord(
        tool_name="hybrid-search",
        input_json={"query": "奖学金"},
        output_json={
            "kind": "workspace_file_search",
            "ok": True,
            "query": "奖学金",
            "total_returned": 2,
            "partial": False,
            "user_message": "",
            "results": [
                {
                    "working_copy_id": "wc-1",
                    "document_id": "d-1",
                    "document_version_id": "v-1",
                    "filename": "奖学金.docx",
                    "category_path": ["奖助学金"],
                    "year": 2025,
                    "overview": "奖学金材料",
                    "match_reasons": ["文件名命中"],
                    "match_location": {"page_number": 2},
                    "evidence_preview": "...",
                    "_score": 0.9,  # 应被过滤掉
                    "_hit_source": "exact_filename",  # 应被过滤掉
                },
            ],
        },
        status="COMPLETED",
    )
    receipt = build_user_task_receipt(_make_result(tool_invocations=[invocation]))

    assert receipt.file_search_result is not None
    assert receipt.response_type == "file_search_results"
    assert receipt.file_search_result["query"] == "奖学金"
    assert receipt.file_search_result["total_returned"] == 2

    file_item = receipt.file_search_result["files"][0]
    assert file_item["filename"] == "奖学金.docx"
    # 内部 _score 等字段应被过滤
    assert "_score" not in file_item
    assert "_hit_source" not in file_item


def test_new_search_result_handles_empty_results():
    """新链路返回空结果时也正确投影。"""

    invocation = ToolInvocationRecord(
        tool_name="hybrid-search",
        input_json={"query": "不存在"},
        output_json={
            "kind": "workspace_file_search",
            "ok": True,
            "query": "不存在",
            "total_returned": 0,
            "partial": False,
            "user_message": "未找到相关文件",
            "results": [],
        },
        status="COMPLETED",
    )
    receipt = build_user_task_receipt(_make_result(tool_invocations=[invocation]))

    assert receipt.file_search_result is not None
    assert receipt.file_search_result["total_returned"] == 0
    assert receipt.file_search_result["files"] == []
    assert receipt.response_type == "file_search_results"


def test_non_search_tool_not_activating_search_field():
    """非 hybrid-search tool 不应激活 file_search_result。"""

    invocation = ToolInvocationRecord(
        tool_name="managed-file-list",
        input_json={"root_key": "r1"},
        output_json={
            "kind": "managed_file_list",
            "ok": True,
            "files": [],
        },
        status="COMPLETED",
    )
    receipt = build_user_task_receipt(_make_result(tool_invocations=[invocation]))

    assert receipt.file_search_result is None