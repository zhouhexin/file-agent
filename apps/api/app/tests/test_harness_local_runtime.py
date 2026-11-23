"""本地 Python 验收：真实 Job、审计及规划子进程，模型只用确定性替身。"""

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.modules.evidence_harness.improvement import local_runtime as native, isolation, codex_backend
from app.modules.evidence_harness.improvement.process_io import stop_process


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch):
    """宿主模块导入只使用虚拟连接信息，测试不连接真实数据库。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://offline:offline@127.0.0.1/file_agent_unit_tests")


@pytest.fixture(scope="module")
def copied_source(tmp_path_factory):
    """真实源码快照只准备一次，每次请求仍逐文件核验并使用全新 scratch。"""
    root = isolation.Path(tmp_path_factory.mktemp("local-python"))
    code = root / "code"
    isolation.snapshot(code)
    return root, code


def test_local_profile_truthfully_identifies_non_os_sandbox():
    """不能把 Python 审计包装成系统文件隔离，也不能冒充旧容器身份。"""
    result = native.runtime()
    assert result["os_sandbox"] is False
    assert result["filesystem_guard"] == "CPYTHON_AUDIT"
    profile = codex_backend.profile({"model": "fake", "version": "codex-cli 0.166.0",
        "binary_ref": "a" * 64, "isolation": native.POLICY}, 3000)
    assert profile["codex_artifact_kind"] == "NATIVE_EXECUTABLE"
    assert profile["isolation"] == native.POLICY
    assert profile["cli_os_filesystem_sandbox"] is False


@pytest.mark.parametrize("target,original,candidate", [
    ("prompt", 'ADAPTIVE_PLANNER_SYSTEM_PROMPT = "base"\n', 'ADAPTIVE_PLANNER_SYSTEM_PROMPT = "base"\nimport os\n'),
    ("prompt", 'ADAPTIVE_PLANNER_SYSTEM_PROMPT = "base"\n', 'ADAPTIVE_PLANNER_SYSTEM_PROMPT = "other"\n'),
])
def test_native_rejects_executable_or_replaced_prompt(target, original, candidate):
    """本地准入不是裸运行脚本；未获准的 AST 改动必须在启动前被拒绝。"""
    with pytest.raises(ValueError):
        native._validate_prompt(original, candidate)


def test_native_allows_only_appended_literal_prompt():
    from app.modules.evidence_harness.improvement.adaptive_policy import MARKER
    original = 'ADAPTIVE_PLANNER_SYSTEM_PROMPT = "base"\n'
    candidate = 'ADAPTIVE_PLANNER_SYSTEM_PROMPT = ' + repr("base" + MARKER + "请检查当前工具契约。") + '\n'
    native._validate_prompt(original, candidate)


def test_native_condition_uses_existing_guard_whitelist():
    """复用真实补丁生成器，合法条件通过，追加可执行语句失败。"""
    from app.modules.evidence_harness.improvement import patches
    original = (isolation.API_ROOT / patches.TARGET).read_text(encoding="utf-8")
    _, _, node = patches.target_node(original)
    lines = original.encode().splitlines(keepends=True)
    begin = sum(map(len, lines[:node.test.lineno - 1])) + node.test.col_offset
    end = sum(map(len, lines[:node.test.end_lineno - 1])) + node.test.end_col_offset
    expression = ast.unparse(ast.BoolOp(op=ast.And(), values=[node.test, ast.Constant(value=True)]))
    candidate = (original.encode()[:begin] + expression.encode() + original.encode()[end:]).decode()
    native._validate_condition(original, candidate)
    with pytest.raises(ValueError):
        native._validate_condition(original, candidate + '\nimport os\n')


def test_snapshot_rejects_unknown_source_before_launch(tmp_path, monkeypatch):
    """缺源码、混入额外文件或任意第三方脚本时，不能创建本地执行进程。"""
    root = isolation.Path(tmp_path)
    with pytest.raises(ValueError, match="UNSAFE_SNAPSHOT"):
        native.validate_snapshot(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows 本地进程超时专项")
def test_native_timeout_terminates_process_and_closes_job(tmp_path):
    """后台阻塞时仍执行父级截止时间，结束后不能遗留 Job 或进程。"""
    seen = []
    def spawn(*args, **kwargs):
        process = native.popen(*args, **kwargs)
        seen.append(process)
        return process
    with pytest.raises(TimeoutError):
        isolation._exchange([sys.executable, "-I", "-S", "-c", "import sys,time;sys.stdin.readline();time.sleep(60)"],
            tmp_path, dict(os.environ), {}, None, .2, windows=True, popen=spawn)
    assert seen and seen[0].poll() is not None and seen[0]._harness_job.handle is None


@pytest.mark.skipif(os.name != "nt", reason="Windows 原生 Job 专项")
def test_real_job_is_attached_before_task_and_closed_on_cleanup():
    """实际进程握手验证 Job 可用；不只模拟 Win32 返回值。"""
    process = native.popen([sys.executable, "-I", "-S", "-c", "import sys;print(sys.stdin.readline().strip())"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.stdin.write(b"native-job-ok\n")
        process.stdin.close()
        assert process.stdout.read().strip() == b"native-job-ok"
        assert process.wait(timeout=10) == 0
    finally:
        stop_process(process)
        process.stdout.close()
        process.stderr.close()
    assert process._harness_job.handle is None


@pytest.mark.skipif(os.name != "nt", reason="Windows 原生 Job 专项")
def test_job_kernel_blocks_extra_process_without_python_audit():
    """不安装审计钩子也必须由 Job 拒绝子进程，不能只靠模型自律。"""
    body = 'import sys,subprocess,json;sys.stdin.readline()\ntry:\n subprocess.run([sys.executable,"-c","pass"],check=True)\n print("UNEXPECTED_CHILD")\nexcept OSError:\n print("CHILD_BLOCKED")\n'
    process = native.popen([sys.executable, "-I", "-S", "-c", body],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.stdin.write(b"go\n"); process.stdin.close()
        assert process.stdout.read().strip() == b"CHILD_BLOCKED"
        assert process.wait(timeout=10) == 0
    finally:
        stop_process(process)
        process.stdout.close(); process.stderr.close()


@pytest.mark.skipif(os.name != "nt" or os.environ.get("HARNESS_NATIVE_ACCEPTANCE") != "1", reason="显式本地 Python 实际验收")
def test_actual_local_boundaries_and_fixed_regression(copied_source):
    """执行本机 Python 与真实源码：读写、网络、启动程序探针及固定业务回归。"""
    root, code = copied_source
    out = root / "probes"
    out.mkdir()
    assert all(isolation.probe_sandbox(code, out).values())
    from app.modules.evidence_harness.improvement.executor import regression_inputs
    from app.modules.agent.planner import build_plan_from_user_intent
    from app.modules.llm.schemas import UserIntentPlan
    for i, request in enumerate(regression_inputs()):
        result = isolation.isolated_run(code, root / f"regression-{i}", request)
        expected = build_plan_from_user_intent(intent_plan=UserIntentPlan.model_validate(request["intent"]),
            message=request["message"], attachments=request["attachments"]).model_dump(mode="json")
        assert result == expected


@pytest.mark.skipif(os.name != "nt" or os.environ.get("HARNESS_NATIVE_ACCEPTANCE") != "1", reason="显式本地 Python 实际验收")
@pytest.mark.parametrize("mode", ["legacy", "enabled"])
def test_actual_local_planning_preserves_chinese_and_model_broker(copied_source, mode):
    """中文必须经真实 stdin/stdout 完整传递，付费接口由固定结果代替。"""
    from app.modules.evidence_harness.routing.dataset import current_catalog
    from app.modules.evidence_harness.routing.schemas import RoutingCase, SourceSignal
    from app.modules.evidence_harness.routing.runtime import run_planning
    from app.tests.test_planning_evaluation import model_factory
    root, code = copied_source
    case = RoutingCase(case_id="NATIVE1", source_run_ref="a" * 64,
        question="查找培训通知，只列文件名。", adaptive_mode=mode, observation=SourceSignal(
            observed_route="FILE_LIST", entry_tool="hybrid-search", evidence_answer_called=False, run_failed=False))
    catalog = current_catalog()
    client = model_factory()(case, 1)
    def response(**kwargs):
        return client.complete_json_with_usage(**kwargs, max_output_tokens=3000)[0]
    seen = []
    def broker(item):
        seen.append(item)
        assert item["user_payload"]["message"] == case.question
        return {"status": "COMPLETED", "raw": response(system_prompt=item["system_prompt"], user_payload=item["user_payload"])}
    result = isolation.isolated_run(code, root / ("chinese-planning-" + mode), {
        "mode": "planning", "case": case.model_dump(mode="json"), "catalog": catalog, "capture_audit": True},
        broker=broker, planner_mode=mode)
    assert seen and result["status"] == "COMPLETED"
    class Client:
        """对照宿主的同一固定模型结果，不读取 Gold 或外部网络。"""
        def complete_json(self, **kwargs):
            return response(**kwargs)
    from app.core.config import Settings
    settings = Settings(database_url="postgresql://offline:offline@127.0.0.1/file_agent_unit_tests",
                        llm_enabled=True, adaptive_planner_mode=mode)
    expected = run_planning(case, catalog=catalog, client=Client(), settings=settings, capture_audit=True)
    assert result == expected
    if mode == "enabled":
        assert result["planning_audit"]["effective_mode"] == "ADAPTIVE"


@pytest.mark.skipif(os.name != "nt" or os.environ.get("HARNESS_NATIVE_ACCEPTANCE") != "1", reason="显式本地候选提示验收")
def test_actual_candidate_prompt_runs_in_local_python(copied_source):
    """实际子进程必须读到候选常量，不能错误地复用宿主已导入的原提示。"""
    from app.modules.evidence_harness.paths import copy_tree
    from app.modules.evidence_harness.improvement import adaptive_policy as policy
    from app.modules.evidence_harness.routing.dataset import current_catalog
    from app.modules.evidence_harness.routing.schemas import RoutingCase, SourceSignal
    from app.tests.test_planning_evaluation import model_factory
    root, code = copied_source
    candidate = root / "candidate-prompt"
    copy_tree(code, candidate)
    target = candidate / policy.TARGET
    source = target.read_text(encoding="utf-8")
    _, node = policy.prompt_node(source)
    prompt = node.value.value + policy.MARKER + "请逐项核对工具输入。"
    lines = source.encode().splitlines(keepends=True)
    begin = sum(map(len, lines[:node.value.lineno - 1])) + node.value.col_offset
    end = sum(map(len, lines[:node.value.end_lineno - 1])) + node.value.end_col_offset
    changed = source.encode()[:begin] + repr(prompt).encode() + source.encode()[end:]
    target.chmod(0o600)
    target.write_bytes(changed)
    target.chmod(0o400)
    case = RoutingCase(case_id="NATIVEC", source_run_ref="b" * 64,
        question="查找培训通知，只列文件名。", adaptive_mode="enabled", observation=SourceSignal(
            observed_route="FILE_LIST", entry_tool="hybrid-search", evidence_answer_called=False, run_failed=False))
    client = model_factory()(case, 1)
    seen = []
    def broker(item):
        seen.append(item)
        assert item["system_prompt"] == prompt
        raw, _ = client.complete_json_with_usage(system_prompt=item["system_prompt"],
            user_payload=item["user_payload"], max_output_tokens=3000)
        return {"status": "COMPLETED", "raw": raw}
    result = isolation.isolated_run(candidate, root / "candidate-planning", {"mode": "planning",
        "case": case.model_dump(mode="json"), "catalog": current_catalog(), "capture_audit": True},
        planner_mode="enabled", broker=broker)
    assert seen and result["planning_audit"]["effective_mode"] == "ADAPTIVE"


@pytest.mark.skipif(os.name != "nt" or os.environ.get("HARNESS_NATIVE_ACCEPTANCE") != "1", reason="显式本地长路径导入验收")
def test_actual_imports_work_with_long_nested_source(copied_source):
    """源码根尚短于 MAX_PATH、内部模块已超长时，导入与编译仍须成功。"""
    from app.modules.evidence_harness.paths import copy_tree, tree
    root, code = copied_source
    folder = "long-import-" + "a" * max(1, 235 - len(str(root)) - len("long-import-") - 1)
    long_code = root / folder
    scratch = root / "long-import-check"
    try:
        copy_tree(code, long_code)
        assert 220 <= len(str(long_code)) < 240
        assert isolation.isolated_run(long_code, scratch, {"mode": "compile"}) == {"compiled": True}
    finally:
        # 只删除本测试创建的两个临时目标，先确认最终绝对路径未超出 fixture 根。
        for owned in (long_code, scratch):
            if not owned.resolve().is_relative_to(root.resolve()):
                raise AssertionError("Cleanup outside test root")
            if owned.exists():
                for path in sorted(tree(owned), key=lambda p: len(p.parts), reverse=True):
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.chmod(0o600)
                        path.unlink()
                owned.rmdir()


def test_manifest_uses_portable_paths_and_candidate_bytes_preserve_utf8_lf(tmp_path, monkeypatch):
    baseline = tmp_path / "original"
    target = baseline / isolation.TARGET
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original\n")
    source = "# \u4e2d\u6587\nanswer = 42\n"
    output = tmp_path / "copies"
    output.mkdir()
    isolation.prepare_copies(output, source, baseline=baseline)
    manifest = isolation.file_manifest(output / "candidate")
    assert set(manifest) == {isolation.TARGET}
    assert manifest[isolation.TARGET] == hashlib.sha256(source.encode("utf-8")).hexdigest()


def test_snapshot_normalizes_checkout_newlines_and_preserves_non_ascii(tmp_path):
    api = tmp_path / "api"
    source = api / "app" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_bytes("# \u4e2d\u6587\r\nanswer = 42\r\n".encode("utf-8"))
    output = tmp_path / "snapshot"
    manifest = isolation.snapshot(output, api_root=api)
    assert (output / "app/example.py").read_bytes() == "# \u4e2d\u6587\nanswer = 42\n".encode("utf-8")
    assert source.read_bytes().count(b"\r\n") == 2
    assert manifest["app/example.py"] == hashlib.sha256(isolation.canonical_code(source)).hexdigest()


def test_long_snapshot_paths_work_without_global_pathlib_changes(tmp_path):
    from app.modules.evidence_harness.paths import Path as HarnessPath, copy_tree
    # Exercise real filesystem calls beyond MAX_PATH, even when LongPathsEnabled=0.
    root = HarnessPath(tmp_path)
    source = root / "long-source"
    deep = source.joinpath(*["directory-" + str(i) + "-" + "a" * 28 for i in range(7)])
    target = deep / "example.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"answer = 42\n")
    destination = root / "long-copy"
    try:
        copy_tree(source, destination)
        relative = target.relative_to(source)
        assert (destination / relative).read_bytes() == target.read_bytes()
        assert isolation.file_manifest(source) == isolation.file_manifest(destination)
        assert "\\\\?\\" not in str(target.resolve())
        assert Path is not HarnessPath if os.name == "nt" else Path is HarnessPath
    finally:
        # These are precisely the two roots created by this test, both under tmp_path.
        for folder in (destination, source):
            if not folder.resolve().is_relative_to(root.resolve()):
                raise AssertionError("Cleanup outside test root")
            if folder.exists():
                entries = list(folder.rglob("*"))
                for path in sorted(entries, key=lambda p: len(p.parts), reverse=True):
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink()
                folder.rmdir()


def test_windows_launcher_waits_and_returns_actual_child_exit_code():
    import importlib.util
    path = Path(__file__).resolve().parents[4] / "scripts/run_evidence_test.py"
    spec = importlib.util.spec_from_file_location("windows_harness_launcher", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    code = "import os\n" + launcher.handoff([sys.executable, "-c", "raise SystemExit(7)"])
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert result.returncode == 7
