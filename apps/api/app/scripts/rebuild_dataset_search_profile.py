"""为一份已发布的累计 Excel 补建当前工作副本搜索投影。"""

from __future__ import annotations

import argparse

from app.core.database import SessionLocal
from app.db.models import StructuredDataset, StructuredDatasetPublication, WorkingCopy
from app.modules.retrieval.search_profile import DocumentSearchProfileService


def rebuild_profile(db, *, working_copy_id: str, expected_version_id: str) -> dict[str, str]:
    """只修复已完成发布的指定当前版本，不改动 Excel 内容或历史版本。"""

    working = db.get(WorkingCopy, working_copy_id)
    if working is None or working.status != "ACTIVE":
        raise ValueError("目标不是活动工作副本")
    if working.current_version_id != expected_version_id:
        raise ValueError("目标当前版本已变化，拒绝按旧版本补建索引")
    dataset = db.query(StructuredDataset).filter_by(working_copy_id=working.id).one_or_none()
    if dataset is None or dataset.target_filename != working.filename:
        raise ValueError("目标不是同名累计 Excel")
    publication = db.query(StructuredDatasetPublication).filter_by(
        working_copy_id=working.id,
        candidate_document_version_id=expected_version_id,
        status="COMPLETED",
    ).first()
    if publication is None:
        raise ValueError("目标当前版本尚未完成发布")
    result = DocumentSearchProfileService(db=db).upsert_current_profile(working.id)
    if not result.get("ok"):
        raise RuntimeError("搜索投影补建失败")
    db.commit()
    return {
        "filename": working.filename,
        "working_copy_id": working.id,
        "document_version_id": expected_version_id,
        "profile_id": str(result["profile_id"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--working-copy-id", required=True)
    parser.add_argument("--expected-version-id", required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        result = rebuild_profile(
            db,
            working_copy_id=args.working_copy_id,
            expected_version_id=args.expected_version_id,
        )
    print(result)


if __name__ == "__main__":
    main()
