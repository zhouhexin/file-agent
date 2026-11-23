"""Codex登录代理验收：Fake CLI替代网络，保护认证、Gold、用量和进程清理边界。"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.modules.evidence_harness.improvement import codex_backend as codex
from app.modules.llm.client import LLMResponseError


@pytest.fixture(autouse=True)
def offline_database_configuration(monkeypatch):
    """Importing planning modules must not depend on a developer's real .env."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://offline:offline@127.0.0.1/file_agent_unit_tests")


def fake_spec():
    """仅用现有Python模拟CLI，不访问真实Codex认证或模型。"""
    return {"binary": str(Path(sys.executable).resolve()), "binary_ref": hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
        "version": "codex-cli 0.162.1", "model": "fake-model"}


def fake_cli(tmp_path, monkeypatch, kind="normal", *, os_sandbox=False):
    """Fake进程仍经真实stdin/stdout与生命周期管理；专项保留实际OS限制。"""
    script = tmp_path / "fake_codex.py"
    args_path = tmp_path / "arguments.json"
    body = '''import json,os,sys,time
from pathlib import Path
kind=KIND
Path(ARGUMENTS).write_text(json.dumps({'argv':sys.argv[1:],'env_keys':list(os.environ)}))
request=json.loads(sys.stdin.read().split('\\n',1)[1])
def emit(event):
 print(json.dumps(event),flush=True)
emit({'type':'thread.started','thread_id':'fake'})
if kind in ('startup_notices','notice_tool','notice_failed','notice_incomplete'):
 emit({'type':'item.completed','item':{'type':'error','message':'Under-development features enabled: skip_host_skill_discovery. PRIVATE_SENTINEL'}})
 emit({'type':'item.completed','item':{'type':'error','message':'Model metadata for fake-model not found. PRIVATE_SENTINEL'}})
emit({'type':'turn.started'})
if kind.startswith('reconnect_'):
 emit({'type':'error','message':'Reconnecting... 1/5 (stream disconnected). PRIVATE_SENTINEL'})
if kind=='reconnect_failed': emit({'type':'turn.failed','error':{'message':'connection failed. PRIVATE_SENTINEL'}})
if kind=='wait': time.sleep(120)
if kind=='extra_turn': emit({'type':'turn.started'})
if kind in ('tool','notice_tool'): emit({'type':'item.started','item':{'type':'command_execution','command':'PRIVATE_SENTINEL'}})
if kind=='notice_failed': emit({'type':'turn.failed','error':{'message':'The model is not supported. PRIVATE_SENTINEL'}})
if kind=='quota': emit({'type':'turn.failed','error':{'code':'usage_limit_reached','message':'PRIVATE_SENTINEL'}})
if kind=='broken':
 print('PRIVATE_SENTINEL',flush=True)
 sys.exit(0)
if kind=='os_probe':
 try:
  Path(PROTECTED).read_bytes()
  raise RuntimeError('Gold readable')
 except PermissionError: pass
 try:
  import subprocess
  subprocess.run(['/bin/echo','not-allowed'],check=True)
  raise RuntimeError('Exec allowed')
 except PermissionError: pass
raw={'decision_type':'TOOL_PLAN','intent':'EVIDENCE_ANSWER'}
message=json.dumps({'result_json':json.dumps(raw)})
if kind=='wrong_envelope': message=json.dumps({'result_json':42})
if kind=='non_object': message=json.dumps({'result_json':'[]'})
emit({'type':'item.completed','item':{'type':'agent_message','text':message}})
if kind not in ('no_completion','notice_incomplete','reconnect_missing'):
 emit({'type':'turn.completed','usage':{} if kind=='unknown_usage' else {'input_tokens':20,'output_tokens':7}})
if kind in ('exit_failure','reconnect_exit'): sys.exit(2)
'''
    body = body.replace("KIND", repr(kind)).replace("ARGUMENTS", repr(str(args_path))).replace("PROTECTED", repr(str(codex.PROJECT_ROOT / "agent.md")))
    script.write_text(body)
    original = codex._command
    commands, processes = [], []

    def command(spec, scratch, schema):
        """只替换CLI可执行入口，生产参数仍由原构造器生成并接受断言。"""
        args = original(spec, scratch, schema)
        commands.append(args)
        return args[:4] + [str(script)] + args[4:] if os_sandbox else [spec["binary"], str(script), *args[4:]]

    original_popen = subprocess.Popen

    def popen(*a, **kw):
        """捕获真实进程对象，检查超时或中断之后没有残留。"""
        p = original_popen(*a, **kw)
        processes.append(p)
        return p

    monkeypatch.setattr(codex, "_command", command)
    monkeypatch.setattr(codex.subprocess, "Popen", popen)
    return codex.CodexLoginTransport(fake_spec()), commands, processes, args_path


