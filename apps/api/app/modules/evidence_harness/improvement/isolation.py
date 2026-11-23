"""冻结离线候选；macOS 使用系统沙箱，Windows 使用受限本地 Python 子进程。"""

import hashlib
from contextlib import nullcontext
import json
import mimetypes
import os
import subprocess
import sys
import time

from app.modules.evidence_evaluation.frozen_inputs import write_new_json, digest
from app.modules.evidence_harness.improvement.patches import TARGET, source_hash
from app.modules.evidence_harness.improvement.process_io import PipeReader, stop_process
from app.modules.evidence_harness.paths import Path, tree, copy_tree

API_ROOT = Path(__file__).resolve().parents[4]
ROUTING_FILES = {"__init__.py", "runtime.py", "schemas.py", "diagnostics.py"}
# Last pre-Windows executor. Historical results still require full independent replay.
PRE_WINDOWS_TRUSTED_REF = "082b92debfe782cc7e3704d34fa8a8d1ee6159d98e0449ddc2550f84722c1b43"


def canonical_code(path):
    """Git's CRLF checkout must not change the identity of text source snapshots."""
    return Path(path).read_bytes().replace(b"\r\n", b"\n")


def compatible_trusted_refs():
    return {trusted_code_ref(), PRE_WINDOWS_TRUSTED_REF}


def trusted_code_ref():
    """绑定候选之外的执行器和评分规则，换裁判后不能假装同版历史结果。"""
    paths = sorted(Path(__file__).parent.glob("*.py"))
    routing = Path(__file__).parent.parent / "routing"
    paths += [routing / name for name in ("runtime.py", "comparator.py", "schemas.py", "diagnostics.py")]
    paths += [Path(__file__).parent.parent / "paths.py"]
    return digest({p.relative_to(API_ROOT.parents[1]).as_posix(): hashlib.sha256(canonical_code(p)).hexdigest() for p in paths})


def file_manifest(root: Path) -> dict:
    """逐文件核对实际源码，不跟随符号链接，也不把缓存当源码。"""
    result = {}
    root = Path(root)
    for path in tree(root):
        if path.is_symlink():
            raise ValueError("SNAPSHOT_SYMLINK_BLOCKED")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def snapshot(destination: Path, api_root: Path = API_ROOT):
    """复制本轮工作树实际源码，排除测试、脚本、评分器、私有数据与环境配置。"""
    destination, api_root = Path(destination), Path(api_root)
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    for path, relative in snapshot_entries(api_root):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(canonical_code(path))
        target.chmod(0o400)
    return file_manifest(destination)


def snapshot_entries(api_root):
    """冻结副本与本地准入使用同一源码清单，评分器及配置不能混入。"""
    api_root = Path(api_root)
    for path in sorted(tree(api_root / "app")):
        relative = path.relative_to(api_root)
        parts = relative.parts
        if any(p in {"tests", "scripts", "__pycache__"} for p in parts):
            continue
        if path.is_symlink():
            raise ValueError("SNAPSHOT_SYMLINK_BLOCKED")
        if not path.is_file() or path.suffix not in {".py", ".json"}:
            continue
        if "evidence_harness" in parts:
            allowed = (relative.as_posix() in {"app/modules/evidence_harness/__init__.py", "app/modules/evidence_harness/schemas.py"}
                       or path.parent.name == "routing" and path.name in ROUTING_FILES
                       or path.parent.name == "improvement" and path.name in {"__init__.py", "worker.py"})
            if not allowed:
                continue
        yield path, relative


def prepare_copies(root: Path, candidate_source: str, *, baseline: Path | None = None) -> dict:
    """两组从同一不可变快照产生，实际差异必须只有获准判断条件所在文件。"""
    root = Path(root)
    base = root / "baseline"
    if baseline is None:
        manifest = snapshot(base)
    else:
        copy_tree(baseline, base)
        manifest = file_manifest(base)
    for arm in ("control", "candidate"):
        copy_tree(base, root / arm)
    target = root / "candidate" / TARGET
    target.chmod(0o600)
    target.write_text(candidate_source, encoding="utf-8", newline="")
    target.chmod(0o400)
    candidate = file_manifest(root / "candidate")
    changed = {p for p in manifest.keys() | candidate.keys() if manifest.get(p) != candidate.get(p)}
    if changed != {TARGET}:
        raise ValueError("ISOLATION_PATCH_DIFF_INVALID")
    if candidate[TARGET] != source_hash(candidate_source):
        raise ValueError("ISOLATION_PATCH_HASH_INVALID")
    write_new_json(root / "snapshot-manifest.json", {"baseline": manifest, "candidate": candidate})
    return manifest


def sandbox_profile(code: Path, scratch: Path) -> str:
    """默认拒绝，只有运行时和当前源码可读，仅本次scratch可写，禁止网络和派生进程。"""
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        raise RuntimeError("OS_SANDBOX_UNAVAILABLE")
    literal = lambda value: json.dumps(str(Path(value).resolve()))
    read_dirs = [code, Path(sys.prefix), Path("/System"), Path("/usr/lib"), Path("/Library/Apple")]
    rules = ["(version 1)", "(deny default)", "(allow sysctl-read)", "(allow file-read-metadata)",
             "(allow process-exec (literal " + literal(sys.executable) + "))",
             "(allow file-map-executable " + " ".join("(subpath " + literal(p) + ")" for p in read_dirs) + ")",
             "(allow file-read* " + " ".join("(subpath " + literal(p) + ")" for p in read_dirs) + ")",
             # Python/httpx读取公共MIME映射表；只放行固定文件，不放开/etc目录。
             "(allow file-read* " + " ".join("(literal " + literal(p) + ")" for p in mimetypes.knownfiles) + ")",
             # 当前macOS的dyld需要打开根目录本身；literal不会放开其子文件。
             "(allow file-read* (literal \"/\") (literal \"/dev/null\") (literal \"/dev/urandom\") (literal \"/dev/random\"))",
             "(allow file-read* file-write* (subpath " + literal(scratch) + "))"]
    return "\n".join(rules)


