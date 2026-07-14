"""F2 批量重命名受控适配器。"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from app.modules.file_rename.batch_validator import validate_rename_batch
from app.modules.file_rename.executor_protocol import (
    RenameExecutorError,
    RenameExecutorHealth,
)
from app.modules.file_rename.f2_report_parser import F2ReportItem, parse_f2_report
from app.modules.file_rename.native_executor import NativeRenameExecutor
from app.modules.file_rename.schemas import (
    RenameBatchItem,
    RenameBatchRequest,
    RenameBatchResult,
    RenameExecutionItem,
)


RunProcess = Callable[..., subprocess.CompletedProcess[str]]


class F2RenameExecutor:
    """通过私有 CSV 和严格结果校验调用固定版本 F2。"""

    name = "f2"

    def __init__(
        self,
        *,
        binary_path: str = "f2",
        expected_version: str = "2.2.2",
        stdout_max_bytes: int = 1024 * 1024,
        runner: RunProcess = subprocess.run,
    ) -> None:
        self.binary_path = binary_path
        self.expected_version = expected_version
        self.stdout_max_bytes = stdout_max_bytes
        self.runner = runner

    def health_check(self) -> RenameExecutorHealth:
        """验证 F2 二进制存在且版本与固定版本一致。"""

        try:
            completed = self.runner(
                [self.binary_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                shell=False,
            )
        except FileNotFoundError:
            return RenameExecutorHealth(
                executor=self.name,
                available=False,
                error_code="F2_NOT_AVAILABLE",
                message="F2 二进制不可用。",
            )
        except subprocess.TimeoutExpired:
            return RenameExecutorHealth(
                executor=self.name,
                available=False,
                error_code="F2_TIMEOUT",
                message="F2 版本检查超时。",
            )
        version = _extract_version(f"{completed.stdout}\n{completed.stderr}")
        if completed.returncode != 0 or version != self.expected_version:
            return RenameExecutorHealth(
                executor=self.name,
                available=False,
                version=version,
                error_code="F2_VERSION_MISMATCH",
                message="F2 版本与项目固定版本不一致。",
            )
        return RenameExecutorHealth(executor=self.name, available=True, version=version)

    def preview_batch(self, request: RenameBatchRequest) -> RenameBatchResult:
        """调用 F2 dry-run 并严格比对完整映射。"""

        return self._run_batch(request=request, execute=False)

    def execute_batch(self, request: RenameBatchRequest) -> RenameBatchResult:
        """重新 dry-run 后调用 F2 -x，并校验文件系统结果。"""

        preview = self.preview_batch(request)
        if preview.status != "PREVIEWED":
            return preview
        result = self._run_batch(request=request, execute=True)
        if result.status != "EXECUTED":
            return result
        try:
            self._require_postcheck(request=request)
            return result
        except RenameExecutorError as exc:
            compensation = self.compensate_batch(request, result)
            if compensation.status != "COMPENSATED":
                exc = RenameExecutorError("F2_COMPENSATION_FAILED", "F2 执行后校验失败且无法完整恢复原文件名。")
            return _failed_result(request=request, error=exc, started_at=time.monotonic())

    def compensate_batch(
        self,
        request: RenameBatchRequest,
        result: RenameBatchResult,
    ) -> RenameBatchResult:
        """不使用 F2 全局 undo，改用受控 Native 逆向批次。"""

        completed_ids = {item.managed_file_id for item in result.items if item.status == "COMPLETED"}
        if not completed_ids:
            return RenameBatchResult(
                executor=self.name,
                executor_version=self.expected_version,
                preview_digest=result.preview_digest,
                status="COMPENSATED",
                matched_count=0,
                completed_count=0,
                failed_count=0,
                items=[],
            )
        reverse_request = RenameBatchRequest(
            root_path=request.root_path,
            operation_plan_id=request.operation_plan_id,
            timeout_seconds=request.timeout_seconds,
            items=[
                RenameBatchItem(
                    managed_file_id=item.managed_file_id,
                    before_relative_path=item.after_relative_path,
                    after_relative_path=item.before_relative_path,
                    source_sha256=item.source_sha256,
                )
                for item in reversed(request.items)
                if item.managed_file_id in completed_ids
            ],
        )
        reverse_result = NativeRenameExecutor().execute_batch(reverse_request)
        reverse_by_id = {item.managed_file_id: item for item in reverse_result.items}
        request_by_id = {item.managed_file_id: item for item in request.items}
        items = [
            RenameExecutionItem(
                managed_file_id=item.managed_file_id,
                before_relative_path=item.before_relative_path,
                after_relative_path=item.after_relative_path,
                status=(
                    "COMPENSATED"
                    if _compensation_is_valid(
                        root_path=request.root_path,
                        original=request_by_id[item.managed_file_id],
                        reverse_result=reverse_by_id.get(item.managed_file_id),
                    )
                    else "FAILED"
                ),
                error_code=(
                    None
                    if _compensation_is_valid(
                        root_path=request.root_path,
                        original=request_by_id[item.managed_file_id],
                        reverse_result=reverse_by_id.get(item.managed_file_id),
                    )
                    else "F2_COMPENSATION_FAILED"
                ),
                error_message=(
                    None
                    if _compensation_is_valid(
                        root_path=request.root_path,
                        original=request_by_id[item.managed_file_id],
                        reverse_result=reverse_by_id.get(item.managed_file_id),
                    )
                    else "无法恢复原文件名。"
                ),
            )
            for item in result.items
            if item.managed_file_id in completed_ids
        ]
        failed_count = sum(item.status == "FAILED" for item in items)
        return RenameBatchResult(
            executor=self.name,
            executor_version=self.expected_version,
            preview_digest=result.preview_digest,
            status="COMPENSATED" if failed_count == 0 else "PARTIAL",
            matched_count=len(items),
            completed_count=len(items) - failed_count,
            failed_count=failed_count,
            items=items,
        )

    def _run_batch(self, *, request: RenameBatchRequest, execute: bool) -> RenameBatchResult:
        """生成临时 CSV，调用 F2，并把结果映射到统一契约。"""

        started_at = time.monotonic()
        try:
            validate_rename_batch(request)
            self._require_health()
            with tempfile.TemporaryDirectory(prefix="file-agent-f2-") as temp_dir_value:
                temp_dir = Path(temp_dir_value)
                csv_path = temp_dir / "rename-plan.csv"
                _write_f2_csv(csv_path=csv_path, request=request)
                command = [self.binary_path, "--csv", str(csv_path), "--json", "--no-color"]
                if execute:
                    command.append("-x")
                completed = self._invoke(
                    command=command,
                    cwd=request.root_path,
                    temp_dir=temp_dir,
                    timeout_seconds=request.timeout_seconds,
                )
            report = parse_f2_report(completed.stdout, root_path=request.root_path)
            _assert_report_matches(request=request, report=report)
            items = [
                RenameExecutionItem(
                    managed_file_id=item.managed_file_id,
                    before_relative_path=item.before_relative_path,
                    after_relative_path=item.after_relative_path,
                    status="COMPLETED" if execute else "READY",
                )
                for item in request.items
            ]
            return _result(
                request=request,
                status="EXECUTED" if execute else "PREVIEWED",
                items=items,
                version=self.expected_version,
                started_at=started_at,
            )
        except RenameExecutorError as exc:
            return _failed_result(request=request, error=exc, started_at=started_at)

    def _invoke(
        self,
        *,
        command: list[str],
        cwd: Path,
        temp_dir: Path,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        """使用参数数组、受控环境和输出上限调用 F2。"""

        environment = {
            "HOME": str(temp_dir),
            "XDG_DATA_HOME": str(temp_dir / "xdg"),
            "NO_COLOR": "1",
            "F2_NO_COLOR": "1",
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        try:
            completed = self.runner(
                command,
                cwd=str(cwd),
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise RenameExecutorError("F2_NOT_AVAILABLE", "F2 二进制不可用。") from exc
        except subprocess.TimeoutExpired as exc:
            raise RenameExecutorError("F2_TIMEOUT", "F2 批量重命名超时。") from exc
        if len(completed.stdout.encode("utf-8")) > self.stdout_max_bytes:
            raise RenameExecutorError("F2_EXECUTION_FAILED", "F2 标准输出超过安全上限。")
        if len(completed.stderr.encode("utf-8")) > self.stdout_max_bytes:
            raise RenameExecutorError("F2_EXECUTION_FAILED", "F2 错误输出超过安全上限。")
        if completed.returncode != 0:
            raise RenameExecutorError("F2_EXECUTION_FAILED", "F2 返回非零退出码。")
        return completed

    def _require_health(self) -> None:
        """显式启用 F2 时健康检查失败必须关闭执行。"""

        health = self.health_check()
        if not health.available:
            raise RenameExecutorError(health.error_code or "F2_NOT_AVAILABLE", health.message)

    def _require_postcheck(self, *, request: RenameBatchRequest) -> None:
        """执行后校验目标存在、源消失且内容哈希未变。"""

        for item in request.items:
            source = request.root_path / item.before_relative_path
            target = request.root_path / item.after_relative_path
            if source.exists() or not target.is_file():
                raise RenameExecutorError("F2_POSTCHECK_FAILED", "F2 执行后路径状态不一致。")
            if item.source_sha256 and _sha256_file(target) != item.source_sha256:
                raise RenameExecutorError("F2_POSTCHECK_FAILED", "F2 执行后文件内容发生变化。")


def _write_f2_csv(*, csv_path: Path, request: RenameBatchRequest) -> None:
    """按 F2 官方两列格式写入 UTF-8 CSV，不写表头。"""

    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        for item in request.items:
            source_path = (request.root_path / item.before_relative_path).resolve(strict=True)
            writer.writerow([str(source_path), Path(item.after_relative_path).name])


def _assert_report_matches(*, request: RenameBatchRequest, report: list[F2ReportItem]) -> None:
    """F2 输出必须与 OperationPlan 映射逐项完全一致。"""

    expected = {
        (item.before_relative_path, item.after_relative_path)
        for item in request.items
    }
    actual = {
        (item.before_relative_path, item.after_relative_path)
        for item in report
        if item.status in {"ok", "success", "ready", "renamed", "completed"}
    }
    if len(report) != len(request.items) or actual != expected:
        raise RenameExecutorError("F2_PREVIEW_MISMATCH", "F2 预演结果与 OperationPlan 不一致。")


def _extract_version(value: str) -> str:
    """从 F2 版本输出中提取语义版本。"""

    match = re.search(r"(?:^|\s)v?(\d+\.\d+\.\d+)(?:\s|$)", value)
    return match.group(1) if match else ""


def _preview_digest(request: RenameBatchRequest) -> str:
    """根据固定映射生成稳定预演摘要。"""

    lines = [
        "\0".join(
            [
                item.managed_file_id,
                item.before_relative_path,
                item.after_relative_path,
                item.source_sha256,
            ]
        )
        for item in request.items
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _result(
    *,
    request: RenameBatchRequest,
    status: str,
    items: list[RenameExecutionItem],
    version: str,
    started_at: float,
) -> RenameBatchResult:
    """构造统一 F2 批次结果。"""

    completed_count = sum(item.status == "COMPLETED" for item in items)
    failed_count = sum(item.status == "FAILED" for item in items)
    return RenameBatchResult(
        executor="f2",
        executor_version=version,
        preview_digest=_preview_digest(request),
        status=status,
        matched_count=len(items),
        completed_count=completed_count,
        failed_count=failed_count,
        duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        items=items,
    )


def _failed_result(
    *,
    request: RenameBatchRequest,
    error: RenameExecutorError,
    started_at: float,
) -> RenameBatchResult:
    """把 F2 批次错误映射为每个文件的结构化失败。"""

    return _result(
        request=request,
        status="FAILED",
        version="",
        started_at=started_at,
        items=[
            RenameExecutionItem(
                managed_file_id=item.managed_file_id,
                before_relative_path=item.before_relative_path,
                after_relative_path=item.after_relative_path,
                status="FAILED",
                error_code=error.code,
                error_message=str(error),
            )
            for item in request.items
        ],
    )


def _sha256_file(path: Path) -> str:
    """流式计算文件哈希，供 F2 执行后校验。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compensation_is_valid(
    *,
    root_path: Path,
    original: RenameBatchItem,
    reverse_result: RenameExecutionItem | None,
) -> bool:
    """确认逆向改名成功，且有基准哈希时内容仍与计划一致。"""

    if reverse_result is None or reverse_result.status != "COMPLETED":
        return False
    restored_path = root_path / original.before_relative_path
    if not restored_path.is_file():
        return False
    return not original.source_sha256 or _sha256_file(restored_path) == original.source_sha256
