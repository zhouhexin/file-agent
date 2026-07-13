"""重命名策略配置加载器。"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from app.modules.file_rename.schemas import RenamePolicy


def default_policy_path() -> Path:
    """返回项目内默认策略文件路径。"""

    configured = os.getenv("FILE_RENAME_POLICY_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[5] / "rules" / "file_rename_policy.json"


@lru_cache(maxsize=8)
def load_rename_policy(path: str | None = None) -> RenamePolicy:
    """读取并严格校验重命名策略。"""

    policy_path = Path(path).expanduser().resolve() if path else default_policy_path()
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    return RenamePolicy.model_validate(payload)

