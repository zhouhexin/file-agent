"""独立测试代理验收：显式测试配置生效，不污染API/worker或外发认证信息。"""

import json
import os
import subprocess
import sys

import pytest

from app.tests.test_evidence_test_environment import launcher, _env_file


def test_test_env_proxy_reaches_child_and_overrides_both_cases(tmp_path, monkeypatch):
    """真实子进程先读独立env，再覆盖冲突的终端代理，不访问网络或启动模型。"""
    _env_file(tmp_path)
    with (tmp_path / '.env').open('a') as f:
        f.write('\nEVIDENCE_HARNESS_PROXY_URL=http://127.0.0.1:7897\n')
    # Replace only the final dispatch, preserving the real env and proxy preflight.
    monkeypatch.setattr(launcher, 'handoff', lambda args: '\nimport json\nprint(json.dumps({k:os.environ.get(k) for k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy")}))\n')
    code = launcher.command_code('harness', ['--help'])
    env = launcher.isolated_environment({**os.environ, 'HTTPS_PROXY': 'http://old.invalid:1234', 'https_proxy': 'http://old.invalid:4567'})
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout).values()) == {'http://127.0.0.1:7897'}


@pytest.mark.parametrize('value', ['', 'file:///private/path', 'http://host:bad', 'http://host:0', 'http://host:70000', 'http://host:7897/path', 'http://host:7897?secret=PRIVATE_SENTINEL'])
def test_invalid_proxy_fails_without_echoing_value(monkeypatch, value):
    """配置出错时只返回固定错误，URL中的口令、路径及查询不得出现在异常中。"""
    monkeypatch.setenv('EVIDENCE_HARNESS_PROXY_URL', value)
    with pytest.raises(SystemExit) as e:
        exec(launcher.HARNESS_NETWORK_PREFLIGHT, {'os': os})
    assert 'HARNESS_PROXY_URL_INVALID' in str(e.value) and 'PRIVATE_SENTINEL' not in str(e.value)


def test_absent_proxy_preserves_terminal_and_other_actions(monkeypatch):
    """未设置新配置时保持原行为；其他测试服务不隐式修改代理。"""
    monkeypatch.delenv('EVIDENCE_HARNESS_PROXY_URL', raising=False)
    monkeypatch.setenv('HTTPS_PROXY', 'http://existing.invalid:7890')
    exec(launcher.HARNESS_NETWORK_PREFLIGHT, {'os': os})
    assert os.environ['HTTPS_PROXY'] == 'http://existing.invalid:7890'
    for action in ('api','analysis','lifecycle','evaluate','frozen','check'):
        assert 'EVIDENCE_HARNESS_PROXY_URL' not in launcher.command_code(action)
