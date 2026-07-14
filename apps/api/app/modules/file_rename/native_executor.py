"""确认后执行受管文件原地重命名。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from app.modules.file_rename.batch_validator import validate_rename_batch, validate_rename_mapping
from app.modules.file_rename.executor_protocol import (
    RenameExecutorError,
    RenameExecutorHealth,
)
from app.modules.file_rename.schemas import (
    RenameBatchRequest,
    RenameBatchResult,
    RenameExecutionItem,
)


class NativeRenameError(RenameExecutorError):
    """Native 重命名校验或执行失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class NativeRenameExecutor:
    """使用 Python 文件系统 API 执行同目录 basename 重命名。"""

    name = "native"

    def health_check(self) -> RenameExecutorHealth:
        """Native 执行器依赖 Python 标准库，始终可用。"""

        return RenameExecutorHealth(executor=self.name, available=True, version="builtin")

    def preview(
        self,
        *,
        root_path: Path,
        before_relative_path: str,
        after_relative_path: str,
    ) -> tuple[Path, Path]:
        """校验源和目标路径，不修改文件。"""

        try:
            paths = validate_rename_mapping(
                root_path=root_path,
                before_relative_path=before_relative_path,
                after_relative_path=after_relative_path,
            )
        except RenameExecutorError as exc:
            raise NativeRenameError(exc.code, str(exc)) from exc
        return paths.source_path, paths.target_path

    def execute(
        self,
        *,
        root_path: Path,
        before_relative_path: str,
        after_relative_path: str,
    ) -> tuple[Path, Path]:
        """校验后执行原子同目录 rename。"""

        source_path, target_path = self.preview(
            root_path=root_path,
            before_relative_path=before_relative_path,
            after_relative_path=after_relative_path,
        )
        if source_path == target_path:
            return source_path, target_path
        source_path.rename(target_path)
        return source_path, target_path

    def compensate(self, *, source_path: Path, target_path: Path) -> None:
        """数据库写入失败时尽力恢复原文件名。"""

        if target_path.exists() and not source_path.exists():
            target_path.rename(source_path)

    def preview_batch(self, request: RenameBatchRequest) -> RenameBatchResult:
        """一次性验证完整批次，不修改文件。"""

        started_at = time.monotonic()
        try:
            validate_rename_batch(request)
            items = [
                RenameExecutionItem(
                    managed_file_id=item.managed_file_id,
                    before_relative_path=item.before_relative_path,
                    after_relative_path=item.after_relative_path,
                    status="READY",
                )
                for item in request.items
            ]
            return _build_batch_result(
                status="PREVIEWED",
                request=request,
                items=items,
                started_at=started_at,
            )
        except RenameExecutorError as exc:
            return _failed_batch_result(request=request, error=exc, started_at=started_at)

    def execute_batch(self, request: RenameBatchRequest) -> RenameBatchResult:
        """先校验完整批次，再逐项执行并隔离失败。"""

        started_at = time.monotonic()
        try:
            validate_rename_batch(request)
        except RenameExecutorError as exc:
            return _failed_batch_result(request=request, error=exc, started_at=started_at)

        results: list[RenameExecutionItem] = []
        for item in request.items:
            try:
                self.execute(
                    root_path=request.root_path,
                    before_relative_path=item.before_relative_path,
                    after_relative_path=item.after_relative_path,
                )
                results.append(
                    RenameExecutionItem(
                        managed_file_id=item.managed_file_id,
                        before_relative_path=item.before_relative_path,
                        after_relative_path=item.after_relative_path,
                        status="COMPLETED",
                    )
                )
            except RenameExecutorError as exc:
                results.append(
                    RenameExecutionItem(
                        managed_file_id=item.managed_file_id,
                        before_relative_path=item.before_relative_path,
                        after_relative_path=item.after_relative_path,
                        status="FAILED",
                        error_code=exc.code,
                        error_message=str(exc),
                    )
                )
        completed_count = sum(item.status == "COMPLETED" for item in results)
        status = "EXECUTED" if completed_count == len(results) else "FAILED" if completed_count == 0 else "PARTIAL"
        return _build_batch_result(
            status=status,
            request=request,
            items=results,
            started_at=started_at,
        )

    def compensate_batch(
        self,
        request: RenameBatchRequest,
        result: RenameBatchResult,
    ) -> RenameBatchResult:
        """逆序恢复已经完成的项目。"""

        started_at = time.monotonic()
        compensated: list[RenameExecutionItem] = []
        for item in reversed(result.items):
            if item.status != "COMPLETED":
                continue
            try:
                source_path = request.root_path / item.before_relative_path
                target_path = request.root_path / item.after_relative_path
                self.compensate(source_path=source_path, target_path=target_path)
                compensated.append(item.model_copy(update={"status": "COMPENSATED"}))
            except OSError as exc:
                compensated.append(
                    item.model_copy(
                        update={
                            "status": "FAILED",
                            "error_code": "NATIVE_COMPENSATION_FAILED",
                            "error_message": str(exc),
                        }
                    )
                )
        failed_count = sum(item.status == "FAILED" for item in compensated)
        return _build_batch_result(
            status="COMPENSATED" if failed_count == 0 else "PARTIAL",
            request=request,
            items=compensated,
            started_at=started_at,
        )


def _preview_digest(request: RenameBatchRequest) -> str:
    """根据固定映射计算与执行器无关的预演摘要。"""

    payload = [
        {
            "managed_file_id": item.managed_file_id,
            "before": item.before_relative_path,
            "after": item.after_relative_path,
            "sha256": item.source_sha256,
        }
        for item in request.items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_batch_result(
    *,
    status: str,
    request: RenameBatchRequest,
    items: list[RenameExecutionItem],
    started_at: float,
) -> RenameBatchResult:
    """构造统一 Native 批次结果。"""

    completed_count = sum(item.status in {"COMPLETED", "COMPENSATED"} for item in items)
    failed_count = sum(item.status == "FAILED" for item in items)
    return RenameBatchResult(
        executor="native",
        executor_version="builtin",
        preview_digest=_preview_digest(request),
        status=status,
        matched_count=len(items),
        completed_count=completed_count,
        failed_count=failed_count,
        duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        items=items,
    )


def _failed_batch_result(
    *,
    request: RenameBatchRequest,
    error: RenameExecutorError,
    started_at: float,
) -> RenameBatchResult:
    """把批次级校验失败映射到每个项目。"""

    return _build_batch_result(
        status="FAILED",
        request=request,
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
        started_at=started_at,
    )
