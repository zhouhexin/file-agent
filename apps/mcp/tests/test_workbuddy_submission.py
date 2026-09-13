"""WorkBuddy 可信附件提交单测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from file_agent_mcp.attachment_transfer import AttachmentTransferService
from file_agent_mcp.client import WorkBuddyAttachmentRegistry
from file_agent_mcp.workbuddy_submission import (
    WorkBuddySubmissionService,
    _host_document_paths,
    _message_matches_prompt,
)



def test_prompt_match_normalizes_workbuddy_utf16_surrogate_pair() -> None:
    """待解析提交单和 transcript 使用不同 Unicode 表示时仍应匹配同一任务文字。"""

    host_prompt = json.loads(r'"\ud83d\udca8 请归档图片"')
    message = {
        "content": [{"type": "input_text", "text": "💨 请归档图片"}],
    }

    assert _message_matches_prompt(message, host_prompt)


class FakeAttachmentClient:
    """模拟附件批次接口，验证可信提交单复用了现有传输协议。"""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.uploaded: list[tuple[str, Path, str]] = []

    async def create_batch(self, payload: dict) -> dict:
        return {"id": "batch-attachment", "manifest_status": "OPEN"}

    async def append_items(self, *, batch_id: str, items: list[dict]) -> dict:
        self.items = [
            {**item, "id": f"item-{index}", "stage": "RECEIVE", "actual_sha256": None}
            for index, item in enumerate(items, start=1)
        ]
        return {"items": self.items}

    async def seal_batch(self, *, batch_id: str) -> dict:
        return {"id": batch_id}

    async def upload_resolved_file(self, *, batch_id: str, item_id: str, path: Path, filename: str) -> dict:
        self.uploaded.append((item_id, path, filename))
        return {"accepted": True}

    async def batch_get(self, *, batch_id: str) -> dict:
        return {"id": batch_id, "status": "RUNNING"}

    async def batch_items(self, *, batch_id: str) -> list[dict]:
        return self.items


def _document_message(*, path: Path, prompt: str, created_at: float) -> dict:
    """模拟 WorkBuddy 5.5.3 在用户发送本地文档后生成的宿主上下文。"""

    source = str(path)
    return {
        "id": "document-message-1",
        "role": "user",
        "sessionId": "session-1",
        "timestamp": int(created_at * 1000),
        "content": [{
            "type": "input_text",
            "text": (
                '<system-reminder data-role="user-context">\n'
                '<user_references>\n'
                f'{json.dumps(source, ensure_ascii=False)}\n'
                '</user_references>\n'
                '<additional_data><attached_files>\n'
                f'<file path={quoteattr(source)} />\n'
                '</attached_files></additional_data>\n'
                '</system-reminder>\n'
                f'<user_query>@{json.dumps(source, ensure_ascii=False)} {prompt}</user_query>'
            ),
        }],
    }


@pytest.mark.parametrize("suffix", [".doc", ".docx", ".pdf", ".xls", ".xlsx", ".txt"])
def test_host_document_reference_stages_and_ingests_without_model_path(tmp_path, suffix) -> None:
    """文档只从宿主附件区提取，按消息 ID 固定清单并复用既有批次上传。"""

    home = tmp_path / ".workbuddy"
    projects = home / "projects"
    projects.mkdir(parents=True)
    source = tmp_path / f"材料{suffix}"
    source.write_bytes(b"workbuddy-document-byte-snapshot")
    now = time.time()
    transcript = projects / "session.jsonl"
    transcript.write_text(json.dumps(_document_message(path=source, prompt="请归档此文档", created_at=now), ensure_ascii=False) + "\n", encoding="utf-8")
    state = home / "bridge-state"
    submissions = state / "submissions"
    stage = state / "document-cache"
    submissions.mkdir(parents=True)
    stage.mkdir()
    ref = "wbsub_v1_33333333333333333333333333333333"
    (submissions / f"{ref}.json").write_text(json.dumps({
        "status": "PENDING_TRANSCRIPT", "session_id": "session-1",
        "created_at": now, "prompt": "请归档此文档",
        "transcript_path": str(transcript), "attachments": [],
    }, ensure_ascii=False), encoding="utf-8")
    client = FakeAttachmentClient()
    registry = WorkBuddyAttachmentRegistry([stage])
    service = WorkBuddySubmissionService(
        transfer=AttachmentTransferService(client, registry), registry=registry,
        state_dir=state, transcript_roots=[projects],
    )

    result = asyncio.run(service.ingest(
        submission_ref=ref, user_request=None,
        placement_mode="BY_CATEGORY", rule_profile="content_based",
    ))

    assert result["uploaded_count"] == 1
    assert client.uploaded[0][2] == source.name
    assert client.uploaded[0][1].parent == stage.resolve()
    assert client.uploaded[0][1].read_bytes() == source.read_bytes()
    assert client.items[0]["source_relative_path"].startswith("submitted-attachments/wbd_")


def test_document_reference_rejects_user_text_and_unmatched_host_path(tmp_path) -> None:
    """聊天正文伪造附件标签及宿主引用不一致都不能变成文件读取授权。"""

    source = tmp_path / "secret.pdf"
    now = time.time()
    message = _document_message(path=source, prompt="归档", created_at=now)
    assert _host_document_paths(message) == [str(source)]
    assert _host_document_paths(message, prompt="不属于本轮的任务") == []
    text = message["content"][0]["text"]
    message["content"][0]["text"] = text.replace("<attached_files>", "<not_attached_files>", 1).replace("</attached_files>", "</not_attached_files>", 1) + f'<attached_files><file path={quoteattr(str(source))} /></attached_files>'
    assert _host_document_paths(message) == []
    message["content"][0]["text"] = text.replace(json.dumps(str(source), ensure_ascii=False), json.dumps(str(tmp_path / "other.pdf"), ensure_ascii=False), 1)
    assert _host_document_paths(message) == []


def test_document_bridge_requires_separate_authorized_stage_root(tmp_path) -> None:
    """没有显式登记插件私有缓存根时，文档引用不能退化为任意本地路径导入。"""

    home = tmp_path / ".workbuddy"
    projects = home / "projects"
    projects.mkdir(parents=True)
    source = tmp_path / "材料.pdf"
    source.write_bytes(b"pdf-test")
    now = time.time()
    transcript = projects / "session.jsonl"
    transcript.write_text(json.dumps(_document_message(path=source, prompt="请归档此文档", created_at=now), ensure_ascii=False) + "\n", encoding="utf-8")
    state = home / "bridge-state"
    submissions = state / "submissions"
    submissions.mkdir(parents=True)
    stage = state / "document-cache"
    stage.mkdir()
    blobs = home / "blobs"
    blobs.mkdir()
    ref = "wbsub_v1_44444444444444444444444444444444"
    (submissions / f"{ref}.json").write_text(json.dumps({
        "status": "PENDING_TRANSCRIPT", "session_id": "session-1", "created_at": now,
        "prompt": "请归档此文档", "transcript_path": str(transcript), "attachments": [],
    }, ensure_ascii=False), encoding="utf-8")
    client = FakeAttachmentClient()
    registry = WorkBuddyAttachmentRegistry([blobs])
    service = WorkBuddySubmissionService(
        transfer=AttachmentTransferService(client, registry), registry=registry,
        state_dir=state, transcript_roots=[projects],
    )

    with pytest.raises(ValueError, match="专用缓存根"):
        asyncio.run(service.ingest(
            submission_ref=ref, user_request=None,
            placement_mode="BY_CATEGORY", rule_profile="content_based",
        ))
    assert not list(stage.iterdir())
    assert not client.uploaded


def test_document_hook_to_mcp_bridge_end_to_end(monkeypatch, tmp_path) -> None:
    """真实 Hook 先签发短时引用，消息落盘后 MCP 才解析并快照文档。"""

    home = tmp_path / ".workbuddy"
    projects = home / "projects"
    projects.mkdir(parents=True)
    transcript = projects / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    state = home / "bridge-state"
    stage = state / "document-cache"
    stage.mkdir(parents=True)
    source = tmp_path / "申请表.xlsx"
    source.write_bytes(b"xlsx-test")
    monkeypatch.setenv("FILE_AGENT_WORKBUDDY_HOME", str(home))
    monkeypatch.setenv("CODEBUDDY_PLUGIN_DATA", str(state))
    hook = _load_capture_module()
    result = hook.capture_submission({
        "hook_event_name": "UserPromptSubmit", "session_id": "session-1",
        "transcript_path": str(transcript), "prompt": "请用文件助手归档并分类",
    })
    ref = re.search(r"ref=(wbsub_v1_[a-f0-9]{32})", result["hookSpecificOutput"]["additionalContext"])
    assert ref is not None
    manifest = json.loads((state / "submissions" / f"{ref.group(1)}.json").read_text(encoding="utf-8"))
    transcript.write_text(json.dumps(_document_message(
        path=source, prompt=manifest["prompt"], created_at=manifest["created_at"] - 6,
    ), ensure_ascii=False) + "\n", encoding="utf-8")
    client = FakeAttachmentClient()
    registry = WorkBuddyAttachmentRegistry([stage])
    service = WorkBuddySubmissionService(
        transfer=AttachmentTransferService(client, registry), registry=registry,
        state_dir=state, transcript_roots=[projects],
    )

    uploaded = asyncio.run(service.ingest(
        submission_ref=ref.group(1), user_request=None,
        placement_mode="BY_CATEGORY", rule_profile="content_based",
    ))

    assert uploaded["uploaded_count"] == 1
    assert client.uploaded[0][2] == source.name
    assert client.uploaded[0][1].parent == stage.resolve()


def _load_capture_module():
    """加载真实 Hook 脚本，用于覆盖 Hook 到 MCP 的清单兼容性。"""

    script = (
        Path(__file__).resolve().parents[3]
        / "integrations"
        / "workbuddy"
        / "file-agent-plugin"
        / "scripts"
        / "capture_submission.py"
    )
    spec = importlib.util.spec_from_file_location("workbuddy_capture_for_submission_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trusted_submission_accepts_hashed_blob_filename(tmp_path) -> None:
    """可信入口允许 blob 物理名与用户原文件名不同，旧手工入口仍不放宽。"""

    cache = tmp_path / "blobs"
    cache.mkdir()
    blob = cache / "5f4dcc3b5aa765d61d8327deb882cf99.jpg"
    blob.write_bytes(b"image")
    state = tmp_path / "state"
    submissions = state / "submissions"
    submissions.mkdir(parents=True)
    submission_ref = "wbsub_v1_0123456789abcdef0123456789abcdef"
    (submissions / f"{submission_ref}.json").write_text(
        json.dumps(
            {
                "submission_id": "message-1",
                "created_at": time.time(),
                "prompt": "请归档这个附件",
                "attachments": [
                    {"attachment_id": "blob-1", "filename": "奖学金材料.jpg", "local_path": str(blob)}
                ],
            }
        ),
        encoding="utf-8",
    )
    client = FakeAttachmentClient()
    registry = WorkBuddyAttachmentRegistry([cache])
    service = WorkBuddySubmissionService(
        transfer=AttachmentTransferService(client, registry),
        registry=registry,
        state_dir=state,
    )

    result = asyncio.run(
        service.ingest(
            submission_ref=submission_ref,
            user_request=None,
            placement_mode="BY_CATEGORY",
            rule_profile="content_based",
        )
    )

    assert result["uploaded_count"] == 1
    assert client.uploaded[0][2] == "奖学金材料.jpg"
    with pytest.raises(ValueError, match="文件名与缓存文件不一致"):
        registry.resolve(local_path=str(blob), filename="奖学金材料.jpg")


def test_submission_rejects_expired_or_forged_request(tmp_path) -> None:
    """提交单必须在时效内，模型也不能改写 Hook 已冻结的任务文本。"""

    cache = tmp_path / "blobs"
    cache.mkdir()
    blob = cache / "hash.jpg"
    blob.write_bytes(b"image")
    state = tmp_path / "state"
    submissions = state / "submissions"
    submissions.mkdir(parents=True)
    submission_ref = "wbsub_v1_abcdef0123456789abcdef0123456789"
    path = submissions / f"{submission_ref}.json"
    path.write_text(
        json.dumps(
            {
                "submission_id": "message-1",
                "created_at": 0,
                "prompt": "请归档这个附件",
                "attachments": [{"attachment_id": "blob-1", "filename": "a.jpg", "local_path": str(blob)}],
            }
        ),
        encoding="utf-8",
    )
    registry = WorkBuddyAttachmentRegistry([cache])
    service = WorkBuddySubmissionService(
        transfer=AttachmentTransferService(FakeAttachmentClient(), registry), registry=registry, state_dir=state
    )
    with pytest.raises(ValueError, match="已过期"):
        service.load(submission_ref=submission_ref)


@pytest.mark.parametrize("message_offset,accepted", [(0, True), (-6, True), (-29, True), (-31, False)])
def test_pending_submission_resolves_exact_current_transcript_message(tmp_path, message_offset, accepted) -> None:
    """MCP 调用时从固定 transcript 唯一解析本轮附件，再复用既有传输链路。"""

    home = tmp_path / ".workbuddy"
    projects = home / "projects"
    transcript = projects / "workspace" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    cache = home / "blobs"
    blob = cache / "aa" / "hash.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"image")
    created_at = time.time()
    transcript.write_text(
        json.dumps(
            {
                "id": "current-message",
                "role": "user",
                "sessionId": "session-1",
                "timestamp": int((created_at + message_offset) * 1000),
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "<system-reminder>宿主运行上下文</system-reminder>"
                            "<user_query>@image#1:材料.jpg 请把这个附件归档并分类</user_query>"
                        ),
                    },
                    {
                        "type": "image_blob_ref",
                        "blob_id": "blob-1",
                        "blob_path": str(blob),
                        "original_filename": "奖学金材料.jpg",
                    },
                    {"type": "input_text", "text": "<image_local_path>宿主私有路径</image_local_path>"},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    submissions = state / "submissions"
    submissions.mkdir(parents=True)
    submission_ref = "wbsub_v1_11111111111111111111111111111111"
    (submissions / f"{submission_ref}.json").write_text(
        json.dumps(
            {
                "version": 2,
                "status": "PENDING_TRANSCRIPT",
                "submission_ref": submission_ref,
                "session_id": "session-1",
                "created_at": created_at,
                "prompt": "请把这个附件归档并分类",
                "transcript_path": str(transcript),
                "attachments": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = FakeAttachmentClient()
    registry = WorkBuddyAttachmentRegistry([cache])
    service = WorkBuddySubmissionService(
        transfer=AttachmentTransferService(client, registry),
        registry=registry,
        state_dir=state,
        transcript_roots=[projects],
    )

    # 只容忍有限的宿主附件准备耗时，时间窗外的历史消息仍不得导入。
    if not accepted:
        with pytest.raises(ValueError, match="未能在固定会话记录"):
            service.load(submission_ref=submission_ref)
        assert not client.uploaded
        return

    result = asyncio.run(
        service.ingest(
            submission_ref=submission_ref,
            user_request=None,
            placement_mode="BY_CATEGORY",
            rule_profile="content_based",
        )
    )

    assert result["uploaded_count"] == 1
    assert client.uploaded[0][2] == "奖学金材料.jpg"
    assert client.items[0]["source_relative_path"] == "submitted-attachments/blob-1/奖学金材料.jpg"


def test_pending_submission_rejects_ambiguous_matching_messages(tmp_path) -> None:
    """同一时间窗出现两个相同任务附件消息时必须拒绝，不能猜测最新一个。"""

    home = tmp_path / ".workbuddy"
    projects = home / "projects"
    transcript = projects / "workspace" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    cache = home / "blobs"
    blob = cache / "aa" / "hash.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"image")
    created_at = time.time()
    messages = []
    for index in (1, 2):
        messages.append(
            json.dumps(
                {
                    "id": f"message-{index}",
                    "role": "user",
                    "sessionId": "session-1",
                    "timestamp": int(created_at * 1000),
                    "content": [
                        {"type": "input_text", "text": "归档附件"},
                        {
                            "type": "image_blob_ref",
                            "blob_id": f"blob-{index}",
                            "blob_path": str(blob),
                            "original_filename": "材料.jpg",
                        },
                    ],
                },
                ensure_ascii=False,
            )
        )
    transcript.write_text("\n".join(messages) + "\n", encoding="utf-8")
    state = tmp_path / "state"
    submissions = state / "submissions"
    submissions.mkdir(parents=True)
    submission_ref = "wbsub_v1_22222222222222222222222222222222"
    (submissions / f"{submission_ref}.json").write_text(
        json.dumps(
            {
                "version": 2,
                "status": "PENDING_TRANSCRIPT",
                "submission_ref": submission_ref,
                "session_id": "session-1",
                "created_at": created_at,
                "prompt": "归档附件",
                "transcript_path": str(transcript),
                "attachments": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    registry = WorkBuddyAttachmentRegistry([cache])
    service = WorkBuddySubmissionService(
        transfer=AttachmentTransferService(FakeAttachmentClient(), registry),
        registry=registry,
        state_dir=state,
        transcript_roots=[projects],
    )

    with pytest.raises(ValueError, match="匹配不唯一"):
        service.load(submission_ref=submission_ref)


def test_environment_derives_projects_root_from_standard_blobs_root(monkeypatch, tmp_path) -> None:
    """标准 WorkBuddy 布局不要求用户重复配置 transcript 根。"""

    home = tmp_path / ".workbuddy"
    cache = home / "blobs"
    projects = home / "projects"
    state = home / "bridge-state"
    cache.mkdir(parents=True)
    projects.mkdir()
    state.mkdir()
    monkeypatch.setenv("FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS", json.dumps([str(cache)]))
    monkeypatch.setenv("FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR", str(state))
    monkeypatch.delenv("FILE_AGENT_WORKBUDDY_TRANSCRIPT_ROOTS", raising=False)
    registry = WorkBuddyAttachmentRegistry([cache])

    service = WorkBuddySubmissionService.from_environment(
        transfer=AttachmentTransferService(FakeAttachmentClient(), registry),
        registry=registry,
    )

    assert service.transcript_roots == [projects.resolve()]


@pytest.mark.parametrize("stdio_encoding", ["utf-8", "gbk:surrogateescape"])
def test_hook_pending_manifest_is_resolved_after_current_message_is_written(monkeypatch, tmp_path, stdio_encoding) -> None:
    """复现 WorkBuddy 5.5.3 时序：Hook 先返回，随后消息落盘，再由 MCP 导入。"""

    home = tmp_path / ".workbuddy"
    projects = home / "projects"
    transcript = projects / "workspace" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    cache = home / "blobs"
    blob = cache / "aa" / "hash.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"image")
    state = home / "bridge-state"
    monkeypatch.setenv("FILE_AGENT_WORKBUDDY_HOME", str(home))
    monkeypatch.setenv("CODEBUDDY_PLUGIN_DATA", str(state))
    prompt = "@image#1:材料.jpg 请使用文件助手归档并分类本轮图片，测试 WB-0912-06。"

    module = _load_capture_module()
    # 通过真实 stdin 字节和进程入口复现宿主，直接调用函数无法发现 GBK 管道解码错误。
    completed = subprocess.run(
        [sys.executable, module.__file__],
        input=json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "transcript_path": str(transcript),
            "prompt": prompt,
        }, ensure_ascii=False).encode("utf-8"),
        env={**os.environ, "PYTHONIOENCODING": stdio_encoding},
        capture_output=True,
        timeout=10,
        check=True,
    )
    hook_result = json.loads(completed.stdout.decode("utf-8"))
    context = hook_result["hookSpecificOutput"]["additionalContext"]
    match = re.search(r"ref=(wbsub_v1_[a-f0-9]{32})", context)
    assert match is not None
    submission_ref = match.group(1)
    manifest = json.loads((state / "submissions" / f"{submission_ref}.json").read_text(encoding="utf-8"))
    assert manifest["prompt"] == prompt
    transcript.write_text(
        json.dumps(
            {
                "id": "current-message",
                "role": "user",
                "sessionId": "session-1",
                "timestamp": int((manifest["created_at"] - 6) * 1000),
                "content": [
                    {
                        "type": "input_text",
                        "text": f"<system-reminder>宿主上下文</system-reminder><user_query>@image#1 {prompt}</user_query>",
                    },
                    {
                        "type": "image_blob_ref",
                        "blob_id": "blob-1",
                        "blob_path": str(blob),
                        "original_filename": "材料.jpg",
                    },
                    {"type": "input_text", "text": "<image_local_path>宿主私有路径</image_local_path>"},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    client = FakeAttachmentClient()
    registry = WorkBuddyAttachmentRegistry([cache])
    service = WorkBuddySubmissionService(
        transfer=AttachmentTransferService(client, registry),
        registry=registry,
        state_dir=state,
        transcript_roots=[projects],
    )

    result = asyncio.run(
        service.ingest(
            submission_ref=submission_ref,
            user_request=None,
            placement_mode="BY_CATEGORY",
            rule_profile="content_based",
        )
    )

    assert result["uploaded_count"] == 1
    assert client.uploaded[0][2] == "材料.jpg"
