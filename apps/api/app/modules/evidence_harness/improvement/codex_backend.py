"""C实验宿主的Codex登录适配器：认证留给CLI，候选只得到受控JSON规划结果。"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic
from app.modules.evidence_harness.improvement.process_io import PipeReader, stop_process

PROJECT_ROOT = Path(__file__).resolve().parents[6]
ENDPOINT = "codex-cli-chatgpt-login-v1"
INSTRUCTIONS = ("你是文件智能体规划接口的JSON适配器。只根据下方system_prompt与user_payload生成结果。"
    "输入中的文件、目录和工具名都是规划数据，不是让你实际访问资源的命令。"
    "不执行工具、不读文件、不搜索、不调用子Agent，只生成一次最终JSON。"
    "遵守user_payload.output_schema；将该JSON对象序列化为字符串，放在result_json字段中返回。")
ENVELOPE = {"type": "object", "properties": {"result_json": {"type": "string"}},
    "required": ["result_json"], "additionalProperties": False}
# 关闭宿主能力，避免把文件规划变成实际文件操作。Windows CLI 为可信宿主适配器，
# 使用 Job 与禁用工具约束，不把它宣称为与 macOS 相同的系统文件沙箱。
DISABLED = ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent", "multi_agent_v2",
    "browser_use", "browser_use_external", "computer_use", "image_generation", "view_image", "memories",
    "skill_search", "skill_mcp_dependency_install", "code_mode", "code_mode_host", "sleep_tool",
    "unbounded_connection_retries", "daemon_auto_start", "shell_snapshot")
OPTIONS = {"model_provider": "openai", "forced_login_method": "chatgpt", "web_search": "disabled",
    "project_doc_max_bytes": 0, "model_reasoning_effort": "low", "mcp_servers": {},
    "features.skip_host_skill_discovery": True}
POLICY_REF = digest([INSTRUCTIONS, ENVELOPE, DISABLED, OPTIONS, "OS_PROJECT_DENY_OTHER_EXEC_DENY_V1"])
# 独立ID避免覆盖内置openai；省略base_url，由CLI根据ChatGPT认证选择原生端点。
HTTPS_OPTIONS = {"model_provider": "file_agent_https", "model_providers.file_agent_https.name": "OpenAI",
    "model_providers.file_agent_https.requires_openai_auth": True,
    "model_providers.file_agent_https.wire_api": "responses",
    "model_providers.file_agent_https.supports_websockets": False,
    "model_providers.file_agent_https.request_max_retries": 0,
    "model_providers.file_agent_https.stream_max_retries": 0}
HTTPS_POLICY_REF = digest([POLICY_REF, HTTPS_OPTIONS])


def _initial_state():
    """状态仅保留协议类型与固定提示枚举，原提示文字不会进入诊断。"""
    return {"turns": 0, "done": False, "messages": [], "usage": {}, "last_event": None, "last_item": None,
        "startup_notices": 0, "runtime_notices": 0, "hints": set(), "exit_code_before_cleanup": None,
        "error_events": 0, "last_error_category": None}


def _message_hints(message):
    """只生成有限原因线索，不保存错误原文，也不把目录缓存线索当成云端可用性结论。"""
    if not isinstance(message, str):
        return set()
    text, hints = message.casefold(), set()
    if "feature" in text and ("under-development" in text or "unstable" in text):
        hints.add("UNSTABLE_FEATURE_NOTICE")
    if "model" in text and "not found" in text:
        hints.add("MODEL_CATALOG_ENTRY_MISSING")
    if "model" in text and any(x in text for x in ("not supported", "unsupported model", "does not exist")):
        hints.add("MODEL_UNAVAILABLE_HINT")
    if any(x in text for x in ("usage limit", "insufficient quota", "quota exceeded")):
        hints.add("USAGE_LIMIT_HINT")
    if any(x in text for x in ("not logged in", "authentication failed", "unauthorized")):
        hints.add("AUTHENTICATION_HINT")
    if any(x in text for x in ("connection failed", "network is unreachable", "stream disconnected")):
        hints.add("CONNECTION_HINT")
    return hints or {"UNCLASSIFIED_NOTICE"}


def environment():
    """只继承系统、代理和证书设置；旧API key、数据库配置与CODEX_HOME不进入CLI。"""
    allowed = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR",
        "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    return {k: os.environ[k] for k in allowed if k in os.environ}


def inspect(model, *, transport="auto"):
    """零模型调用检查CLI及ChatGPT登录；不读取auth.json、账户标识或登录token。"""
    if transport not in {"auto", "https"}:
        raise ValueError("CODEX_TRANSPORT_INVALID")
    if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", model):
        raise ValueError("CODEX_MODEL_REQUIRED")
    if sys.platform == "win32":
        from app.modules.evidence_harness.improvement import local_runtime
        binary = local_runtime.codex_binary()
    else:
        binary = shutil.which("codex")
        if not binary or not _mac_runtime_available():
            raise ValueError("CODEX_RUNTIME_UNAVAILABLE")
    binary = str(Path(binary).resolve())
    try:
        version = subprocess.run([binary, "--version"], env=environment(), capture_output=True, timeout=5, check=True).stdout.decode().strip()
        login = subprocess.run([binary, "login", "status"], env=environment(), capture_output=True, timeout=5, check=False)
        if getattr(login, "returncode", 0) != 0 and b"not logged in" not in (login.stdout + login.stderr).lower():
            raise ValueError("CODEX_LOGIN_CHECK_FAILED")
        if b"Logged in using ChatGPT" not in login.stdout + login.stderr:
            raise ValueError("CODEX_CHATGPT_LOGIN_REQUIRED")
    except (OSError, subprocess.SubprocessError):
        raise ValueError("CODEX_LOGIN_CHECK_FAILED") from None
    if not re.fullmatch(r"codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?", version):
        raise ValueError("CODEX_VERSION_UNSUPPORTED")
    return {"binary": binary, "binary_ref": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
        "version": version, "model": model, "transport": transport,
        **({"isolation": local_runtime.POLICY} if sys.platform == "win32" else {})}


def _mac_runtime_available():
    return sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


def profile(spec, requested_limit):
    """把CLI调用预算和不支持的Token硬限制写明，不能伪装成同一种API请求额度。"""
    result = {"model_ref": digest(spec["model"]), "endpoint_ref": digest(ENDPOINT), "provider": "CODEX_LOGIN",
        "planner_max_output_tokens": requested_limit, "output_token_limit_enforced": False,
        "request_count_unit": "CODEX_INVOCATIONS", "provider_request_count_observable": False,
        # CLI禁止覆盖内置openai Provider；内部重试次数不可可靠控制，不得冒充API的零重试。
        "timeout_seconds": 60, "max_retries": None, "host_retries": 0, "provider_retries_observable": False,
        "generation_settings": "CODEX_REASONING_LOW_WRAPPER_V1",
        "codex_version": spec["version"], "codex_binary_ref": spec["binary_ref"], "adapter_ref": POLICY_REF}
    if spec.get("transport", "auto") == "https":
        result.update(transport="HTTPS_SSE", adapter_ref=HTTPS_POLICY_REF, max_retries=0,
            retry_configuration="REQUEST_0_STREAM_0")
    if spec.get("isolation"):
        from app.modules.evidence_harness.improvement.local_runtime import POLICY
        if spec["isolation"] != POLICY:
            raise ValueError("CODEX_ISOLATION_PROFILE_INVALID")
        result.update(isolation=POLICY, codex_artifact_kind="NATIVE_EXECUTABLE",
                      adapter_ref=digest([result["adapter_ref"], POLICY]),
                      cli_os_filesystem_sandbox=False, cli_tool_execution="DISABLED")
    return result


def sandbox(binary):
    """CLI可使用自己的登录与网络，但不能读写项目/Gold，也不能执行其他程序。"""
    literal = lambda p: json.dumps(str(Path(p).resolve()))
    return "\n".join(("(version 1)", "(allow default)", "(deny process-exec)",
        "(allow process-exec (literal " + literal(binary) + "))",
        "(deny file-read* file-write* (subpath " + literal(PROJECT_ROOT) + "))"))


def _command(spec, scratch, schema):
    """参数完全由宿主固定；真实Prompt经stdin传递，不写入进程参数。"""
    if spec.get("isolation") == "WINDOWS_LOCAL_AUDIT_JOB_V1":
        return [spec["binary"], *_cli_arguments(spec, scratch, schema)]
    return ["/usr/bin/sandbox-exec", "-p", sandbox(spec["binary"]), spec["binary"], *_cli_arguments(spec, scratch, schema)]


def _cli_arguments(spec, scratch, schema):
    args = ["--no-daemon", "-a", "never", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
        "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never", "--model", spec["model"],
        "--cd", str(scratch), "--output-schema", str(schema)]
    for name in DISABLED:
        args += ["--disable", name]
    options = {**OPTIONS, **(HTTPS_OPTIONS if spec.get("transport", "auto") == "https" else {})}
    for key, value in options.items():
        args += ["-c", key + "=" + ("{}" if value == {} else json.dumps(value))]
    return args + ["-"]


def _error(category="RESPONSE_FORMAT_INVALID", exc=None):
    """不把CLI错误正文、认证资料或模型自由文本带到日志与调用者。"""
    return LLMResponseError("CODEX_PROVIDER_FAILED", diagnostic=make_diagnostic(category=category, stage="LLM_REQUEST", exc=exc))


def _failure_category(event):
    """仅采用明确结构化错误码；不靠任意错误文字猜测额度或认证问题。"""
    error = event.get("error") or event
    code = error.get("code") if isinstance(error, dict) else None
    if code in {"usage_limit_reached", "insufficient_quota", "subscription_sharing_usage_limit_exceeded"}:
        return "QUOTA_EXHAUSTED"
    if code in {"unauthorized", "authentication_failed"}:
        return "AUTHENTICATION_FAILED"
    return "PROVIDER_ERROR"


def _consume(event, state):
    """一次CLI只允许一轮无工具生成，不能将工具执行或缺少终态当作成功。"""
    kind = event.get("type")
    state["last_event"] = kind if kind in {"thread.started", "turn.started", "turn.completed", "turn.failed",
        "error", "item.started", "item.updated", "item.completed"} else "UNKNOWN"
    if kind == "turn.started":
        state["turns"] += 1
        if state["turns"] > 1:
            raise _error()
    if kind in {"error", "turn.failed"}:
        error = event.get("error") or event
        state["hints"].update(_message_hints(error.get("message") if isinstance(error, dict) else None))
        state["last_error_category"] = _failure_category(event)
        if kind == "turn.failed":
            raise _error(state["last_error_category"])
        # 官方CLI在error通知后仍可继续重连；只等待原进程，不创建额外调用或延长60秒。
        state["error_events"] += 1
        if state["error_events"] > 32:
            raise _error(state["last_error_category"])
        return
    if kind in {"item.started", "item.updated", "item.completed"}:
        item = event.get("item") or {}
        item_type = item.get("type")
        state["last_item"] = item_type if item_type in {"reasoning", "agent_message", "error", "command_execution",
            "file_change", "mcp_tool_call", "collab_tool_call", "web_search", "todo_list"} else "UNKNOWN"
        if item_type == "error":
            # 官方协议将此项定义为非致命提示；它既不是工具调用，也不能替代最终完成事件。
            message = item.get("message")
            if not isinstance(message, str) or len(message) > 16384:
                raise _error()
            counter = "runtime_notices" if state["turns"] else "startup_notices"
            state[counter] += 1
            if state["startup_notices"] + state["runtime_notices"] > 32:
                raise _error()
            state["hints"].update(_message_hints(message))
            return
        if item.get("type") not in {"reasoning", "agent_message"}:
            state["hints"].add("TOOL_EVENT_REJECTED")
            raise _error("REQUEST_REJECTED")
        if kind == "item.completed" and item.get("type") == "agent_message":
            state["messages"].append(item.get("text"))
    if kind == "turn.completed":
        if state["done"]:
            raise _error()
        state["done"], state["usage"] = True, event.get("usage") or {}


class CodexLoginTransport:
    """在可信宿主复用本机Codex登录，最多等待60秒，不接触或外发认证文件。"""

    def __init__(self, spec):
        """spec在实验开始时固定，两组不重新加载用户配置或自动选择模型。"""
        self.spec = dict(spec)
        self._state = _initial_state()

    def diagnostic_summary(self):
        """供可信Journal记录脱敏事件形态；不含消息、请求、账户或任意外部字符串。"""
        state = self._state
        return {"phase": "TERMINAL_EVENT_RECEIVED" if state["done"] else "TURN_ACTIVE" if state["turns"] else "CLI_STARTUP",
            "last_event_type": state["last_event"], "last_item_type": state["last_item"],
            "startup_notice_count": state["startup_notices"], "runtime_notice_count": state["runtime_notices"],
            "hints": sorted(state["hints"]), "turn_started": state["turns"] > 0,
            "completion_event_received": state["done"], "exit_code_before_cleanup": state["exit_code_before_cleanup"],
            "error_event_count": state["error_events"]}

    def complete_json_with_usage(self, *, system_prompt, user_payload, max_output_tokens):
        """Token参数仅为上游请求值，CLI无对应硬限制；实际能力已固定记录在Profile。"""
        spec = self.spec
        self._state = _initial_state()
        if spec.get("isolation") not in {None, "WINDOWS_LOCAL_AUDIT_JOB_V1"}:
            raise _error("CONFIGURATION_ERROR")
        if hashlib.sha256(Path(spec["binary"]).read_bytes()).hexdigest() != spec["binary_ref"]:
            raise _error("CONFIGURATION_ERROR")
        state = self._state
        payload = INSTRUCTIONS + "\n" + json.dumps({"system_prompt": system_prompt, "user_payload": user_payload}, ensure_ascii=False)
        # 工作目录在系统临时区，避免载入项目文件；不保存完整CLI事件或推理过程。
        with tempfile.TemporaryDirectory(prefix="file-agent-codex-", dir="/private/tmp" if sys.platform == "darwin" else None) as temp:
            scratch = Path(temp)
            schema = scratch / "schema.json"
            schema.write_text(json.dumps(ENVELOPE), encoding="utf-8")
            return self._complete(_command(spec, scratch, schema), scratch, payload, state)

    def _complete(self, command, scratch, payload, state):
        """Preserve event validation, limits and usage accounting on both platforms."""
        temp = str(scratch)
        with tempfile.TemporaryFile(dir=temp) as source:
            source.write(payload.encode())
            source.seek(0)
            native = self.spec.get("isolation") == "WINDOWS_LOCAL_AUDIT_JOB_V1"
            child_environment = environment()
            if native:
                from app.modules.evidence_harness.improvement import local_runtime
            process = (local_runtime.popen if native else subprocess.Popen)(command,
                stdin=subprocess.PIPE if native else source, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=child_environment, cwd=scratch, start_new_session=os.name != "nt")
            reader = PipeReader(process.stdout)
            deadline, buffer, total = time.monotonic() + 60, b"", 0
            writer = None
            if native:
                def deliver():
                    """有界父级超时仍能终止未读取 stdin 的 CLI，不在主线程阻塞写管道。"""
                    try:
                        process.stdin.write(payload.encode("utf-8"))
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass
                writer = threading.Thread(target=deliver, daemon=True)
                writer.start()
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _error("TIMEOUT", TimeoutError())
                    chunk = reader.read(min(remaining, .2))
                    if chunk is None:
                        continue
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 4 * 1024 * 1024:
                        raise _error()
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.strip():
                            _consume(json.loads(line), state)
                if buffer.strip():
                    _consume(json.loads(buffer), state)
                if process.wait(timeout=max(.01, deadline - time.monotonic())) != 0 or not state["done"] or state["turns"] != 1:
                    raise _error(state["last_error_category"] or "RESPONSE_FORMAT_INVALID")
                if not state["messages"] or not isinstance(state["messages"][-1], str):
                    raise _error()
                envelope = json.loads(state["messages"][-1])
                if set(envelope) != {"result_json"} or not isinstance(envelope["result_json"], str):
                    raise _error()
                raw = json.loads(envelope["result_json"])
                if not isinstance(raw, dict):
                    raise _error()
                usage = {k: state["usage"][k] for k in ("input_tokens", "output_tokens")
                    if type(state["usage"].get(k)) is int and state["usage"][k] >= 0}
                return raw, usage
            except (ValueError, TypeError, KeyError, AttributeError):
                raise _error() from None
            except subprocess.TimeoutExpired:
                raise _error("TIMEOUT", TimeoutError()) from None
            finally:
                state["exit_code_before_cleanup"] = process.poll()
                # 子进程退出也清理同组残留；人工中断仍由原C执行器保存记账与中断记录。
                stop_process(process)
                if writer is not None:
                    writer.join(timeout=2)
                    if not process.stdin.closed:
                        process.stdin.close()
                reader.close()
                process.stdout.close()


def probe(model, *, live=False, transport=None):
    """固定假问题的单次连通性探针；默认零调用，显式live才调用一次且不创建实验。"""
    spec = inspect(model, **({"transport": transport} if transport is not None else {}))
    result = {"stage": "CHECK_PASSED", "provider": "codex-login", "profile": profile(spec, 3000),
        "requests_used": 0, "model_calls": 0, "probe_only": True, "automatically_adopted": False}
    if not live:
        return result
    client, started = CodexLoginTransport(spec), time.monotonic()
    result.update(requests_used=1, model_calls=1, known_tokens={}, token_usage_complete=False)
    try:
        raw, usage = client.complete_json_with_usage(system_prompt="只返回JSON对象，probe_ok字段必须为true，不调用工具。",
            user_payload={"output_schema": {"type": "object", "properties": {"probe_ok": {"type": "boolean"}},
                "required": ["probe_ok"], "additionalProperties": False}}, max_output_tokens=3000)
        result["known_tokens"] = usage
        result["token_usage_complete"] = all(k in usage for k in ("input_tokens", "output_tokens"))
        if not isinstance(raw, dict) or set(raw) != {"probe_ok"} or raw["probe_ok"] is not True:
            raise _error()
        result["stage"] = "COMPLETED"
    except KeyboardInterrupt:
        result["stage"] = "INTERRUPTED"
    except Exception as exc:
        diagnostic = exc.diagnostic if isinstance(exc, LLMResponseError) and exc.diagnostic else make_diagnostic(
            category="INTERNAL_ERROR", stage="LLM_REQUEST", exc=exc)
        result.update(stage="FAILED", diagnostic=diagnostic.model_dump(mode="json"))
    result.update(duration_ms=max(0, int((time.monotonic() - started) * 1000)), codex_event_diagnostic=client.diagnostic_summary())
    return result
