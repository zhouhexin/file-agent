"""文件重命名执行器统一契约测试。"""

import csv
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.modules.file_rename.f2_executor import F2RenameExecutor
from app.modules.file_rename.f2_report_parser import parse_f2_report
from app.modules.file_rename.native_executor import NativeRenameExecutor
from app.modules.file_rename.schemas import RenameBatchItem, RenameBatchRequest


def _request(root_path: Path, *items: tuple[str, str, str]) -> RenameBatchRequest:
    """构造 Native 和 F2 共用的测试请求。"""

    return RenameBatchRequest(
        root_path=root_path,
        operation_plan_id="plan-1",
        items=[
            RenameBatchItem(
                managed_file_id=managed_file_id,
                before_relative_path=before,
                after_relative_path=after,
            )
            for managed_file_id, before, after in items
        ],
    )


def test_native_batch_preview_does_not_modify_files(tmp_path):
    """Native 批次预演应返回固定摘要且不修改源文件。"""

    source = tmp_path / "党办" / "旧名称.txt"
    source.parent.mkdir()
    source.write_text("测试", encoding="utf-8")
    request = _request(tmp_path, ("managed-1", "党办/旧名称.txt", "党办/2026_新名称.txt"))

    result = NativeRenameExecutor().preview_batch(request)

    assert result.status == "PREVIEWED"
    assert result.executor == "native"
    assert len(result.preview_digest) == 64
    assert result.items[0].status == "READY"
    assert source.exists()
    assert not (tmp_path / "党办" / "2026_新名称.txt").exists()


def test_native_batch_execute_and_compensate(tmp_path):
    """Native 批次执行后应可按统一契约逆序补偿。"""

    source = tmp_path / "旧名称.txt"
    target = tmp_path / "2026_新名称.txt"
    source.write_text("测试", encoding="utf-8")
    request = _request(tmp_path, ("managed-1", source.name, target.name))
    executor = NativeRenameExecutor()

    result = executor.execute_batch(request)

    assert result.status == "EXECUTED"
    assert result.completed_count == 1
    assert target.exists()
    compensation = executor.compensate_batch(request, result)
    assert compensation.status == "COMPENSATED"
    assert source.exists()
    assert not target.exists()


def test_native_batch_rejects_duplicate_targets_without_modification(tmp_path):
    """批次存在重复目标时必须整体拒绝，不能先改一部分文件。"""

    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    request = _request(
        tmp_path,
        ("managed-1", first.name, "same.txt"),
        ("managed-2", second.name, "same.txt"),
    )

    result = NativeRenameExecutor().execute_batch(request)

    assert result.status == "FAILED"
    assert {item.error_code for item in result.items} == {"DUPLICATE_TARGET"}
    assert first.exists()
    assert second.exists()


def test_native_batch_rejects_hidden_and_cross_directory_paths(tmp_path):
    """隐藏路径和跨目录移动必须由公共校验器拒绝。"""

    hidden = tmp_path / ".secret.txt"
    hidden.write_text("secret", encoding="utf-8")
    hidden_result = NativeRenameExecutor().preview_batch(
        _request(tmp_path, ("managed-1", hidden.name, ".renamed.txt"))
    )
    assert hidden_result.items[0].error_code == "HIDDEN_FILE_NOT_ALLOWED"

    source = tmp_path / "a" / "file.txt"
    source.parent.mkdir()
    source.write_text("data", encoding="utf-8")
    move_result = NativeRenameExecutor().preview_batch(
        _request(tmp_path, ("managed-2", "a/file.txt", "b/file.txt"))
    )
    assert move_result.items[0].error_code == "MOVE_NOT_ALLOWED"


def test_f2_report_parser_accepts_array_and_wrapper(tmp_path):
    """F2 JSON 数组和常见包装对象应归一化为相对路径。"""

    source = tmp_path / "党办" / "旧名称.txt"
    target = source.with_name("新名称.txt")
    source.parent.mkdir()
    source.write_text("data", encoding="utf-8")
    for payload in [
        [{"original": str(source), "renamed": str(target), "status": "ok"}],
        {"results": [{"input": str(source), "output": str(target), "status": "success"}]},
    ]:
        parsed = parse_f2_report(json.dumps(payload, ensure_ascii=False), root_path=tmp_path)
        assert parsed[0].before_relative_path == "党办/旧名称.txt"
        assert parsed[0].after_relative_path == "党办/新名称.txt"


def test_f2_preview_writes_escaped_csv_and_does_not_modify_file(tmp_path):
    """F2 dry-run 应正确处理中文、逗号和引号，并保持文件不变。"""

    source = tmp_path / '旧,名称"测试.txt'
    target = tmp_path / '2026_新,名称"测试.txt'
    source.write_text("data", encoding="utf-8")
    observed_rows: list[list[str]] = []

    def fake_runner(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="f2 v2.2.2\n", stderr="")
        csv_path = Path(command[command.index("--csv") + 1])
        with csv_path.open(encoding="utf-8", newline="") as file:
            observed_rows.extend(csv.reader(file))
        output = [
            {
                "original": observed_rows[0][0],
                "renamed": str(Path(observed_rows[0][0]).with_name(observed_rows[0][1])),
                "status": "ok",
            }
        ]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(output, ensure_ascii=False),
            stderr="",
        )

    result = F2RenameExecutor(runner=fake_runner).preview_batch(
        _request(tmp_path, ("managed-1", source.name, target.name))
    )

    assert result.status == "PREVIEWED"
    assert observed_rows == [[str(source), target.name]]
    assert source.exists()
    assert not target.exists()


