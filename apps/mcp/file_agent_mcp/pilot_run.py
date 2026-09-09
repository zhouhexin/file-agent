"""执行 WorkBuddy 本地目录试点导入。

该命令只接受已配置逻辑根及相对目录，访问令牌继续从环境变量读取，避免把密钥写入参数、
输出或项目文件。它复用正式批量传输服务，不提供绝对路径或绕过清单冻结的快捷入口。
"""

from __future__ import annotations

import argparse
import asyncio
import json

from .client import FileAgentIntegrationClient, LocalRootRegistry
from .transfer import BatchTransferService, TransferStateStore


def build_pilot_run_summary(result: dict) -> dict:
    """把正式传输结果投影为不含令牌、路径和文件正文的命令行摘要。"""

    batch = result.get("batch") if isinstance(result.get("batch"), dict) else {}
    counts = batch.get("counts") if isinstance(batch.get("counts"), dict) else {}
    return {
        "batch_id": batch.get("id"),
        "uploaded_item_count": int(result.get("uploaded_count") or 0),
        "pending_duplicate_review_count": int(
            counts.get("waiting_duplicate_confirmation") or 0
        ),
        "transfer_failed_count": int(result.get("transfer_failed_count") or 0),
    }


async def _run(args: argparse.Namespace) -> int:
    """调用正式 MCP 传输服务，并仅输出不含令牌和绝对路径的批次摘要。"""

    roots = LocalRootRegistry.from_environment()
    client = FileAgentIntegrationClient.from_environment(roots=roots)
    try:
        result = await BatchTransferService(
            client,
            TransferStateStore.from_environment(),
        ).ingest_directory(
            source_root_ref=args.source_root_ref,
            relative_directory=args.relative_directory,
            recursive=args.recursive,
            user_request=args.user_request,
            placement_mode=args.placement_mode,
            rule_profile=args.rule_profile,
        )
    finally:
        await client.close()
    # BatchTransferService 把后端批次快照放在 ``batch`` 字段中；试点命令只做
    # 脱敏投影，不能误读顶层并把已经创建成功的批次显示成 null/0。
    print(
        json.dumps(
            build_pilot_run_summary(result),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> None:
    """解析受控试点参数并执行一次完整目录导入。"""

    parser = argparse.ArgumentParser(description="执行 WorkBuddy 本地目录试点导入")
    parser.add_argument("--source-root-ref", required=True)
    parser.add_argument("--relative-directory", required=True)
    parser.add_argument("--no-recursive", action="store_false", dest="recursive")
    parser.add_argument("--user-request")
    parser.add_argument(
        "--placement-mode",
        choices=("BY_CATEGORY", "NEUTRAL"),
        default="BY_CATEGORY",
    )
    parser.add_argument(
        "--rule-profile",
        choices=("content_based", "legacy_school_materials"),
        default="content_based",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
