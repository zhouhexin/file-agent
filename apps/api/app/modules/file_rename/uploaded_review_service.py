"""上传文件低置信度命名建议的对话更正服务。

待确认事实来自已持久化的 ``generate-rename-suggestions`` ToolInvocation；用户提供实际
新文件名后创建延后工作副本计划。该服务不修改上传原件，也不复用已退役的受管原件执行器。
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    Document,
    DocumentVersion,
    Message,
    OperationPlan,
    ToolInvocation,
    UploadArchiveRecord,
    WorkingCopy,
)
from app.modules.file_lifecycle.operations import DEFERRED_UPLOAD_RENAME_OPERATION
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.operations.repository import OperationPlanRepository
from app.modules.retrieval.clarification_service import FileSearchClarificationService


_CORRECTION_PATTERN = re.compile(
    r"^\s*(?:把\s*)?(?:文件\s*)?(?P<source>.+?)\s*(?:更正为|改为|重命名为)\s*(?P<target>.+?)\s*[。；;]?\s*$"
)
_DISMISS_MESSAGES = {"不需要", "不需要改名", "无需改名", "不用改名"}
_SOURCE_PLACEHOLDERS = {"原文件名", "文件原文件名"}
_TARGET_PLACEHOLDERS = {"新文件名", "实际名称", "新的文件名", "请填写实际名称"}


class UploadedRenameReviewResolutionService:
    """把当前会话最新上传重命名待确认项转换为安全 OperationPlan。"""

    def __init__(self, db: Session, user_id: str) -> None:
        """保存请求级数据库会话和用户边界。"""

        self.db = db
        self.user_id = user_id
        self.repository = OperationPlanRepository(db)

    def resolve(
        self,
        *,
        conversation_id: str,
        agent_run_id: str,
        message: str,
    ) -> dict[str, Any]:
        """解析用户给出的实际名称；不在本步骤直接执行物理重命名。"""

        pending = self._latest_pending_suggestions(conversation_id=conversation_id)
        candidates = self._conversation_candidates(
            conversation_id=conversation_id,
            pending=pending,
        )
        normalized = message.strip().rstrip("。！!")
        if normalized in _DISMISS_MESSAGES:
            if not pending:
                return _error(
                    "PENDING_RENAME_NOT_FOUND",
                    "当前会话没有仍待确认的上传文件。",
                )
            return {
                "ok": True,
                "kind": "rename_review_resolution",
                "status": "COMPLETED",
                "dismissed_count": len(pending),
                "accepted_count": 0,
                "remaining_review_count": 0,
                "operation_plan_id": None,
                "completed_items": [],
                "failed_items": [],
                "ambiguous_items": [],
            }
        corrections = self._parse_corrections(
            message=message,
            pending=pending or candidates,
        )
        if isinstance(corrections, dict):
            return corrections

        items: list[dict[str, Any]] = []
        used_source_ids: set[str] = set()
        for source_name, target_name in corrections:
            matches = self._match_pending(
                pending=candidates,
                source_name=source_name,
            )
            if len(matches) != 1:
                similar = (
                    matches
                    if matches
                    else _similar_rename_candidates(
                        source_name=source_name,
                        candidates=candidates,
                    )
                )
                if similar:
                    return self._create_file_selection(
                        conversation_id=conversation_id,
                        agent_run_id=agent_run_id,
                        source_name=source_name,
                        target_name=target_name,
                        candidates=similar,
                    )
                return _error(
                    "PENDING_RENAME_NOT_FOUND",
                    (
                        f"没有找到名为“{source_name}”的文件，也没有可供确认的相似文件。"
                        "请重新附加要重命名的文件。"
                    ),
                )
            suggestion = matches[0]
            source_document_id = str(
                suggestion.get("source_document_id")
                or suggestion.get("document_id")
                or ""
            )
            if not source_document_id or source_document_id in used_source_ids:
                return _error("PENDING_RENAME_INVALID", "待确认文件范围无效，请重新生成计划。")
            document = self.db.get(Document, source_document_id)
            if document is None or document.user_id != self.user_id:
                return _error("DOCUMENT_NOT_FOUND", "待确认文件不存在或不属于当前用户。")
            try:
                validated_target = _validate_target_filename(
                    source_filename=str(suggestion.get("filename") or document.original_filename),
                    requested_name=target_name,
                )
            except ValueError as exc:
                return _error("INVALID_TARGET_FILENAME", str(exc))
            used_source_ids.add(source_document_id)
            items.append(
                {
                    "document_id": source_document_id,
                    "working_copy_id": suggestion.get("working_copy_id"),
                    "operation": DEFERRED_UPLOAD_RENAME_OPERATION,
                    "before": {
                        "filename": str(
                            suggestion.get("filename") or document.original_filename
                        ),
                        "sha256": str(
                            suggestion.get("source_sha256") or document.sha256
                        ),
                    },
                    "after": {"filename": validated_target},
                    "rename_metadata": {
                        "source": "user_correction",
                        "source_agent_run_id": suggestion.get("_source_agent_run_id"),
                    },
                    "protection": {
                        "managed_original_unchanged": True,
                        "creates_new_version": False,
                        "deferred_until_working_copy_ready": True,
                    },
                    "execution_status": "PLANNED",
                }
            )

        source_run_ids = {
            str(item.get("rename_metadata", {}).get("source_agent_run_id") or "")
            for item in items
            if str(item.get("rename_metadata", {}).get("source_agent_run_id") or "")
        }
        for source_run_id in source_run_ids:
            self._invalidate_previous_plan(
                conversation_id=conversation_id,
                source_agent_run_id=source_run_id,
            )
        plan = self.repository.create_plan(
            workspace_id=get_shared_workspace_id(self.db),
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            user_id=self.user_id,
            operation_type=DEFERRED_UPLOAD_RENAME_OPERATION,
            risk_level="medium",
            reason="用户明确补充文件名，待工作副本就绪后执行重命名",
            plan_json={
                "target": "PENDING_UPLOAD_WORKING_COPY",
                "source_rename_agent_run_id": (
                    next(iter(source_run_ids)) if len(source_run_ids) == 1 else ""
                ),
                "source_rename_agent_run_ids": sorted(source_run_ids),
                "items": items,
            },
        )
        self.db.flush()
        return {
            "ok": True,
            "kind": "rename_review_resolution",
            "status": "WAITING_CONFIRMATION",
            "dismissed_count": 0,
            "accepted_count": len(items),
            "remaining_review_count": max(0, len(pending) - len(items)),
            "operation_plan_id": plan.id,
            "completed_items": [],
            "failed_items": [],
            "ambiguous_items": [],
        }

    def _latest_pending_suggestions(
        self,
        *,
        conversation_id: str,
    ) -> list[dict[str, Any]]:
        """读取当前会话最新一次上传重命名调用中的低置信度文件。"""

        row = (
            self.db.query(ToolInvocation, AgentRun)
            .join(AgentRun, AgentRun.id == ToolInvocation.agent_run_id)
            .filter(
                AgentRun.user_id == self.user_id,
                AgentRun.conversation_id == conversation_id,
                ToolInvocation.tool_name == "generate-rename-suggestions",
            )
            .order_by(ToolInvocation.created_at.desc())
            .first()
        )
        if row is None:
            return []
        invocation, run = row
        output = invocation.output_json if isinstance(invocation.output_json, dict) else {}
        suggestions = [
            {
                **item,
                "_source_agent_run_id": run.id,
            }
            for item in output.get("suggestions", [])
            if isinstance(item, dict)
            and item.get("status") == "NEEDS_REVIEW"
            and (
                item.get("source_kind") == "uploaded_document"
                or item.get("source_document_id")
            )
        ]
        return suggestions

    def _conversation_candidates(
        self,
        *,
        conversation_id: str,
        pending: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """扩展可确认的文件候选，但不跨用户泄露上传记录。

        优先保留最新待复核项，再补充本会话较早的命名建议、用户消息附件，
        最后查询当前用户可见的活动共享工作副本。这里只生成候选；只有用户选中
        具体文件后才能进入 OperationPlan，回收站文件不会混入重命名候选。
        """

        candidates = [dict(item) for item in pending]
        rows = (
            self.db.query(ToolInvocation, AgentRun)
            .join(AgentRun, AgentRun.id == ToolInvocation.agent_run_id)
            .filter(
                AgentRun.user_id == self.user_id,
                AgentRun.conversation_id == conversation_id,
                ToolInvocation.tool_name == "generate-rename-suggestions",
            )
            .order_by(ToolInvocation.created_at.desc())
            .limit(50)
            .all()
        )
        for invocation, run in rows:
            output = (
                invocation.output_json
                if isinstance(invocation.output_json, dict)
                else {}
            )
            for value in output.get("suggestions", []):
                if not isinstance(value, dict):
                    continue
                document_id = str(
                    value.get("source_document_id")
                    or value.get("document_id")
                    or ""
                )
                if not document_id:
                    continue
                candidates.append(
                    {
                        **value,
                        "_source_agent_run_id": run.id,
                    }
                )

        messages = (
            self.db.query(Message)
            .filter(
                Message.conversation_id == conversation_id,
                Message.user_id == self.user_id,
                Message.role == "user",
            )
            .order_by(Message.created_at.desc())
            .limit(100)
            .all()
        )
        attachment_ids: list[str] = []
        for row in messages:
            for value in row.attachments_json or []:
                if not isinstance(value, dict):
                    continue
                document_id = str(value.get("document_id") or "")
                if document_id and document_id not in attachment_ids:
                    attachment_ids.append(document_id)
        if attachment_ids:
            documents = (
                self.db.query(Document)
                .filter(
                    Document.user_id == self.user_id,
                    Document.id.in_(attachment_ids),
                )
                .all()
            )
            by_id = {row.id: row for row in documents}
            for document_id in attachment_ids:
                document = by_id.get(document_id)
                if document is None:
                    continue
                candidates.append(
                    {
                        "document_id": document.id,
                        "source_document_id": document.id,
                        "filename": document.original_filename,
                        "source_sha256": document.sha256,
                        "source_kind": "conversation_attachment",
                        "size_bytes": document.size_bytes,
                        "created_at": document.created_at.isoformat(),
                        "_source_agent_run_id": "",
                    }
                )

        active_rows = (
            self.db.query(WorkingCopy, Document)
            .join(Document, Document.id == WorkingCopy.document_id)
            .filter(
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                WorkingCopy.status == "ACTIVE",
                Document.user_id == self.user_id,
            )
            .order_by(WorkingCopy.updated_at.desc())
            .limit(500)
            .all()
        )
        for working_copy, document in active_rows:
            candidates.append(
                {
                    "document_id": document.id,
                    "source_document_id": document.id,
                    "working_copy_id": working_copy.id,
                    "filename": working_copy.filename,
                    "source_sha256": working_copy.content_sha256,
                    "source_kind": "active_working_copy",
                    "size_bytes": working_copy.size_bytes,
                    "created_at": working_copy.created_at.isoformat(),
                    "_source_agent_run_id": "",
                }
            )

        # 同一 Document 只展示一次，避免历史调用和附件记录形成重复选择项。
        unique: list[dict[str, Any]] = []
        seen_candidate_ids: set[str] = set()
        for candidate in candidates:
            document_id = str(
                candidate.get("source_document_id")
                or candidate.get("document_id")
                or ""
            )
            if not document_id:
                continue
            document = self.db.get(Document, document_id)
            if document is None or document.user_id != self.user_id:
                continue
            active_copy = self._resolve_active_working_copy(document=document)
            if active_copy is not None:
                active_document = self.db.get(Document, active_copy.document_id)
                if active_document is None or active_document.user_id != self.user_id:
                    continue
                document = active_document
                document_id = active_document.id
                candidate_key = f"working-copy:{active_copy.id}"
            else:
                candidate_key = f"document:{document_id}"
            if candidate_key in seen_candidate_ids:
                continue
            seen_candidate_ids.add(candidate_key)
            unique.append(
                {
                    **candidate,
                    "document_id": document_id,
                    "source_document_id": document_id,
                    "working_copy_id": (
                        active_copy.id
                        if active_copy is not None
                        else candidate.get("working_copy_id")
                    ),
                    "filename": str(
                        active_copy.filename
                        if active_copy is not None
                        else candidate.get("filename")
                        or document.original_filename
                    ),
                    "source_sha256": str(
                        active_copy.content_sha256
                        if active_copy is not None
                        else candidate.get("source_sha256")
                        or document.sha256
                    ),
                    "size_bytes": int(
                        active_copy.size_bytes
                        if active_copy is not None
                        else candidate.get("size_bytes")
                        or document.size_bytes
                    ),
                    "created_at": str(
                        (
                            active_copy.created_at.isoformat()
                            if active_copy is not None
                            else candidate.get("created_at")
                        )
                        or document.created_at.isoformat()
                    ),
                }
            )
        return unique[:100]

    def _resolve_active_working_copy(
        self,
        *,
        document: Document,
    ) -> WorkingCopy | None:
        """把上传来源或工作副本文档解析到当前用户可见的活动共享副本。"""

        direct = (
            self.db.query(WorkingCopy)
            .join(Document, Document.id == WorkingCopy.document_id)
            .filter(
                WorkingCopy.document_id == document.id,
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                WorkingCopy.status == "ACTIVE",
                Document.user_id == self.user_id,
            )
            .one_or_none()
        )
        if direct is not None:
            return direct
        upload_version = (
            self.db.query(DocumentVersion)
            .filter(
                DocumentVersion.document_id == document.id,
                DocumentVersion.storage_tier == "UPLOAD",
            )
            .order_by(DocumentVersion.version_number.desc())
            .first()
        )
        if upload_version is None:
            return None
        archive = (
            self.db.query(UploadArchiveRecord)
            .filter(
                UploadArchiveRecord.upload_document_version_id
                == upload_version.id,
                UploadArchiveRecord.status == "ARCHIVED",
            )
            .one_or_none()
        )
        if archive is None or not archive.managed_file_id:
            return None
        return (
            self.db.query(WorkingCopy)
            .join(Document, Document.id == WorkingCopy.document_id)
            .filter(
                WorkingCopy.managed_file_id == archive.managed_file_id,
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                WorkingCopy.status == "ACTIVE",
                Document.user_id == self.user_id,
            )
            .one_or_none()
        )

    def _create_file_selection(
        self,
        *,
        conversation_id: str,
        agent_run_id: str,
        source_name: str,
        target_name: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """持久化相似文件选择卡，选择前不得生成或执行重命名计划。"""

        options: list[dict[str, Any]] = []
        choices: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates[:5], start=1):
            document_id = str(
                candidate.get("source_document_id")
                or candidate.get("document_id")
                or ""
            )
            document = self.db.get(Document, document_id)
            if document is None or document.user_id != self.user_id:
                continue
            version = (
                self.db.query(DocumentVersion)
                .filter(DocumentVersion.document_id == document.id)
                .order_by(
                    DocumentVersion.version_number.desc(),
                    DocumentVersion.created_at.desc(),
                )
                .first()
            )
            filename = str(candidate.get("filename") or document.original_filename)
            option_id = f"rename-file-{index}"
            score = _filename_similarity(source_name, filename)
            options.append(
                {
                    "id": option_id,
                    "label": filename,
                    "description": f"文件名相似度 {score:.0%}",
                    "document_id": document.id,
                    "source_filename": filename,
                    "target_filename": target_name,
                }
            )
            choices.append(
                {
                    "option_id": option_id,
                    "document_id": document.id,
                    "document_version_id": version.id if version is not None else "",
                    "working_copy_id": (
                        str(version.working_copy_id or "")
                        if version is not None
                        else ""
                    ),
                    "filename": filename,
                    "size_bytes": document.size_bytes,
                    "created_at": document.created_at.isoformat(),
                }
            )
        if not choices:
            return _error(
                "PENDING_RENAME_NOT_FOUND",
                (
                    f"没有找到名为“{source_name}”的文件，也没有可供确认的相似文件。"
                    "请重新附加要重命名的文件。"
                ),
            )
        clarification = FileSearchClarificationService(self.db).create(
            conversation_id=conversation_id,
            user_id=self.user_id,
            agent_run_id=agent_run_id,
            original_query=f"文件“{source_name}”更正为“{target_name}”",
            core_phrase=source_name[:120],
            relation_mode="RENAME_DOCUMENT_SELECTION",
            options=options,
        )
        return {
            "ok": True,
            "kind": "file_selection",
            "status": "NEEDS_CLARIFICATION",
            "message": (
                f"没有精确找到“{source_name}”。"
                "以下文件名称较相似，请选择要重命名的具体文件。"
            ),
            "clarification_id": clarification.id,
            "choices": choices,
            "operation_plan_id": None,
        }

    def _parse_corrections(
        self,
        *,
        message: str,
        pending: list[dict[str, Any]],
    ) -> list[tuple[str, str]] | dict[str, Any]:
        """解析逐行更正，并对模板占位文字给出具体文件名提示。"""

        corrections: list[tuple[str, str]] = []
        lines = [
            line.strip()
            for line in message.replace("；", "\n").splitlines()
            if line.strip()
        ]
        for line in lines:
            matched = _CORRECTION_PATTERN.match(line)
            if not matched:
                return _error(
                    "RENAME_CORRECTION_REQUIRED",
                    self._format_hint(pending, prefix="没有识别到实际的新文件名。"),
                )
            source = _strip_quotes(matched.group("source"))
            target = _strip_quotes(matched.group("target"))
            if target in _TARGET_PLACEHOLDERS:
                return _error(
                    "RENAME_PLACEHOLDER_NOT_REPLACED",
                    self._format_hint(
                        pending,
                        prefix="“原文件名/新文件名”是格式占位词，不能原样发送。",
                    ),
                )
            corrections.append((source, target))
        return corrections

    @staticmethod
    def _match_pending(
        *,
        pending: list[dict[str, Any]],
        source_name: str,
    ) -> list[dict[str, Any]]:
        """按完整文件名匹配；只有唯一待确认项时允许“这个文件”。"""

        if source_name in {"这个文件", "该文件"} and len(pending) == 1:
            return pending
        return [
            item
            for item in pending
            if str(item.get("filename") or "") == source_name
        ]

    @staticmethod
    def _format_hint(
        pending: list[dict[str, Any]],
        *,
        prefix: str,
    ) -> str:
        """生成带真实原文件名的更正格式，不再展示可误复制的空模板。"""

        filename = str((pending[0] if pending else {}).get("filename") or "当前文件")
        suffix = Path(filename).suffix
        return (
            f"{prefix} 请把尖括号内容替换成实际名称，例如："
            f"文件“{filename}”更正为“<请填写实际名称>{suffix}”。"
        )

    def _invalidate_previous_plan(
        self,
        *,
        conversation_id: str,
        source_agent_run_id: str,
    ) -> None:
        """同一待确认来源再次更正时废弃旧计划，防止两个名称都可执行。"""

        plans = (
            self.db.query(OperationPlan)
            .filter(
                OperationPlan.user_id == self.user_id,
                OperationPlan.conversation_id == conversation_id,
                OperationPlan.operation_type == DEFERRED_UPLOAD_RENAME_OPERATION,
                OperationPlan.status.in_(("PLANNED", "WAITING_CONFIRMATION")),
            )
            .all()
        )
        for plan in plans:
            if str((plan.plan_json or {}).get("source_rename_agent_run_id") or "") == source_agent_run_id:
                plan.status = "INVALIDATED"


def _validate_target_filename(*, source_filename: str, requested_name: str) -> str:
    """校验用户实际名称，保留源扩展名并拒绝 Windows/Unix 非法字符。"""

    target = _strip_quotes(requested_name).strip()
    if not target or target in {".", ".."}:
        raise ValueError("请提供实际的新文件名。")
    if target in _TARGET_PLACEHOLDERS or "<" in target or ">" in target:
        raise ValueError("请把新文件名占位内容替换为实际名称。")
    if any(character in target for character in '/\\:*?"|'):
        raise ValueError("新文件名包含系统不允许的字符。")
    if target.startswith("."):
        raise ValueError("新文件名不能是隐藏文件名。")
    source_suffix = Path(source_filename).suffix
    requested_suffix = Path(target).suffix
    if requested_suffix and requested_suffix.lower() != source_suffix.lower():
        raise ValueError("新文件名不能改变原文件扩展名。")
    if not requested_suffix:
        target = f"{target}{source_suffix}"
    if len(target.encode("utf-8")) > 240:
        raise ValueError("新文件名超过 240 字节限制。")
    return target


def _strip_quotes(value: str) -> str:
    """移除文件名两侧常见引号和书名号。"""

    return value.strip().strip("\"'“”‘’《》")


def _similar_rename_candidates(
    *,
    source_name: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按文件名召回少量相似候选，宽泛占位名只用于请求用户选择。"""

    normalized_source = _normalize_filename(source_name)
    placeholder = normalized_source in {
        _normalize_filename(value) for value in _SOURCE_PLACEHOLDERS
    }
    ranked = sorted(
        (
            (
                _filename_similarity(
                    source_name,
                    str(candidate.get("filename") or ""),
                ),
                candidate,
            )
            for candidate in candidates
            if str(candidate.get("filename") or "")
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if placeholder:
        return [candidate for _, candidate in ranked[:5]]
    return [candidate for score, candidate in ranked if score >= 0.45][:5]


def _filename_similarity(left: str, right: str) -> float:
    """结合连续字符顺序和字符集合重叠计算确定性文件名相似度。"""

    normalized_left = _normalize_filename(left)
    normalized_right = _normalize_filename(right)
    if not normalized_left or not normalized_right:
        return 0.0
    sequence_score = SequenceMatcher(
        None,
        normalized_left,
        normalized_right,
    ).ratio()
    left_chars = set(normalized_left)
    right_chars = set(normalized_right)
    overlap_score = len(left_chars & right_chars) / max(
        1,
        len(left_chars | right_chars),
    )
    return max(sequence_score, overlap_score * 0.9)


def _normalize_filename(value: str) -> str:
    """归一化文件名主体，避免扩展名、空白和标点妨碍相似召回。"""

    stem = Path(_strip_quotes(value)).stem
    normalized = unicodedata.normalize("NFKC", stem).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _error(code: str, message: str) -> dict[str, Any]:
    """构造普通用户可见的待确认处理错误。"""

    return {
        "ok": False,
        "kind": "rename_review_resolution",
        "status": "NEEDS_REVIEW",
        "error": {"code": code, "message": message},
        "dismissed_count": 0,
        "accepted_count": 0,
        "remaining_review_count": 0,
        "operation_plan_id": None,
        "completed_items": [],
        "failed_items": [],
        "ambiguous_items": [],
    }
