"""保护 WorkBuddy 附件桥接探针的本地边界测试。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


def _load_probe_module():
    """从插件源目录加载探针，保证测试覆盖实际由 Hook 执行的脚本。"""

    script = (
        Path(__file__).resolve().parents[3]
        / "integrations"
        / "workbuddy"
        / "file-agent-plugin"
        / "scripts"
        / "capture_submission.py"
    )
    spec = importlib.util.spec_from_file_location("workbuddy_bridge_probe", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_attachment_skill_drives_external_ocr_instead_of_polling() -> None:
    """附件 Skill 必须领取、识图并回填，不能把等待状态误当成可轮询完成。"""

    skill = (
        Path(__file__).resolve().parents[3]
        / "integrations"
        / "workbuddy"
        / "file-agent-plugin"
        / "skills"
        / "file-agent"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "WAITING_EXTERNAL_EXTRACTION" in skill
    assert "extraction_claim" in skill
    assert "ocr.extract" in skill
    assert "--accurate --positions" in skill
    assert "extraction_submit" in skill
    assert "仅重复 `batch_get` 或 `job_get` 不会完成提取" in skill
    assert "不得要求文件助手后端临时自行 OCR" in skill
    assert "`confidence`" in skill and "填 `null`" in skill


def test_marketplace_0110_requires_duplicate_comparison_link_without_cache_files() -> None:
    """0.1.10 交付包必须先展示重复对比链接，且不混入本机 Python 缓存。"""

    marketplace = (
        Path(__file__).resolve().parents[3]
        / "integrations"
        / "workbuddy"
        / "file-agent-marketplace-0.1.10.zip"
    )
    with zipfile.ZipFile(marketplace) as archive:
        names = set(archive.namelist())
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
        manifest = json.loads(
            archive.read("file-agent-plugin/.codebuddy-plugin/plugin.json")
        )
        skill = archive.read(
            "file-agent-plugin/skills/file-agent/SKILL.md"
        ).decode("utf-8")
    assert manifest["version"] == "0.1.10"
    assert "extraction_claim" in skill
    assert "ocr.extract" in skill
    assert "extraction_submit" in skill
    assert "请用文件助手归档并分类本轮附件" in skill
    assert ".doc/.docx/.pdf/.xls/.xlsx/.txt" in skill
    assert "primary_category.category_path" in skill
    assert "classification_outcome" in skill
    assert "本工具回执不展示分类证据或关键词" in skill
    assert "重复文件对比强制流程" in skill
    assert "必须在询问用户决定前调用" in skill
    assert "duplicate_comparison_get 返回的 comparison_url" in skill
    assert "[打开“<候选文件名>”对比页面]" in skill
    assert "不得声称插件已经替用户自动打开浏览器窗口" in skill


def test_capture_creates_pending_reference_and_never_exposes_path(monkeypatch, tmp_path) -> None:
    """Hook 只注入不透明待解析引用，路径和文件名不进入模型上下文。"""

    home = tmp_path / ".workbuddy"
    transcript = home / "projects" / "workspace" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    blob_path = home / "blobs" / "aa" / "hash.jpg"
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(b"not-read-by-probe")
    transcript.write_text(
        json.dumps(
            {
                "id": "message-1",
                "role": "user",
                "sessionId": "session-1",
                "content": [
                    {"type": "input_text", "text": "请归档图片"},
                    {
                        "type": "image_blob_ref",
                        "blob_id": "blob-1",
                        "blob_path": str(blob_path),
                        "original_filename": "secret-name.jpg",
                    },
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FILE_AGENT_WORKBUDDY_HOME", str(home))
    state_dir = tmp_path / "plugin-data"
    monkeypatch.setenv("CODEBUDDY_PLUGIN_DATA", str(state_dir))
    module = _load_probe_module()

    result = module.capture_submission(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "transcript_path": str(transcript),
            "prompt": "请归档图片",
        }
    )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "FILE_AGENT_HOST_SUBMISSION ref=wbsub_v1_" in context
    assert "请用文件助手归档并分类本轮附件" in context
    assert str(blob_path) not in context
    assert "secret-name.jpg" not in context
    # 诊断只记录状态码，不能把附件标识、名称、缓存路径或任务文字写入日志。
    diagnostic = (state_dir / "capture-diagnostics.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line)["status"] for line in diagnostic.splitlines()] == [
        "PENDING_MANIFEST_CREATED"
    ]
    assert "secret-name.jpg" not in diagnostic
    assert str(blob_path) not in diagnostic
    assert "请归档图片" not in diagnostic


def test_probe_rejects_transcript_outside_workbuddy_projects(monkeypatch, tmp_path) -> None:
    """Hook 不得借 transcript_path 读取 WorkBuddy 数据根以外的任意 JSONL 文件。"""

    home = tmp_path / ".workbuddy"
    (home / "projects").mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("FILE_AGENT_WORKBUDDY_HOME", str(home))
    module = _load_probe_module()

    result = module.capture_submission(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "transcript_path": str(outside),
            "prompt": "请归档",
        }
    )
    assert "FILE_AGENT_ATTACHMENT_BRIDGE_UNAVAILABLE" in result["hookSpecificOutput"]["additionalContext"]
    assert str(outside) not in result["hookSpecificOutput"]["additionalContext"]


def test_probe_creates_pending_reference_without_reading_old_attachment(monkeypatch, tmp_path) -> None:
    """Hook 直接签发待解析引用，不能读取或冻结历史附件。"""

    home = tmp_path / ".workbuddy"
    transcript = home / "projects" / "workspace" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    blob = home / "blobs" / "aa" / "hash.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"image")
    transcript.write_text(
        json.dumps(
            {
                "id": "old-message",
                "role": "user",
                "sessionId": "session-1",
                "content": [
                    {"type": "input_text", "text": "历史任务"},
                    {
                        "type": "image_blob_ref",
                        "blob_id": "blob-1",
                        "blob_path": str(blob),
                        "original_filename": "private.jpg",
                    },
                ],
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    state = tmp_path / "plugin-data"
    monkeypatch.setenv("FILE_AGENT_WORKBUDDY_HOME", str(home))
    monkeypatch.setenv("CODEBUDDY_PLUGIN_DATA", str(state))

    result = _load_probe_module().capture_submission(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "transcript_path": str(transcript),
            "prompt": "本轮归档",
        }
    )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "FILE_AGENT_HOST_SUBMISSION ref=wbsub_v1_" in context
    assert "count=pending" in context
    manifests = list((state / "submissions").glob("wbsub_v1_*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "PENDING_TRANSCRIPT"
    assert manifest["attachments"] == []
    assert manifest["prompt"] == "本轮归档"
    diagnostic_lines = (state / "capture-diagnostics.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["status"] for line in diagnostic_lines] == [
        "PENDING_MANIFEST_CREATED"
    ]
    diagnostic = "\n".join(diagnostic_lines)
    assert "历史任务" not in diagnostic
    assert "private.jpg" not in diagnostic
    assert str(blob) not in diagnostic


def test_probe_normalizes_utf16_surrogate_pair_before_writing_manifest(
    monkeypatch, tmp_path
) -> None:
    """WorkBuddy 以 JSON 代理对传入 emoji 时，Hook 仍能写出合法 UTF-8 提交单。"""

    home = tmp_path / ".workbuddy"
    transcript = home / "projects" / "workspace" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    state = tmp_path / "plugin-data"
    monkeypatch.setenv("FILE_AGENT_WORKBUDDY_HOME", str(home))
    monkeypatch.setenv("CODEBUDDY_PLUGIN_DATA", str(state))
    host_prompt = json.loads(r'"\ud83d\udca8 请归档图片"')

    result = _load_probe_module().capture_submission(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-emoji",
            "transcript_path": str(transcript),
            "prompt": host_prompt,
        }
    )

    assert "FILE_AGENT_HOST_SUBMISSION ref=wbsub_v1_" in result["hookSpecificOutput"][
        "additionalContext"
    ]
    manifest_path = next((state / "submissions").glob("wbsub_v1_*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["prompt"] == "💨 请归档图片"


@pytest.mark.parametrize("raw_input", [b'{"prompt":"\xff"}', b'{"prompt":'])
def test_capture_invalid_stdin_never_creates_submission(monkeypatch, tmp_path, raw_input) -> None:
    """无效 UTF-8 或截断 JSON 必须明确失败，不能通过替换字符创建授权。"""

    state = tmp_path / "state"
    module = _load_probe_module()
    result = subprocess.run(
        [sys.executable, module.__file__], input=raw_input, capture_output=True,
        env={**os.environ, "CODEBUDDY_PLUGIN_DATA": str(state), "PYTHONIOENCODING": "gbk"},
        timeout=10, check=True,
    )
    output = json.loads(result.stdout.decode("utf-8"))
    assert "FILE_AGENT_ATTACHMENT_BRIDGE_UNAVAILABLE" in output["hookSpecificOutput"]["additionalContext"]
    assert not list(state.glob("submissions/*.json"))


def test_capture_rejects_isolated_surrogate(monkeypatch, tmp_path) -> None:
    """孤立代理字符代表损坏的任务输入，不能清洗成另一份任务后签发提交单。"""

    workbuddy_root = tmp_path / ".workbuddy"
    transcript = workbuddy_root / "projects" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("FILE_AGENT_WORKBUDDY_HOME", str(workbuddy_root))
    monkeypatch.setenv("CODEBUDDY_PLUGIN_DATA", str(state))
    output = _load_probe_module().capture_submission({
        "hook_event_name": "UserPromptSubmit", "session_id": "s1",
        "transcript_path": str(transcript), "prompt": "归档\udca8",
    })
    assert "FILE_AGENT_ATTACHMENT_BRIDGE_UNAVAILABLE" in output["hookSpecificOutput"]["additionalContext"]
    assert not list(state.glob("submissions/*.json"))
    assert "PENDING_INPUT_ENCODING_INVALID" in (state / "capture-diagnostics.jsonl").read_text(encoding="utf-8")
