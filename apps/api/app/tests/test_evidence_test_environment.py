"""保护独立测试环境不能继承旧数据库、旧目录或越过建表目标校验。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("evidence_test_launcher", REPO / "scripts/run_evidence_test.py")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


def _env_file(directory: Path, *, database="file_agent_evidence_test_isolated", uploads="./uploads"):
    """写不含真实连接信息的独立配置，测试不连接数据库或模型。"""
    directory.joinpath(".env").write_text("\n".join([
        f"DATABASE_URL=postgresql://fake:fake@localhost/{database}",
        f"FILE_STORAGE_ROOT={uploads}", "MANAGED_ROOT_ARCHIVE_WRITE_PATH=./originals",
        "WORKING_COPY_STORAGE_ROOT=./working", "TRASH_STORAGE_ROOT=./trash", "LOG_DIR=./logs",
        "LLM_ENABLED=true", "EVIDENCE_EVALUATION_MODE=shadow",
    ]), encoding="utf-8")


def test_launcher_drops_inherited_application_configuration():
    """旧终端中残留的数据库、密钥和受管根绝不能带进新测试进程。"""
    actual = launcher.isolated_environment({
        "PATH": "/bin", "LANG": "en_US.UTF-8", "DATABASE_URL": "old-database",
        "MANAGED_ROOT_OLD": "/old/files", "LLM_API_KEY": "old-key",
        "WORKING_COPY_STORAGE_ROOT": "/old/working", "GRAPH_CLASSIFICATION_ENABLED": "true",
        "EVIDENCE_HARNESS_PROVIDER": "codex-login", "EVIDENCE_HARNESS_CODEX_MODEL": "OLD_MODEL",
        "EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE": "true",
    })
    assert actual["PATH"] == "/bin"
    assert not {"DATABASE_URL", "MANAGED_ROOT_OLD", "LLM_API_KEY", "WORKING_COPY_STORAGE_ROOT",
                "GRAPH_CLASSIFICATION_ENABLED", "EVIDENCE_HARNESS_PROVIDER", "EVIDENCE_HARNESS_CODEX_MODEL",
                "EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE"}.intersection(actual)


@pytest.mark.parametrize("action", ["check", "migrate", "api", "lifecycle", "analysis", "frozen", "evaluate", "harness"])
def test_old_database_is_rejected_before_any_action(tmp_path, action):
    """即使误改独立env指向旧库，所有动作也必须在连接/写入/启动前拒绝。"""
    _env_file(tmp_path, database="fileAgent")
    result = subprocess.run([sys.executable, "-c", launcher.command_code(action)],
                            cwd=tmp_path, env=launcher.isolated_environment(dict(os.environ)),
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "DATABASE_URL必须指向独立" in result.stderr
    assert "fake:fake" not in result.stderr


def test_test_environment_cannot_point_at_old_storage(tmp_path):
    """数据库独立还不够，文件存储也必须限定在独立测试目录内。"""
    _env_file(tmp_path, uploads=str(tmp_path.parent / "old-uploads"))
    result = subprocess.run([sys.executable, "-c", launcher.command_code("check")],
                            cwd=tmp_path, env=launcher.isolated_environment(dict(os.environ)),
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "所有存储和日志目录必须位于独立测试目录" in result.stderr


def test_missing_standalone_env_does_not_fall_back(monkeypatch, tmp_path):
    """缺独立配置必须退出，不能向上查找到真实根.env后继续运行。"""
    monkeypatch.setattr(launcher, "TEST_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_evidence_test.py", "check"])
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 2


@pytest.mark.parametrize("action", ["api", "analysis"])
def test_capture_is_explicit_for_both_request_and_async_processes(action):
    code = launcher.command_code(action, capture_plan="/controlled/test/plan.json")
    assert "EVIDENCE_EVALUATION_CAPTURE_PLAN" in code
    assert "EVIDENCE_EVALUATION_CAPTURE_PLAN" not in launcher.command_code(action)


def test_frozen_evaluation_loads_only_isolated_env_and_forwards_literal_arguments(tmp_path):
    _env_file(tmp_path)
    code = launcher.command_code("evaluate", ["--help"])
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
        env=launcher.isolated_environment({**os.environ, "LLM_API_KEY": "MUST_NOT_INHERIT"}),
        capture_output=True, text=True)
    assert result.returncode == 0
    assert "--dataset" in result.stdout
    assert "MUST_NOT_INHERIT" not in result.stdout + result.stderr


def test_harness_selection_uses_standalone_env_without_inheriting_terminal_values(tmp_path):
    """启动器丢弃终端旧选择，先加载独立env再解析，不连接数据库或模型。"""
    import json
    _env_file(tmp_path)
    with (tmp_path / ".env").open("a") as file:
        file.write("\nEVIDENCE_HARNESS_PROVIDER=codex-login\nEVIDENCE_HARNESS_CODEX_MODEL=FROZEN_FAKE_MODEL\n"
                   "EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE=true\n")
    code = launcher.PREFLIGHT + "\nfrom app.modules.evidence_harness.config import validation_provider_options\nimport json\nprint(json.dumps(validation_provider_options()))\n"
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
        env=launcher.isolated_environment({**os.environ, "EVIDENCE_HARNESS_PROVIDER": "openai-compatible",
            "EVIDENCE_HARNESS_CODEX_MODEL": "OLD_MODEL", "EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE": "false"}),
        capture_output=True, text=True)
    assert result.returncode == 0
    options, sources = json.loads(result.stdout)
    assert options == {"provider": "codex-login", "codex_model": "FROZEN_FAKE_MODEL", "allow_model_profile_change": True}
    assert set(sources.values()) == {"ENV"}