def test_f2_execute_runs_dry_run_before_exec_and_postchecks_hash(tmp_path):
    """F2 执行必须先预演，再使用 -x，最后验证源和目标状态。"""

    source = tmp_path / "旧名称.txt"
    target = tmp_path / "新名称.txt"
    source.write_text("data", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="f2 v2.2.2\n", stderr="")
        csv_path = Path(command[command.index("--csv") + 1])
        with csv_path.open(encoding="utf-8", newline="") as file:
            source_value, target_name = next(csv.reader(file))
        source_path = Path(source_value)
        target_path = source_path.with_name(target_name)
        if "-x" in command:
            source_path.rename(target_path)
        payload = [{"original": str(source_path), "renamed": str(target_path), "status": "ok"}]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    request = _request(tmp_path, ("managed-1", source.name, target.name))
    result = F2RenameExecutor(runner=fake_runner).execute_batch(request)

    operation_calls = [command for command in calls if "--csv" in command]
    assert result.status == "EXECUTED"
    assert len(operation_calls) == 2
    assert "-x" not in operation_calls[0]
    assert "-x" in operation_calls[1]
    assert target.exists()


def test_f2_rejects_version_and_preview_mismatch(tmp_path):
    """版本不匹配或预演映射不一致时不得修改文件。"""

    source = tmp_path / "old.txt"
    source.write_text("data", encoding="utf-8")
    request = _request(tmp_path, ("managed-1", source.name, "new.txt"))

    def wrong_version_runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="f2 v2.1.2\n", stderr="")

    version_result = F2RenameExecutor(runner=wrong_version_runner).preview_batch(request)
    assert version_result.items[0].error_code == "F2_VERSION_MISMATCH"

    def mismatch_runner(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="f2 v2.2.2\n", stderr="")
        payload = [{"original": str(source), "renamed": str(tmp_path / "other.txt"), "status": "ok"}]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    mismatch_result = F2RenameExecutor(runner=mismatch_runner).preview_batch(request)
    assert mismatch_result.items[0].error_code == "F2_PREVIEW_MISMATCH"
    assert source.exists()


def test_f2_postcheck_failure_compensates_completed_rename(tmp_path):
    """F2 改名后内容校验失败时必须恢复原文件名。"""

    source = tmp_path / "old.txt"
    target = tmp_path / "new.txt"
    source.write_text("original", encoding="utf-8")

    def corrupting_runner(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="f2 v2.2.2\n", stderr="")
        csv_path = Path(command[command.index("--csv") + 1])
        with csv_path.open(encoding="utf-8", newline="") as file:
            source_value, target_name = next(csv.reader(file))
        source_path = Path(source_value)
        target_path = source_path.with_name(target_name)
        if "-x" in command:
            source_path.rename(target_path)
            target_path.write_text("corrupted", encoding="utf-8")
        payload = [{"original": str(source_path), "renamed": str(target_path), "status": "ok"}]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    request = RenameBatchRequest(
        root_path=tmp_path,
        operation_plan_id="plan-1",
        items=[
            RenameBatchItem(
                managed_file_id="managed-1",
                before_relative_path=source.name,
                after_relative_path=target.name,
                source_sha256="0" * 64,
            )
        ],
    )
    result = F2RenameExecutor(runner=corrupting_runner).execute_batch(request)

    assert result.items[0].error_code == "F2_COMPENSATION_FAILED"
    assert source.exists()
    assert not target.exists()


def test_f2_timeout_and_invalid_json_are_structured_failures(tmp_path):
    """F2 超时和非 JSON 输出必须转换为稳定错误码。"""

    source = tmp_path / "old.txt"
    source.write_text("data", encoding="utf-8")
    request = _request(tmp_path, ("managed-1", source.name, "new.txt"))

    def timeout_runner(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="f2 v2.2.2\n", stderr="")
        raise subprocess.TimeoutExpired(command, timeout=1)

    timeout_result = F2RenameExecutor(runner=timeout_runner).preview_batch(request)
    assert timeout_result.items[0].error_code == "F2_TIMEOUT"

    def invalid_json_runner(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="f2 v2.2.2\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="not-json", stderr="")

    invalid_result = F2RenameExecutor(runner=invalid_json_runner).preview_batch(request)
    assert invalid_result.items[0].error_code == "F2_INVALID_JSON"


@pytest.mark.skipif(
    os.getenv("RUN_F2_INTEGRATION_TESTS", "false").lower() != "true",
    reason="只有显式启用时才调用本机真实 F2。",
)
def test_real_f2_dry_run_and_execute_smoke(tmp_path):
    """在具备固定 F2 二进制的环境中验证真实 CSV/JSON 契约。"""

    binary = os.getenv("F2_BINARY_PATH") or shutil.which("f2")
    if not binary:
        pytest.skip("未找到 F2 二进制。")
    source = tmp_path / "中文,原文件.txt"
    target = tmp_path / "2026_中文新文件.txt"
    source.write_text("真实 F2 smoke test", encoding="utf-8")
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    request = RenameBatchRequest(
        root_path=tmp_path,
        operation_plan_id="real-f2-plan",
        items=[
            RenameBatchItem(
                managed_file_id="real-managed-file",
                before_relative_path=source.name,
                after_relative_path=target.name,
                source_sha256=source_sha256,
            )
        ],
    )
    executor = F2RenameExecutor(
        binary_path=binary,
        expected_version=os.getenv("F2_EXPECTED_VERSION", "2.2.2"),
    )

    preview = executor.preview_batch(request)
    assert preview.status == "PREVIEWED"
    assert source.exists()
    result = executor.execute_batch(request)
    assert result.status == "EXECUTED"
    assert target.exists()
