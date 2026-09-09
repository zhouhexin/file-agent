"""首批 WorkBuddy 本地导入的只读验收报告工具。

工具复用导入时保存的不可变清单，对比当前源文件哈希和后端持久化批次快照。它不会创建、重试、
取消或确认任何任务，因此不能代替用户明确调用 ``file_batch_ingest``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from .client import FileAgentIntegrationClient, LocalRootRegistry, file_sha256
from .transfer import TransferStateStore


_FINAL_OR_USER_WAIT = {
    "WAITING_DUPLICATE_CONFIRMATION",
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
    "SKIPPED",
}


def build_pilot_report(
    *,
    batch_id: str,
    local_state: dict[str, Any],
    snapshot: dict[str, Any],
    roots: LocalRootRegistry,
    min_files: int,
    max_files: int,
) -> dict[str, Any]:
    """核对源文件不变性、固定成员、终态回执及逐文件最终映射。"""

    saved_items = list(local_state.get("items") or [])
    if not min_files <= len(saved_items) <= max_files:
        raise RuntimeError(
            f"试点清单应包含 {min_files}–{max_files} 个文件，当前为 {len(saved_items)} 个"
        )
    source_root_ref = str(local_state.get("source_root_ref") or "")
    changed_sources: list[str] = []
    for item in saved_items:
        relative_path = str(item.get("source_relative_path") or "")
        path = roots.resolve_file(
            source_root_ref=source_root_ref,
            source_relative_path=relative_path,
        )
        stat = path.stat()
        if (
            stat.st_size != int(item.get("size_bytes") or 0)
            or stat.st_mtime_ns != int(item.get("mtime_ns") or 0)
            or file_sha256(path) != str(item.get("expected_sha256") or "")
        ):
            changed_sources.append(relative_path)

    batch = dict(snapshot.get("batch") or {})
    server_items = list(snapshot.get("items") or [])
    saved_by_client_id = {str(item["client_item_id"]): item for item in saved_items}
    server_by_client_id = {str(item["client_item_id"]): item for item in server_items}
    missing_server_items = sorted(set(saved_by_client_id) - set(server_by_client_id))
    unexpected_server_items = sorted(set(server_by_client_id) - set(saved_by_client_id))
    source_mapping_mismatches = sorted(
        client_id
        for client_id in set(saved_by_client_id) & set(server_by_client_id)
        if (
            saved_by_client_id[client_id]["source_relative_path"]
            != server_by_client_id[client_id]["source_relative_path"]
            or saved_by_client_id[client_id]["expected_sha256"]
            != server_by_client_id[client_id]["expected_sha256"]
        )
    )
    unfinished = [
        str(item["client_item_id"])
        for item in server_items
        if str(item.get("status") or "") not in _FINAL_OR_USER_WAIT
    ]
    missing_final_mapping = [
        str(item["client_item_id"])
        for item in server_items
        if item.get("status") in {"SUCCEEDED", "PARTIAL"}
        and (
            not item.get("final_document_id")
            or not item.get("final_version_id")
            or not item.get("final_working_copy_id")
        )
    ]
    incomplete_organization = [
        str(item["client_item_id"])
        for item in server_items
        if item.get("status") in {"SUCCEEDED", "PARTIAL"}
        and (
            item.get("organization_status") != "COMPLETED"
            or item.get("index_status") != "COMPLETED"
        )
    ]
    missing_naming_receipt = [
        str(item["client_item_id"])
        for item in server_items
        if item.get("status") in {"SUCCEEDED", "PARTIAL"}
        and item.get("decision") not in {"USE_EXISTING_FILE", "WAIT_AND_REUSE"}
        and str((item.get("result") or {}).get("rename_status") or "")
        not in {"COMPLETED", "NO_CHANGE"}
    ]
    waiting_ids = {
        str(item["id"])
        for item in server_items
        if item.get("status") == "WAITING_DUPLICATE_CONFIRMATION"
    }
    restored_review_ids = {
        str(review.get("item_id") or "")
        for review in list(snapshot.get("pending_duplicate_reviews") or [])
    }
    missing_reviews = sorted(waiting_ids - restored_review_ids)
    errors = {
        "changed_sources": changed_sources,
        "missing_server_items": missing_server_items,
        "unexpected_server_items": unexpected_server_items,
        "source_mapping_mismatches": source_mapping_mismatches,
        "unfinished_items": unfinished,
        "missing_final_mapping": missing_final_mapping,
        "incomplete_organization": incomplete_organization,
        "missing_naming_receipt": missing_naming_receipt,
        "missing_duplicate_reviews": missing_reviews,
    }
    ok = (
        not any(errors.values())
        and batch.get("display_status") == "FILE_PROCESSING_COMPLETED"
        and bool(batch.get("final_receipt_ready"))
    )
    return {
        "ok": ok,
        "batch_id": batch_id,
        "manifest_file_count": len(saved_items),
        "server_item_count": len(server_items),
        "batch_status": batch.get("status"),
        "display_status": batch.get("display_status"),
        "result_revision": batch.get("result_revision"),
        "counts": batch.get("counts") or {},
        "receipt": batch.get("receipt") or {},
        "errors": errors,
    }


async def _run(args: argparse.Namespace) -> int:
    """读取本地清单与后端事实，输出不含令牌和绝对路径的 JSON 报告。"""

    roots = LocalRootRegistry.from_environment()
    state = TransferStateStore.from_environment().load(batch_id=args.batch_id)
    if not state:
        raise RuntimeError("本机没有该批次的冻结清单")
    client = FileAgentIntegrationClient.from_environment(roots=roots)
    try:
        snapshot = await client.batch_snapshot(batch_id=args.batch_id)
    finally:
        await client.close()
    report = build_pilot_report(
        batch_id=args.batch_id,
        local_state=state,
        snapshot=snapshot,
        roots=roots,
        min_files=args.min_files,
        max_files=args.max_files,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def main() -> None:
    """命令行入口要求明确批次 ID，并以退出码表达验收结果。"""

    parser = argparse.ArgumentParser(description="生成 WorkBuddy 首批材料只读验收报告")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--min-files", type=int, default=20)
    parser.add_argument("--max-files", type=int, default=30)
    args = parser.parse_args()
    if args.min_files < 1 or args.max_files < args.min_files:
        parser.error("文件数量范围无效")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
