"""异步 Tool 回执聚合和 Agent 等待状态的回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

from app.modules.agent.graph import evidence_or_change, response


def test_structured_async_job_id_keeps_agent_waiting_instead_of_generic_fallback():
    """结构化抽取的 async_job_id 必须进入 State，不能提前返回“暂无业务结果”。"""

    tool_result = {
        "kind": "filesystem_job",
        "ok": True,
        "status": "WAITING_FOR_ASYNC_JOB",
        "async_job_id": "structured-job-1",
        "document_id": "document-1",
        "structured_extraction_run_id": "structured-run-1",
    }
    state = {
        "slots": {"document_ids": ["document-1"]},
        "tool_invocations": [],
        "tool_results": [tool_result],
    }

    aggregation = evidence_or_change(
        state,
        SimpleNamespace(context=SimpleNamespace(classification_service=None)),
    )
    result = response(
        {
            **state,
            **aggregation,
        },
        None,
    )

    assert aggregation["result_summary"]["filesystem_job"]["job_id"] == "structured-job-1"
    assert aggregation["result_summary"]["filesystem_job"]["async_job_id"] == "structured-job-1"
    assert aggregation["async_job_ids"] == ["structured-job-1"]
    assert result["status"] == "WAITING_FOR_ASYNC_JOB"
    assert result["final_response"] is None