def test_fake_codex_parameters_json_usage_and_no_credentials(tmp_path, monkeypatch):
    """Prompt走stdin，忽略旧配置，所有认证和数据库环境变量不交付CLI。"""
    monkeypatch.setenv("LLM_API_KEY", "PRIVATE_SENTINEL")
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE_SENTINEL")
    monkeypatch.setenv("CODEX_HOME", "PRIVATE_SENTINEL")
    client, commands, processes, args_path = fake_cli(tmp_path, monkeypatch)
    raw, usage = client.complete_json_with_usage(system_prompt="PRIVATE_PROMPT", user_payload={"output_schema": {}}, max_output_tokens=3000)
    assert raw["intent"] == "EVIDENCE_ANSWER" and usage == {"input_tokens": 20, "output_tokens": 7}
    args = json.loads(args_path.read_text())
    assert not {"LLM_API_KEY", "OPENAI_API_KEY", "CODEX_HOME", "DATABASE_URL"} & set(args["env_keys"])
    cmd = commands[0]
    for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--no-daemon", "--json"):
        assert flag in cmd
    assert "PRIVATE_PROMPT" not in str(cmd) and "PRIVATE_SENTINEL" not in str(cmd)
    assert 'model_provider="openai"' in cmd and 'forced_login_method="chatgpt"' in cmd
    # 内置Provider不允许覆盖；实际不支持的内部重试额度必须如实标记未知。
    assert not any('model_providers.openai.' in v for v in cmd)
    assert "shell_tool" in cmd and "multi_agent" in cmd and "plugins" in cmd
    assert all(p.poll() is not None for p in processes)
    p = codex.profile(fake_spec(), 3000)
    assert not p["output_token_limit_enforced"] and p["request_count_unit"] == "CODEX_INVOCATIONS"
    assert p["max_retries"] is None and p["host_retries"] == 0 and not p["provider_retries_observable"]


@pytest.mark.parametrize("kind", ["tool", "extra_turn", "no_completion", "broken", "wrong_envelope", "non_object", "exit_failure", "quota"])
def test_codex_failures_are_rejected_without_private_error_text(tmp_path, monkeypatch, kind):
    """无工具单轮和明确完成事件是成功前提；服务端自由文本不得进入诊断。"""
    client, _, processes, _ = fake_cli(tmp_path, monkeypatch, kind)
    with pytest.raises(LLMResponseError) as exc:
        client.complete_json_with_usage(system_prompt="original", user_payload={}, max_output_tokens=3000)
    assert "PRIVATE_SENTINEL" not in str(exc.value) + exc.value.diagnostic.model_dump_json()
    if kind == "quota": assert exc.value.diagnostic.category == "QUOTA_EXHAUSTED"
    assert all(p.poll() is not None for p in processes)


def test_codex_unknown_usage_is_not_reported_as_zero(tmp_path, monkeypatch):
    """未提供Token用量时保持未知，让原Journal统一记账。"""
    client, *_ = fake_cli(tmp_path, monkeypatch, "unknown_usage")
    assert client.complete_json_with_usage(system_prompt="original", user_payload={}, max_output_tokens=3000)[1] == {}


@pytest.mark.parametrize("kind", ["reconnect_ok", "reconnect_failed", "reconnect_missing", "reconnect_exit"])
def test_error_notification_waits_for_actual_terminal_result(tmp_path, monkeypatch, kind):
    """官方error通知后进程仍可运行；恢复可成功，真失败/缺终态/非零退出不能伪成功。"""
    client, *_ = fake_cli(tmp_path, monkeypatch, kind)
    if kind == "reconnect_ok":
        raw, usage = client.complete_json_with_usage(system_prompt="original", user_payload={}, max_output_tokens=3000)
        assert raw["intent"] == "EVIDENCE_ANSWER" and usage["output_tokens"] == 7
    else:
        with pytest.raises(LLMResponseError):
            client.complete_json_with_usage(system_prompt="original", user_payload={}, max_output_tokens=3000)
    summary = client.diagnostic_summary()
    assert summary["error_event_count"] == 1
    assert "CONNECTION_HINT" in summary["hints"] and "PRIVATE_SENTINEL" not in json.dumps(summary)


