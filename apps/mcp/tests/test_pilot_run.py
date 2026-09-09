"""WorkBuddy 试点执行命令的脱敏回执测试。"""

from file_agent_mcp.pilot_run import build_pilot_run_summary


def test_pilot_run_summary_reads_nested_batch_and_transfer_counts() -> None:
    """真实传输成功后必须显示批次 ID 与上传数，不能因误读层级返回 null/0。"""

    summary = build_pilot_run_summary(
        {
            "batch": {
                "id": "batch-1",
                "counts": {"waiting_duplicate_confirmation": 2},
            },
            "uploaded_count": 5,
            "transfer_failed_count": 1,
        }
    )

    assert summary == {
        "batch_id": "batch-1",
        "uploaded_item_count": 5,
        "pending_duplicate_review_count": 2,
        "transfer_failed_count": 1,
    }
