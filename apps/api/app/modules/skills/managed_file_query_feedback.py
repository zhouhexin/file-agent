"""managed-file-query Skill 的反馈样本记录。

第一阶段只把用户纠错样本写入本地 JSONL，作为后续 Skill Candidate 和
回归测试集的输入；该模块不自动修改生产规则。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4


SKILL_ID = "managed-file-query"
ARTIFACT_KEY = "skill-artifacts/managed-file-query-feedback.jsonl"


def record_managed_file_query_feedback_sample(
    *,
    user_id: str | None,
    feedback_type: str,
    comment: str,
    context_json: Dict[str, Any] | None = None,
    storage_root: Path | None = None,
) -> Dict[str, Any]:
    """把 managed-file-query 解析纠错样本追加写入本地 JSONL。"""

    sample_id = str(uuid4())
    payload = {
        "sample_id": sample_id,
        "skill_id": SKILL_ID,
        "user_id": user_id,
        "feedback_type": feedback_type,
        "comment": comment,
        "context_json": context_json or {},
        "status": "OPEN",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _feedback_file_path(storage_root=storage_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    return {
        "sample_id": sample_id,
        "skill_id": SKILL_ID,
        "artifact_key": ARTIFACT_KEY,
        "status": "OPEN",
    }


def _feedback_file_path(*, storage_root: Path | None = None) -> Path:
    """返回反馈样本文件路径；Tool 输出不会暴露该绝对路径。"""

    root = storage_root or Path("storage")
    return root / "skill-artifacts" / "managed-file-query-feedback.jsonl"