def test_https_reuses_chatgpt_auth_disables_websocket_and_binds_profile(tmp_path, monkeypatch):
    """HTTPS模式仅改变原生CLI连接配置，不提取token、设置外部网关或修改全局文件。"""
    client, commands, *_ = fake_cli(tmp_path, monkeypatch)
    client.spec["transport"] = "https"
    client.complete_json_with_usage(system_prompt="original", user_payload={}, max_output_tokens=3000)
    args = commands[0]
    assert 'model_provider="file_agent_https"' in args
    assert 'model_providers.file_agent_https.requires_openai_auth=true' in args
    assert 'model_providers.file_agent_https.supports_websockets=false' in args
    assert 'model_providers.file_agent_https.request_max_retries=0' in args
    assert 'forced_login_method="chatgpt"' in args
    assert not any('base_url=' in x or 'env_key=' in x or 'model_providers.openai.' in x for x in args)
    profile = codex.profile(client.spec, 3000)
    assert profile["transport"] == "HTTPS_SSE" and profile["adapter_ref"] == codex.HTTPS_POLICY_REF
    assert profile["max_retries"] == 0
    assert "transport" not in codex.profile(fake_spec(), 3000)


def test_startup_nonfatal_notices_are_not_tools_and_are_safely_recorded(tmp_path, monkeypatch):
    """复现真实CLI在turn.started前的error提示，保留正常JSON完成与用量。"""
    from app.modules.evidence_harness.improvement.executor import Journal
    from app.modules.evidence_harness.improvement.adaptive_validation import ValidationAuthorization
    client, *_ = fake_cli(tmp_path, monkeypatch, "startup_notices")
    journal = Journal(tmp_path / "journal", ValidationAuthorization(max_total_requests=1))
    raw = journal.call(client, role="PLANNER", round_id=1, arm="control", case_id="FAKE", repetition=1,
        system_prompt="original", user_payload={})
    assert raw["intent"] == "EVIDENCE_ANSWER"
    record = json.loads((tmp_path / "journal/calls/0001.finished.json").read_text())
    summary = record["codex_event_diagnostic"]
    assert record["status"] == "COMPLETED" and record["output_tokens"] == 7
    assert summary["startup_notice_count"] == 2 and summary["completion_event_received"]
    assert set(summary["hints"]) == {"UNSTABLE_FEATURE_NOTICE", "MODEL_CATALOG_ENTRY_MISSING"}
    assert "PRIVATE_SENTINEL" not in json.dumps(record)


@pytest.mark.parametrize("kind", ["notice_tool", "notice_failed", "notice_incomplete"])
def test_notices_do_not_hide_tools_fatal_failure_or_missing_completion(tmp_path, monkeypatch, kind):
    """启动提示可以存在，真实工具、真正失败和未完成仍须拒绝，不能为通过测试放宽完成门槛。"""
    from app.modules.evidence_harness.improvement.executor import Journal
    from app.modules.evidence_harness.improvement.adaptive_validation import ValidationAuthorization
    client, *_ = fake_cli(tmp_path, monkeypatch, kind)
    journal = Journal(tmp_path / "journal", ValidationAuthorization(max_total_requests=1))
    with pytest.raises(LLMResponseError):
        journal.call(client, role="PLANNER", round_id=1, arm="control", case_id="FAKE", repetition=1,
            system_prompt="original", user_payload={})
    record = json.loads((tmp_path / "journal/calls/0001.finished.json").read_text())
    assert record["status"] == "FAILED" and record["output_tokens"] is None
    assert record["codex_event_diagnostic"]["startup_notice_count"] == 2
    assert "PRIVATE_SENTINEL" not in json.dumps(record)
    if kind == "notice_tool":
        assert record["diagnostic"]["category"] == "REQUEST_REJECTED"
        assert "TOOL_EVENT_REJECTED" in record["codex_event_diagnostic"]["hints"]


@pytest.mark.parametrize("live,kind", [(False, "normal"), (True, "normal"), (True, "failure"), (True, "wrong_json")])
def test_single_probe_requires_live_and_never_retries(monkeypatch, live, kind):
    """单次探针只有明确live才发起一次Fake模型调用，不读取题库、Gold或创建实验。"""
    monkeypatch.setattr(codex, "inspect", lambda model: fake_spec())
    calls = []

    class Client:
        """确定性探针Provider只返回固定JSON或模拟故障。"""
        def __init__(self, spec):
            pass

        def complete_json_with_usage(self, **kw):
            """失败不重试，不把任意模型结果回显到诊断。"""
            calls.append(kw)
            if kind == "failure":
                raise codex._error("PROVIDER_ERROR")
            return ({"probe_ok": True} if kind == "normal" else {"PRIVATE_SENTINEL": "bad"}), {"input_tokens": 4, "output_tokens": 3}

        def diagnostic_summary(self):
            """Fake只提供固定枚举，不返回错误正文。"""
            return {"phase": "TURN_ACTIVE"}

    monkeypatch.setattr(codex, "CodexLoginTransport", Client)
    result = codex.probe("fake-model", live=live)
    assert len(calls) == result["requests_used"] == int(live)
    assert result["stage"] == ("CHECK_PASSED" if not live else "COMPLETED" if kind == "normal" else "FAILED")
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


