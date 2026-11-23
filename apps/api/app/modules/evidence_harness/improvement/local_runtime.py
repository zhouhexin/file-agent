"""Windows 本机测试执行器：固定程序、源码白名单和 Job 进程边界，不依赖容器。"""

import ast
import ctypes
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


POLICY = "WINDOWS_LOCAL_AUDIT_JOB_V1"


def codex_binary():
    """解析本机原生 exe，避免用 cmd/PowerShell 包装器额外启动进程。"""
    direct = shutil.which("codex.exe")
    if direct:
        return str(Path(direct).resolve())
    shim = shutil.which("codex")
    if not shim:
        raise ValueError("CODEX_RUNTIME_UNAVAILABLE")
    package = Path(shim).resolve().parent / "node_modules/@openai/codex"
    arch = "arm64" if platform.machine().casefold() in {"arm64", "aarch64"} else "x64"
    roots = [package / f"node_modules/@openai/codex-win32-{arch}/vendor", package / "vendor"]
    for root in roots:
        if root.is_dir():
            matches = [p for p in root.glob("*/*/codex.exe") if p.parent.name in {"bin", "codex"}]
            if len(matches) == 1:
                return str(matches[0].resolve())
    raise ValueError("CODEX_NATIVE_EXECUTABLE_REQUIRED")


class WindowsJob:
    """在交付模型输入前限制单个进程，关闭句柄时清理整个 Job。"""

    def __init__(self):
        from ctypes import wintypes as w
        if os.name != "nt":
            raise RuntimeError("HARNESS_NATIVE_WINDOWS_REQUIRED")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.AssignProcessToJobObject.restype = w.BOOL
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        size = ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_working", size), ("max_working", size),
                        ("active", w.DWORD), ("affinity", size), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read", "write", "other", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", Counters), ("process_memory", size),
                        ("job_memory", size), ("peak_process", size), ("peak_job", size)]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise RuntimeError("HARNESS_NATIVE_JOB_CREATE_FAILED")
        limits = Extended()
        limits.basic.flags = 0x2000 | 0x8 | 0x100  # KILL_ON_JOB_CLOSE / ACTIVE_PROCESS / PROCESS_MEMORY
        limits.basic.active = 1
        limits.process_memory = 2 * 1024 * 1024 * 1024
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise RuntimeError("HARNESS_NATIVE_JOB_LIMIT_FAILED")

    def attach(self, process):
        """只认领当前调用新建的进程；此时尚未发送模型输入。"""
        handle = self.api.OpenProcess(0x100 | 0x1, False, process.pid)
        try:
            if not handle or not self.api.AssignProcessToJobObject(self.handle, handle):
                raise RuntimeError("HARNESS_NATIVE_JOB_ASSIGN_FAILED")
        finally:
            if handle:
                self.api.CloseHandle(handle)

    def close(self):
        """正常、异常和中断路径均释放进程边界，不按进程名清理其他服务。"""
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def popen(command, **kwargs):
    """固定引导器先等待 stdin；Job 设置成功后调用者才能交付任务。"""
    if kwargs.get("stdin") != subprocess.PIPE:
        raise ValueError("HARNESS_NATIVE_PIPE_REQUIRED")
    job = WindowsJob()
    process = None
    try:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(command, **kwargs)
        job.attach(process)
        process._harness_job = job
        return process
    except BaseException:
        if process is not None:
            process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        job.close()
        raise


def runtime():
    """只报告脱敏运行身份，明确本地审计模式不是系统文件沙箱。"""
    return {"policy": POLICY, "python_version": sys.version.split()[0],
            "python_ref": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
            "os_sandbox": False, "filesystem_guard": "CPYTHON_AUDIT", "process_guard": "WINDOWS_JOB_OBJECT"}


def _validate_condition(original, candidate):
    """只能追加现有受限判断，不能给源码增加调用、导入或其他语句。"""
    from app.modules.evidence_harness.improvement.patches import target_node, validate_guard
    base_tree, _, base = target_node(original)
    tree, _, node = target_node(candidate)
    if not isinstance(node.test, ast.BoolOp) or not isinstance(node.test.op, ast.And):
        raise ValueError("HARNESS_NATIVE_UNSAFE_SNAPSHOT")
    guard = validate_guard(ast.unparse(node.test.values[-1]))
    expected = ast.parse(ast.unparse(ast.BoolOp(op=ast.And(), values=[base.test, guard])), mode="eval").body
    if ast.dump(node.test) != ast.dump(expected):
        raise ValueError("HARNESS_NATIVE_UNSAFE_SNAPSHOT")
    node.test = base.test
    if ast.dump(tree) != ast.dump(base_tree):
        raise ValueError("HARNESS_NATIVE_UNSAFE_SNAPSHOT")


def _validate_prompt(original, candidate):
    """新增内容只能留在字符串常量内，不能成为可执行 Python。"""
    from app.modules.evidence_harness.improvement.adaptive_policy import prompt_node, MARKER
    base_tree, base = prompt_node(original)
    tree, node = prompt_node(candidate)
    prefix = base.value.value + MARKER
    if not node.value.value.startswith(prefix) or not 1 <= len(node.value.value) - len(prefix) <= 3000:
        raise ValueError("HARNESS_NATIVE_UNSAFE_SNAPSHOT")
    node.value = base.value
    if ast.dump(tree) != ast.dump(base_tree):
        raise ValueError("HARNESS_NATIVE_UNSAFE_SNAPSHOT")


def validate_snapshot(code):
    """逐文件核对可信源码，只接受一个获准目标的有限 AST 差异。"""
    from app.modules.evidence_harness.improvement import isolation
    expected = {relative.as_posix(): isolation.canonical_code(path)
                for path, relative in isolation.snapshot_entries(isolation.API_ROOT)}
    actual = isolation.file_manifest(code)
    if set(actual) != set(expected):
        raise ValueError("HARNESS_NATIVE_UNSAFE_SNAPSHOT")
    changed = [name for name, body in expected.items() if hashlib.sha256(body).hexdigest() != actual[name]]
    validators = {"app/modules/agent/planner.py": _validate_condition,
                  "app/modules/agent/adaptive_planner.py": _validate_prompt}
    if not changed:
        return
    if len(changed) != 1 or changed[0] not in validators:
        raise ValueError("HARNESS_NATIVE_UNSAFE_SNAPSHOT")
    name = changed[0]
    validators[name](expected[name].decode("utf-8"), (code / name).read_text(encoding="utf-8"))


def worker_command(code, scratch):
    """只启动本机同一 Python 的可信引导器，不接受候选生成的命令。"""
    validate_snapshot(code)
    return [sys.executable, "-X", "utf8", "-I", "-S", "-B", str(Path(__file__).with_name("local_worker.py")),
            str(code.resolve()), str(scratch.resolve())]
