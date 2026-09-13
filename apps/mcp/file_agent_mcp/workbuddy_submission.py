"""读取 WorkBuddy 插件生成的可信附件提交单。

Hook 先冻结当前会话引用；MCP 再从固定 transcript 验证图片 blob 或宿主文档双重引用，
并把文档快照放入已授权的插件私有缓存。此模块只把经验证的引用转换为既有
``AttachmentTransferService`` 所需的输入，绝不向
模型、后端响应或日志返回本机绝对路径。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .attachment_transfer import AttachmentTransferService, WorkBuddyAttachment
from .client import WorkBuddyAttachmentRegistry, file_sha256


_REF_PATTERN = re.compile(r"^wbsub_v1_[a-f0-9]{32}$")
_ATTACHMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_DEFAULT_TTL_SECONDS = 600
_PENDING_CLOCK_SKEW_SECONDS = 5
# 消息时间在宿主准备附件时记录；实测 Hook 启动晚约 6 秒，与提交单未来时钟容差分开。
_PENDING_HOST_PREPARATION_SECONDS = 30
_VERIFIED_ATTACHMENT_TYPES = frozenset({"image_blob_ref"})
_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt"})
_DOCUMENT_LIMIT_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TrustedSubmission:
    """经本机状态目录和时效校验后的不可变提交单。"""

    submission_ref: str
    submission_id: str
    user_request: str
    attachments: list[dict[str, str]]


class WorkBuddySubmissionService:
    """验证可信提交单并复用既有附件传输服务。"""

    def __init__(
        self,
        *,
        transfer: AttachmentTransferService,
        registry: WorkBuddyAttachmentRegistry,
        state_dir: Path,
        transcript_roots: list[Path] | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        """注入传输依赖；状态目录必须由 MCP 配置显式指定。"""

        self.transfer = transfer
        self.registry = registry
        self.state_dir = state_dir.expanduser().resolve()
        self.transcript_roots = _normalize_transcript_roots(transcript_roots or [])
        self.ttl_seconds = ttl_seconds

    @classmethod
    def from_environment(
        cls,
        *,
        transfer: AttachmentTransferService,
        registry: WorkBuddyAttachmentRegistry,
    ) -> "WorkBuddySubmissionService":
        """从环境读取插件私有提交单目录，未配置时禁用自动桥接入口。"""

        raw_dir = os.getenv("FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR", "").strip()
        if not raw_dir:
            raise ValueError("未配置 FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR，无法读取 WorkBuddy 附件提交单")
        try:
            ttl_seconds = int(os.getenv("FILE_AGENT_WORKBUDDY_SUBMISSION_TTL_SECONDS", "600"))
        except ValueError as exc:
            raise ValueError("FILE_AGENT_WORKBUDDY_SUBMISSION_TTL_SECONDS 必须是整数") from exc
        if not 60 <= ttl_seconds <= 3600:
            raise ValueError("FILE_AGENT_WORKBUDDY_SUBMISSION_TTL_SECONDS 必须介于 60 和 3600 秒")
        transcript_roots = _transcript_roots_from_environment()
        return cls(
            transfer=transfer,
            registry=registry,
            state_dir=Path(raw_dir),
            transcript_roots=transcript_roots,
            ttl_seconds=ttl_seconds,
        )

    async def ingest(
        self,
        *,
        submission_ref: str,
        user_request: str | None,
        placement_mode: str,
        rule_profile: str,
    ) -> dict[str, Any]:
        """导入本轮受信提交单；任务文字只能为空或与 Hook 捕获的原文一致。"""

        submission = self.load(submission_ref=submission_ref)
        if user_request is not None and _normalize(user_request) != _normalize(submission.user_request):
            raise ValueError("user_request 必须省略或与本轮 WorkBuddy 提交文字完全一致")
        manifest = [self._resolve_attachment(item) for item in submission.attachments]
        # AttachmentTransferService 的公开入口专为手工 local_path 设计。这里先完成可信
        # 清单、缓存根和快照校验，再调用其内部的统一批次创建、上传及二次哈希复核流程。
        return await self.transfer.ingest_resolved(
            submission_id=submission.submission_id,
            attachments=manifest,
            user_request=submission.user_request,
            placement_mode=placement_mode,
            rule_profile=rule_profile,
        )

    def load(self, *, submission_ref: str) -> TrustedSubmission:
        """读取一个未过期、非软链接且结构完整的插件提交单。"""

        if not _REF_PATTERN.fullmatch(submission_ref):
            raise ValueError("submission_ref 格式无效")
        submissions_dir = self.state_dir / "submissions"
        if self.state_dir.is_symlink() or submissions_dir.is_symlink() or not submissions_dir.is_dir():
            raise ValueError("WorkBuddy 附件提交单目录不可用")
        path = submissions_dir / f"{submission_ref}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("附件提交单不存在或不可读取")
        try:
            mode = path.stat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("附件提交单不是普通文件")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("附件提交单内容无效") from exc
        if not isinstance(raw, dict):
            raise ValueError("附件提交单内容无效")
        created_at = raw.get("created_at")
        if (
            not isinstance(created_at, (int, float))
            or created_at > time.time() + _PENDING_CLOCK_SKEW_SECONDS
            or time.time() - created_at > self.ttl_seconds
        ):
            raise ValueError("附件提交单已过期，请重新发送本轮附件和任务")
        status = raw.get("status", "READY")
        if status == "PENDING_TRANSCRIPT":
            raw = self._resolve_pending_manifest(raw=raw, created_at=float(created_at))
        elif status != "READY":
            raise ValueError("附件提交单状态无效")
        submission_id = raw.get("submission_id")
        prompt = raw.get("prompt")
        attachments = raw.get("attachments")
        if (
            not isinstance(submission_id, str)
            or not submission_id
            or not isinstance(prompt, str)
            or not prompt.strip()
            or not isinstance(attachments, list)
            or not attachments
            or len(attachments) > 1000
        ):
            raise ValueError("附件提交单字段不完整")
        parsed: list[dict[str, str]] = []
        pending_verified = status == "PENDING_TRANSCRIPT"
        seen_ids: set[str] = set()
        for item in attachments:
            if not isinstance(item, dict):
                raise ValueError("附件提交单包含无效附件")
            attachment_id = item.get("attachment_id")
            filename = item.get("filename")
            local_path = item.get("local_path")
            source_path = item.get("source_path")
            if (
                not isinstance(attachment_id, str)
                or not _ATTACHMENT_ID_PATTERN.fullmatch(attachment_id)
                or attachment_id in seen_ids
                or not isinstance(filename, str)
                or not (
                    isinstance(local_path, str) and source_path is None
                    or pending_verified and local_path is None and isinstance(source_path, str)
                )
            ):
                raise ValueError("附件提交单包含无效附件")
            seen_ids.add(attachment_id)
            parsed.append({
                "attachment_id": attachment_id,
                "filename": filename,
                **({"source_path": source_path} if source_path is not None else {"local_path": local_path}),
            })
        return TrustedSubmission(submission_ref, submission_id, prompt, parsed)

    def _resolve_pending_manifest(
        self, *, raw: dict[str, Any], created_at: float
    ) -> dict[str, Any]:
        """从 Hook 固定的 transcript 中唯一解析刚刚落盘的本轮附件消息。"""

        session_id = raw.get("session_id")
        prompt = raw.get("prompt")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("待解析附件提交单缺少会话标识")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("待解析附件提交单缺少任务文字")
        transcript = self._resolve_transcript_path(raw.get("transcript_path"))
        candidates: list[dict[str, Any]] = []
        try:
            with transcript.open("r", encoding="utf-8") as source:
                for raw_line in source:
                    try:
                        message = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(message, dict) or message.get("role") != "user":
                        continue
                    if (message.get("sessionId") or message.get("session_id")) != session_id:
                        continue
                    timestamp = _message_timestamp_seconds(message.get("timestamp"))
                    if timestamp is None or not (
                        created_at - _PENDING_HOST_PREPARATION_SECONDS
                        <= timestamp
                        <= created_at + self.ttl_seconds
                    ):
                        continue
                    if not _message_matches_prompt(message, prompt):
                        continue
                    if not any(
                        isinstance(part, dict)
                        and part.get("type") in _VERIFIED_ATTACHMENT_TYPES
                        for part in message.get("content") or []
                    ) and not _host_document_paths(message, prompt=prompt):
                        continue
                    candidates.append(message)
        except OSError as exc:
            raise ValueError("WorkBuddy 会话记录暂不可读，请重新发送附件和任务") from exc
        if not candidates:
            raise ValueError("未能在固定会话记录中找到本轮已验证附件，请重新发送附件和任务")
        if len(candidates) != 1:
            raise ValueError("本轮附件消息匹配不唯一，请更换任务文字后重新发送")
        message = candidates[0]
        submission_id = message.get("uuid") or message.get("id")
        if not isinstance(submission_id, str) or not submission_id:
            raise ValueError("本轮附件消息缺少稳定消息标识")
        attachments: list[dict[str, str]] = []
        for part in message.get("content") or []:
            if not isinstance(part, dict) or part.get("type") not in _VERIFIED_ATTACHMENT_TYPES:
                continue
            attachment_id = part.get("blob_id")
            filename = part.get("original_filename")
            local_path = part.get("blob_path")
            if (
                not isinstance(attachment_id, str)
                or not isinstance(filename, str)
                or not isinstance(local_path, str)
            ):
                raise ValueError("本轮附件消息包含不完整的附件引用")
            attachments.append(
                {
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "local_path": local_path,
                }
            )
        # 文档路径只来自宿主在 user_query 之前渲染的附件区；聊天正文中的路径绝不作为授权。
        for index, source_path in enumerate(_host_document_paths(message, prompt=prompt)):
            filename = Path(source_path).name
            if (
                not filename
                or len(filename) > 255
                or filename in {".", ".."}
                or "/" in filename
                or "\\" in filename
                or "\x00" in filename
                or Path(filename).suffix.casefold() not in _DOCUMENT_EXTENSIONS
            ):
                raise ValueError("本轮 WorkBuddy 文档附件格式不受支持")
            stable_id = hashlib.sha256(
                f"{submission_id}\0{index}\0{source_path}".encode("utf-8")
            ).hexdigest()[:40]
            attachments.append({
                "attachment_id": f"wbd_{stable_id}",
                "filename": filename,
                "source_path": source_path,
            })
        return {
            **raw,
            "status": "READY",
            "submission_id": submission_id,
            "attachments": attachments,
        }

    def _resolve_transcript_path(self, raw_path: object) -> Path:
        """只允许读取显式配置或由标准 blobs 根推导出的 projects 根内 JSONL。"""

        if not self.transcript_roots:
            raise ValueError("未配置 WorkBuddy 会话记录授权根")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("待解析附件提交单缺少会话记录引用")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute() or candidate.is_symlink():
            raise ValueError("WorkBuddy 会话记录引用无效")
        try:
            resolved = candidate.resolve(strict=True)
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise ValueError("WorkBuddy 会话记录不存在") from exc
        if (
            resolved.suffix.lower() != ".jsonl"
            or not stat.S_ISREG(mode)
            or not any(root in resolved.parents for root in self.transcript_roots)
        ):
            raise ValueError("WorkBuddy 会话记录不在授权根内")
        return resolved

    def _resolve_attachment(self, item: dict[str, str]) -> WorkBuddyAttachment:
        """按受信入口规则生成真实文件快照，物理 blob 名不参与逻辑文件名校验。"""

        local_path = item.get("local_path") or str(self._stage_document(item))
        path = self.registry.resolve_trusted_cache_file(local_path=local_path, filename=item["filename"])
        stat_result = path.stat()
        return WorkBuddyAttachment(
            attachment_id=item["attachment_id"],
            filename=item["filename"],
            local_path=path,
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            sha256=file_sha256(path),
        )

    def _stage_document(self, item: dict[str, str]) -> Path:
        """把宿主固定文档引用流式快照到已授权私有缓存；不得读取模型提供的路径。"""

        stage_root = self.state_dir / "document-cache"
        if not stage_root.is_dir() or not self.registry.permits_cache_root(stage_root):
            raise ValueError("未配置 WorkBuddy 文档桥接专用缓存根")
        source = Path(item["source_path"])
        if not source.is_absolute() or source.is_symlink():
            raise ValueError("WorkBuddy 文档附件来源不是普通绝对路径")
        try:
            # 父目录若通过软链接或重解析点跳转，拒绝把宿主显示路径误当成真实文件边界。
            if source.resolve(strict=True) != source.absolute():
                raise ValueError("WorkBuddy 文档附件来源含链接路径")
            before = source.stat()
        except OSError:
            raise ValueError("WorkBuddy 文档附件来源不存在") from None
        if not stat.S_ISREG(before.st_mode) or before.st_size > _DOCUMENT_LIMIT_BYTES:
            raise ValueError("WorkBuddy 文档附件不是允许大小的普通文件")
        temporary = stage_root / f".{item['attachment_id']}.{os.getpid()}.{time.time_ns()}.tmp"
        digest = hashlib.sha256()
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            after = source.stat()
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                raise ValueError("SOURCE_CHANGED")
            target = stage_root / f"{digest.hexdigest()}{Path(item['filename']).suffix.casefold()}"
            if target.exists():
                if target.is_symlink() or not target.is_file() or file_sha256(target) != digest.hexdigest():
                    raise ValueError("WorkBuddy 文档快照缓存冲突")
            else:
                os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
                os.replace(temporary, target)
            return target
        except OSError:
            raise ValueError("WorkBuddy 文档快照失败") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # 临时文件清理失败不覆盖上方的业务错误；下次启动仅按私有缓存策略清理。
                pass


def _host_document_paths(message: dict[str, Any], *, prompt: str | None = None) -> list[str]:
    """仅解析 WorkBuddy 5.5.3 的宿主附件区，拒绝用户正文伪造的同名 XML。"""

    content = message.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return []
    first = content[0]
    text = first.get("text") if first.get("type") == "input_text" else None
    marker = '<system-reminder data-role="user-context">'
    if not isinstance(text, str) or not text.startswith(marker) or len(text) > 1024 * 1024:
        return []
    reminder_end = text.find("</system-reminder>")
    query_start = text.find("<user_query>")
    query_end = text.find("</user_query>", query_start)
    if reminder_end < 0 or query_start < reminder_end or query_end <= query_start:
        return []
    if prompt is not None and _normalize(prompt) not in _normalize(
        text[query_start + len("<user_query>"):query_end]
    ):
        return []
    host = text[:reminder_end]
    if host.count("<attached_files>") != 1 or host.count("</attached_files>") != 1:
        return []
    if host.count("<user_references>") != 1 or host.count("</user_references>") != 1:
        return []
    attached = host.split("<attached_files>", 1)[1].split("</attached_files>", 1)[0]
    references = host.split("<user_references>", 1)[1].split("</user_references>", 1)[0]
    try:
        tree = ET.fromstring(f"<attached_files>{attached}</attached_files>")
    except ET.ParseError:
        return []
    referenced = set()
    for line in references.splitlines():
        line = line.strip()
        if line.startswith('"'):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                return []
            if isinstance(value, str):
                referenced.add(value)
    paths = []
    for child in tree:
        if child.tag != "file" or set(child.attrib) != {"path"} or child.text or len(child):
            return []
        path = child.attrib["path"]
        if path not in referenced or path in paths:
            return []
        paths.append(path)
        if len(paths) > 100:
            return []
    return paths


def _normalize(value: str) -> str:
    """统一宿主 Unicode 与空白差异，避免合法附件消息匹配失败。"""

    scalar_text = value.encode("utf-16-le", errors="surrogatepass").decode(
        "utf-16-le", errors="replace"
    )
    return " ".join(scalar_text.split())


def _message_matches_prompt(message: dict[str, Any], prompt: str) -> bool:
    """确认用户消息正文包含 Hook 冻结的任务文字。"""

    texts = [
        str(part.get("text") or "")
        for part in message.get("content") or []
        if isinstance(part, dict) and part.get("type") == "input_text"
    ]
    return _normalize(prompt) in _normalize(" ".join(texts))


def _message_timestamp_seconds(value: object) -> float | None:
    """兼容 WorkBuddy transcript 的毫秒或秒时间戳。"""

    if not isinstance(value, (int, float)):
        return None
    return float(value) / 1000 if value > 10**11 else float(value)


def _normalize_transcript_roots(roots: list[Path]) -> list[Path]:
    """规范化 transcript 授权根，拒绝链接和不存在目录。"""

    normalized: list[Path] = []
    for root in roots:
        candidate = root.expanduser()
        if candidate.is_symlink():
            raise ValueError("WorkBuddy 会话记录授权根不能是符号链接")
        resolved = candidate.resolve()
        if not resolved.is_dir():
            raise ValueError("WorkBuddy 会话记录授权根不存在或不是目录")
        normalized.append(resolved)
    return normalized


def _transcript_roots_from_environment() -> list[Path]:
    """读取显式 transcript 根；标准布局可由已授权 blobs 根安全推导。"""

    raw = os.getenv("FILE_AGENT_WORKBUDDY_TRANSCRIPT_ROOTS", "").strip()
    source_name = "FILE_AGENT_WORKBUDDY_TRANSCRIPT_ROOTS"
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source_name} 必须是路径字符串 JSON 数组") from exc
    else:
        source_name = "FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS"
        try:
            attachment_roots = json.loads(os.getenv(source_name, "[]"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source_name} 必须是路径字符串 JSON 数组") from exc
        if not isinstance(attachment_roots, list) or not all(
            isinstance(item, str) for item in attachment_roots
        ):
            raise ValueError(f"{source_name} 必须是路径字符串 JSON 数组")
        parsed = [
            str(Path(item).expanduser().parent / "projects")
            for item in attachment_roots
            if Path(item).expanduser().name.casefold() == "blobs"
        ]
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{source_name} 必须是路径字符串 JSON 数组")
    return [Path(item) for item in parsed]