@pytest.mark.parametrize("live", [False, True])
def test_probe_cli_loads_env_and_preserves_explicit_live(monkeypatch, capsys, live):
    """CLI探针沿用env且不暗中启动测试；只把明确live交给可信探针。"""
    from app.core import config
    from app.scripts import evidence_harness
    monkeypatch.setenv("EVIDENCE_HARNESS_PROVIDER", "codex-login")
    monkeypatch.setenv("EVIDENCE_HARNESS_CODEX_MODEL", "fake-model")
    calls = []

    def probe(model, **kw):
        """只观察CLI选择，不运行本机认证或外部模型。"""
        calls.append((model, kw))
        return {"stage": "COMPLETED" if kw["live"] else "CHECK_PASSED", "model_calls": int(kw["live"])}
    monkeypatch.setattr(codex, "probe", probe)
    monkeypatch.setattr(sys, "argv", ["harness", "codex-probe", *(["--live"] if live else [])])
    loader = config.get_settings
    try:
        config.get_settings = lambda: SimpleNamespace(llm_enabled=True)
        evidence_harness.main()
    finally:
        config.get_settings = loader
    result = json.loads(capsys.readouterr().out)
    assert result["model_calls"] == int(live) and calls == [("fake-model", {"live": live})]
    assert result["configuration_sources"]["provider"] == "ENV"


@pytest.mark.parametrize("kind", ["timeout", "interrupt"])
def test_codex_timeout_and_interrupt_kill_process_group(tmp_path, monkeypatch, kind):
    """不等待真实60秒，验证取消控制能终止CLI且异常不吞掉用户中断。"""
    client, _, processes, _ = fake_cli(tmp_path, monkeypatch, "wait")
    if kind == "timeout":
        from itertools import chain, repeat
        ticks = chain([0], repeat(61))
        monkeypatch.setattr(codex.time, "monotonic", lambda: next(ticks))
        expected = LLMResponseError
    else:
        def interrupt(*a, **kw):
            """模拟用户取消正在读取的模型请求。"""
            raise KeyboardInterrupt
        monkeypatch.setattr(codex.PipeReader, "read", interrupt)
        expected = KeyboardInterrupt
    with pytest.raises(expected):
        client.complete_json_with_usage(system_prompt="original", user_payload={}, max_output_tokens=3000)
    assert all(p.poll() is not None for p in processes)


@pytest.mark.parametrize("login", [b"Logged in using ChatGPT", b"Logged in using API key", b"Not logged in"])
def test_login_preflight_only_inspects_cli_status(monkeypatch, login):
    """只允许已登录ChatGPT，不能偷偷沿用旧API认证；检查本身不调用模型。"""
    spec = fake_spec()
    monkeypatch.setattr(codex.sys, "platform", "darwin")
    monkeypatch.setattr(codex, "_mac_runtime_available", lambda: True)
    calls = []
    monkeypatch.setattr(codex.shutil, "which", lambda _: spec["binary"])

    def run(cmd, **kw):
        """仅模拟版本和认证状态命令，不提供任何认证token。"""
        calls.append(cmd)
        return SimpleNamespace(stdout=b"codex-cli 0.162.1" if cmd[-1] == "--version" else login, stderr=b"")
    monkeypatch.setattr(codex.subprocess, "run", run)
    if b"ChatGPT" in login:
        assert codex.inspect("fake-model")["binary_ref"] == spec["binary_ref"]
    else:
        with pytest.raises(ValueError, match="CODEX_CHATGPT_LOGIN_REQUIRED"):
            codex.inspect("fake-model")
    assert len(calls) == 2 and all("exec" not in c for c in calls)


@pytest.mark.skipif(os.environ.get("HARNESS_OS_ACCEPTANCE") != "1" or sys.platform != "darwin", reason="macOS专项；Windows使用容器专项")
def test_os_codex_fake_cli_cannot_read_gold_or_execute_other_programs(tmp_path, monkeypatch):
    """实际OS限制作用于CLI本身，Fake验证项目/Gold不可读与外部程序不可执行。"""
    client, *_ = fake_cli(tmp_path, monkeypatch, "os_probe", os_sandbox=True)
    client.spec["transport"] = "https"
    raw, usage = client.complete_json_with_usage(system_prompt="original", user_payload={}, max_output_tokens=3000)
    assert raw["intent"] == "EVIDENCE_ANSWER" and usage["output_tokens"] == 7
