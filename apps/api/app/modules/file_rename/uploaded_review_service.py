"""上传文件低置信度命名建议的对话更正服务。

待确认事实来自已持久化的 ``generate-rename-suggestions`` ToolInvocation；用户提供实际
新文件名后创建延后工作副本计划。该服务不修改上传原件，也不复用已退役的受管原件执行器。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AgentRun, Document, OperationPlan, ToolInvocation
from app.modules.file_lifecycle.operations import DEFERRED_UPLOAD_RENAME_OPERATION
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.operations.repository import OperationPlanRepository


_CORRECTION_PATTERN = re.compile(
    r"^\s*(?:文件\s*)?(?P<source>.+?)\s*(?:更正为|改为|重命名为)\s*(?P<target>.+?)\s*[。；;]?\s*$"
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
        if not pending:
            return _error(
                "PENDING_RENAME_NOT_FOUND",
                "当前会话没有仍待确认的上传文件，请重新选择文件后生成重命名计划。",
            )
        normalized = message.strip().rstrip("。！!")
        if normalized in _DISMISS_MESSAGES:
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
        corrections = self._parse_corrections(message=message, pending=pending)
        if isinstance(corrections, dict):
            return corrections

        items: list[dict[str, Any]] = []
        used_source_ids: set[str] = set()
        for source_name, target_name in corrections:
            matches = self._match_pending(
                pending=pending,
                source_name=source_name,
            )
            if len(matches) != 1:
                if not matches:
                    return _error(
                        "PENDING_RENAME_NOT_FOUND",
                        self._format_hint(
                            pending,
                            prefix=f"没有找到名为“{source_name}”的待确认文件。",
                        ),
                    )
                return _error(
                    "PENDING_RENAME_AMBIGUOUS",
                    "存在多个同名待确认文件，请逐个选择文件并使用完整文件名确认。",
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

        source_run_id = str(pending[0].get("_source_agent_run_id") or "")
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
                "source_rename_agent_run_id": source_run_id,
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
            if source in _SOURCE_PLACEHOLDERS or target in _TARGET_PLACEHOLDERS:
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

        filename = str(pending[0].get("filename") or "当前文件")
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
