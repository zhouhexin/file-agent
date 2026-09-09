"""WorkBuddy 首批材料只读验收报告测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from file_agent_mcp.client import LocalRootRegistry
from file_agent_mcp.pilot_validation import build_pilot_report


def test_pilot_report_verifies_source_hash_final_mapping_and_restored_review(tmp_path: Path) -> None:
    """报告必须同时证明源文件未变、成功项有最终ID且等待项可恢复确认。"""

    root = tmp_path / "root"
    source = root / "pilot" / "a.txt"
    source.parent.mkdir(parents=True)
    source.write_text("pilot source", encoding="utf-8")
    stat = source.stat()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    local_item = {
        "client_item_id": "local-1",
        "source_relative_path": "pilot/a.txt",
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "expected_sha256": digest,
    }
    snapshot = {
        "batch": {
            "status": "SUCCEEDED",
            "display_status": "FILE_PROCESSING_COMPLETED",
            "final_receipt_ready": True,
            "result_revision": 4,
            "counts": {"succeeded": 1},
            "receipt": {"retained_file_count": 1},
        },
        "items": [{
            **local_item,
            "id": "item-1",
            "status": "SUCCEEDED",
            "final_document_id": "document-1",
            "final_version_id": "version-1",
            "final_working_copy_id": "working-copy-1",
            "organization_status": "COMPLETED",
            "index_status": "COMPLETED",
            "decision": "CONTINUE_UPLOAD",
            "result": {"rename_status": "NO_CHANGE"},
        }],
        "pending_duplicate_reviews": [],
    }
    report = build_pilot_report(
        batch_id="batch-1",
        local_state={"source_root_ref": "materials", "items": [local_item]},
        snapshot=snapshot,
        roots=LocalRootRegistry({"materials": root}),
        min_files=1,
        max_files=2,
    )
    assert report["ok"] is True
    assert not any(report["errors"].values())


def test_pilot_report_fails_when_source_changes_or_waiting_review_is_missing(tmp_path: Path) -> None:
    """来源变化或刷新后缺少确认卡时必须令试点失败，不能仅看批次标题。"""

    root = tmp_path / "root"
    source = root / "pilot" / "a.txt"
    source.parent.mkdir(parents=True)
    source.write_text("changed", encoding="utf-8")
    local_item = {
        "client_item_id": "local-1",
        "source_relative_path": "pilot/a.txt",
        "size_bytes": 3,
        "mtime_ns": 1,
        "expected_sha256": "0" * 64,
    }
    report = build_pilot_report(
        batch_id="batch-1",
        local_state={"source_root_ref": "materials", "items": [local_item]},
        snapshot={
            "batch": {
                "status": "PARTIAL",
                "display_status": "FILE_PROCESSING_COMPLETED",
                "final_receipt_ready": True,
            },
            "items": [{
                **local_item,
                "id": "item-1",
                "status": "WAITING_DUPLICATE_CONFIRMATION",
                "final_document_id": None,
                "final_version_id": None,
                "final_working_copy_id": None,
                "organization_status": "PENDING",
                "index_status": "PENDING",
                "decision": None,
                "result": {},
            }],
            "pending_duplicate_reviews": [],
        },
        roots=LocalRootRegistry({"materials": root}),
        min_files=1,
        max_files=2,
    )
    assert report["ok"] is False
    assert report["errors"]["changed_sources"] == ["pilot/a.txt"]
    assert report["errors"]["missing_duplicate_reviews"] == ["item-1"]
