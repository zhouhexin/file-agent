"""根据预置 taxonomy 和受管目录快照生成统一分类体系。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.modules.classification.loader import load_taxonomy
from app.modules.classification.unified_builder import build_unified_taxonomy


def main() -> None:
    """读取构建参数并写入经过 schema 校验的 JSON。"""

    parser = argparse.ArgumentParser(description="Build unified File Agent taxonomy.")
    parser.add_argument("--base", required=True, help="预置 taxonomy JSON 路径。")
    parser.add_argument(
        "--inventory",
        required=True,
        action="append",
        help="受管目录快照 JSON 路径；可重复传入并按顺序增量合并。",
    )
    parser.add_argument("--output", required=True, help="统一 taxonomy 输出路径。")
    parser.add_argument("--version", required=True, help="新 taxonomy 版本。")
    args = parser.parse_args()

    base_path = Path(args.base)
    # 构建输入先经过同一 loader，确保 v9 动态物化的历史 fallback 也进入冻结兼容快照。
    payload = load_taxonomy(base_path).model_dump(mode="json", exclude_none=True)
    for inventory_value in args.inventory:
        inventory_path = Path(inventory_value)
        payload = build_unified_taxonomy(
            base_payload=payload,
            inventory_payload=json.loads(inventory_path.read_text(encoding="utf-8")),
            version=args.version,
        )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