def isolated_run(code: Path, scratch: Path, request: dict, *, broker=None, timeout=90, planner_mode="legacy") -> dict:
    """固定Python入口和有界IPC；父进程保留密钥，候选不能选择命令或访问父目录。"""
    code, scratch = Path(code), Path(scratch)
    if planner_mode not in {"legacy", "enabled"}:
        raise ValueError("ISOLATED_PLANNER_MODE_INVALID")
    scratch.mkdir(parents=True, mode=0o700, exist_ok=False)
    # 完全新环境，不继承HOME、代理、密钥、数据库配置或PYTHONPATH。
    environment = {"PATH": str(Path(sys.executable).parent), "HOME": str(scratch), "TMPDIR": str(scratch),
                   "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
                   }
    # 配置由宿主经IPC明确交付，不让公共组件从环境或上级.env重新加载。
    if "test_settings" in request:
        raise ValueError("SANDBOX_SETTINGS_OVERRIDE_BLOCKED")
    request = {**request, "test_settings": {
        "database_url": "postgresql://offline:offline@127.0.0.1/file_agent_unit_tests",
        "llm_enabled": True, "adaptive_planner_mode": planner_mode, "log_dir": str(scratch.resolve() / "logs")}}
    windows = sys.platform == "win32"
    if windows:
        from app.modules.evidence_harness.improvement import local_runtime
        write_new_json(scratch / "native-runtime.json", local_runtime.runtime())
        commands = nullcontext(local_runtime.worker_command(code, scratch))
        environment.update({k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ})
        environment.update(TEMP=str(scratch), TMP=str(scratch), USERPROFILE=str(scratch),
                           LOCALAPPDATA=str(scratch), APPDATA=str(scratch))
    else:
        commands = nullcontext(["/usr/bin/sandbox-exec", "-p", sandbox_profile(code, scratch), sys.executable, "-B", "-c",
                   "import sys;sys.path.insert(0," + repr(str(code)) + ");"
                   "from app.modules.evidence_harness.improvement.worker import main;main()"])
    with commands as command:
        return _exchange(command, scratch, environment, request, broker, timeout, windows=windows,
                         popen=local_runtime.popen if windows else None)


def _exchange(command, scratch, environment, request, broker, timeout, *, windows=False, popen=None):
    """The same bounded IPC and deadline apply to both isolation backends."""
    process = (popen or subprocess.Popen)(command, cwd=scratch, env=environment, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=not windows)
    reader = PipeReader(process.stdout)
    deadline, buffer, total, result = time.monotonic() + timeout, b"", 0, None
    try:
        process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
        process.stdin.flush()
        while time.monotonic() < deadline:
            chunk = reader.read(min(0.2, max(0, deadline - time.monotonic())))
            if chunk is None:
                continue
            if not chunk:
                break
            total += len(chunk)
            buffer += chunk
            if total > 16_000_000 or len(buffer) > 2_000_000:
                raise ValueError("SANDBOX_OUTPUT_LIMIT")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                item = json.loads(line)
                if item.get("type") == "MODEL_REQUEST":
                    if broker is None:
                        raise ValueError("SANDBOX_UNAUTHORIZED_MODEL_REQUEST")
                    response = broker(item)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("SANDBOX_DEADLINE")
                    process.stdin.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                    process.stdin.flush()
                elif item.get("type") == "RESULT" and result is None:
                    result = item
                else:
                    raise ValueError("SANDBOX_PROTOCOL_INVALID")
        if time.monotonic() >= deadline:
            raise TimeoutError("SANDBOX_DEADLINE")
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if process.returncode != 0 or result is None or buffer:
            raise RuntimeError("OS_SANDBOX_OR_WORKER_FAILED")
        if result.get("status") != "COMPLETED":
            code = str(result.get("error_type", "UNKNOWN"))
            module = str(result.get("missing_module") or result.get("blocked_basename") or "")
            if not all(c.isalnum() or c in "_." for c in code + module):
                code, module = "UNKNOWN", ""
            raise RuntimeError("ISOLATED_CHECK_FAILED:" + code + ":" + module)
        return result["result"]
    finally:
        stop_process(process)
        reader.close()
        process.stdin.close()
        process.stdout.close()


def probe_sandbox(code: Path, output: Path, *, remaining=None) -> dict:
    """实际验证当前边界；Windows 是 Python 审计及 Job，不能冒称系统文件沙箱。"""
    sentinel = output / "forbidden-secret.txt"
    sentinel.write_text("OS_SANDBOX_PRIVATE_SENTINEL")
    sentinel.chmod(0o600)
    def timeout():
        """探针同样不能绕开实验总时限。"""
        return min(90, remaining()) if remaining else 90
    checks = {"compile": isolated_run(code, output / "probe-compile", {"mode": "compile"}, timeout=timeout())["compiled"]}
    for action in ("read", "write", "network", "exec"):
        checks[action + "_blocked"] = isolated_run(code, output / ("probe-" + action),
            {"mode": "probe", "action": action, "path": str(sentinel)}, timeout=timeout())["blocked"]
    if not all(checks.values()) or sentinel.read_text() != "OS_SANDBOX_PRIVATE_SENTINEL":
        raise RuntimeError("OS_SANDBOX_PROBE_FAILED")
    return checks
