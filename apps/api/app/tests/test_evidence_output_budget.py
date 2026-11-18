"""保护输出额度对照仅改一个变量，默认不外发真实文件内容。"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "budget_launcher", REPO / "scripts/run_evidence_output_budget.py"
)
budget_launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(budget_launcher)


@pytest.mark.parametrize("payload", [
    {"max_output_tokens": 6000, "timeout_seconds": 120},
    {"max_output_tokens": True}, {"max_output_tokens": 8001},
    {"max_output_tokens": 3000}, {"max_output_tokens": "6000"}, [],
])
def test_config_rejects_extra_variables_and_invalid_budgets(tmp_path, payload):
    """不能通过实验配置夹带第二个变量或触发配置层静默截断。"""
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        budget_launcher.read_budget(path)


@pytest.mark.parametrize("run", [False, True])
def test_live_evaluation_requires_explicit_run_flag(monkeypatch, tmp_path, run):
    """默认只检查；只有显式参数才能启用评测分支，且两种模式均隔离终端密钥。"""
    path = tmp_path / "experiment.json"
    path.write_text('{"max_output_tokens":6000}')
    (tmp_path / ".env").write_text("")
    monkeypatch.setattr(budget_launcher, "CONFIG", path)
    monkeypatch.setattr(budget_launcher, "TEST_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_evidence_output_budget.py", *(["--run"] if run else [])])
    captured = {}

    def fake_run(argv, **kwargs):
        """替代子进程，避免测试接触真实配置和外部服务。"""
        captured.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(budget_launcher.subprocess, "run", fake_run)
    monkeypatch.setenv("LLM_API_KEY", "MUST_NOT_INHERIT")
    with pytest.raises(SystemExit) as error:
        budget_launcher.main()
    assert error.value.code == 0
    assert f"if not {run!r}:" in captured["argv"][2]
    assert captured["env"]["EVIDENCE_EVALUATION_MAX_OUTPUT_TOKENS"] == "6000"
    assert "LLM_API_KEY" not in captured["env"]


def test_guard_requires_exact_dataset_config_and_new_report():
    """生成的检查保留基线散列和单变量门槛，不能覆盖旧报告。"""
    code = budget_launcher.experiment_code(6000, run=True)
    compile(code, "budget-experiment", "exec")
    assert "changes != ['max_output_tokens']" in code
    assert "baseline['dataset_sha256'] != digest" in code
    assert "if output.exists():" in code
    assert "'--repetitions', '3'" in code
