"""人工对账后补发一条累计 Excel；默认只读，--apply 才执行。"""

from __future__ import annotations

import argparse

from app.core.database import SessionLocal
from app.db.models import StructuredDatasetPublication
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.structured_datasets.publication import repair_needs_repair_publication


def main() -> int:
    parser = argparse.ArgumentParser(description="核对并补发指定累计 Excel 发布记录")
    parser.add_argument("--publication-id", required=True)
    parser.add_argument("--expected-visible-sha256", required=True)
    parser.add_argument("--apply", action="store_true", help="执行受控补发；不指定时只读对账")
    args = parser.parse_args()
    if len(args.expected_visible_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in args.expected_visible_sha256):
        parser.error("--expected-visible-sha256 必须是 64 位小写十六进制 SHA-256")
    with SessionLocal() as db:
        row = db.get(StructuredDatasetPublication, args.publication_id)
        if row is None:
            parser.error("发布记录不存在")
        storage = FileLifecycleStorageService()
        visible = storage.working_copy_path(row.visible_relative_path)
        candidate = storage.working_copy_path(row.candidate_storage_path)
        visible_sha256 = storage.sha256_file(visible) if visible.is_file() else None
        candidate_sha256 = storage.sha256_file(candidate) if candidate.is_file() else None
        print({"publication_id": row.id, "status": row.status,
               "expected_visible_sha256": args.expected_visible_sha256,
               "actual_visible_sha256": visible_sha256,
               "candidate_sha256": candidate_sha256,
               "recorded_candidate_sha256": row.candidate_sha256})
        if not args.apply:
            return 0
        try:
            repaired = repair_needs_repair_publication(
                db, row.id, expected_visible_sha256=args.expected_visible_sha256)
        except Exception:
            db.rollback()
            raise
        print({"publication_id": row.id, "published": repaired})
        return 0 if repaired else 1


if __name__ == "__main__":
    raise SystemExit(main())
