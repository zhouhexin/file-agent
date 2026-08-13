"""文件检索完整性评估服务。

本模块只基于活动工作副本及其可重建检索派生数据给出确定性覆盖结论。它不会根据
LLM 推测“业务文件是否全部相关”，也不会读取文件正文或修改任何业务事实；因此
“已找全”仅表示当前唯一确定的范围、检索条件和索引能力下没有已知缺口。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import (
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentSearchProfile,
    WorkingCopy,
)
from app.modules.chunks.service import INDEX_VERSION


class SearchCompletenessService:
    """汇总当前检索范围的索引覆盖状态。

    该服务只能用于用户回执和受控 Tool 输出。它不决定相关性、不放宽范围，也不把
    尚未就绪或已失败文件隐藏成“没有命中”，从而避免系统对用户错误承诺检索已找全。
    """

    def __init__(self, *, db: Session, workspace_id: str) -> None:
        """注入请求级数据库会话与已经完成权限解析的共享工作区。"""

        self.db = db
        self.workspace_id = workspace_id

    def attach(
        self,
        *,
        result: dict[str, Any],
        scope: Any,
        unresolved_document_count: int = 0,
    ) -> dict[str, Any]:
        """在既有搜索结果上附加安全的 ``search_completeness`` 用户投影。

        ``result`` 仍由检索服务负责事实命中；本方法只说明结果能否覆盖当前范围。
        若附件尚未映射到活动工作副本或用户需要先完成范围选择，必须明确返回
        ``UNVERIFIABLE``，不能把空结果误报为已找全。
        """

        completeness = self.assess(
            scope=scope,
            result=result,
            unresolved_document_count=unresolved_document_count,
        )
        return {**result, "search_completeness": completeness}

    def assess(
        self,
        *,
        scope: Any,
        result: dict[str, Any],
        unresolved_document_count: int = 0,
    ) -> dict[str, Any]:
        """计算 ``COMPLETE``、``PROCESSING``、``PARTIAL`` 或 ``UNVERIFIABLE``。

        这里的 COMPLETE 只代表范围内的活动文件均具备当前索引版本的检索派生数据，
        且本次检索没有降级、候选保护上限或范围歧义；不代表系统理解了全部业务语义。
        """

        scope_mode = str(getattr(scope, "scope_mode", "global") or "global")
        strict_document_ids = list(
            dict.fromkeys(
                str(value)
                for value in (getattr(scope, "strict_document_ids", ()) or ())
                if str(value)
            )
        )
        scope_label = (
            f"本次确认的 {len(strict_document_ids)} 份文件"
            if scope_mode == "strict" and strict_document_ids
            else "当前共享工作区全部活动文件"
        )

        if (
            isinstance(result.get("search_clarification"), dict)
            or unresolved_document_count > 0
            or (scope_mode == "strict" and not strict_document_ids)
        ):
            payload = self._payload(
                status="UNVERIFIABLE",
                scope_label=scope_label,
                eligible_file_count=0,
                ready_file_count=0,
                pending_file_count=0,
                failed_file_count=0,
                candidate_limit_reached=False,
                message="本次文件范围尚未唯一确认，暂时无法判断结果是否找全。",
            )
            self._log_assessment(payload)
            return payload

        copies_query = self.db.query(
            WorkingCopy.id,
            WorkingCopy.document_id,
            WorkingCopy.current_version_id,
        ).filter(
            WorkingCopy.workspace_id == self.workspace_id,
            WorkingCopy.status == "ACTIVE",
        )
        if scope_mode == "strict":
            copies_query = copies_query.filter(
                WorkingCopy.document_id.in_(strict_document_ids)
            )
        copies = copies_query.all()
        working_copy_ids = [str(row.id) for row in copies]
        version_ids = [
            str(row.current_version_id)
            for row in copies
            if row.current_version_id is not None
        ]
        document_by_version = {
            str(row.current_version_id): str(row.document_id)
            for row in copies
            if row.current_version_id is not None
        }

        profile_ready_pairs: set[tuple[str, str]] = set()
        index_ready_versions: set[str] = set()
        failed_versions: set[str] = set()
        successful_extraction_versions: set[str] = set()
        if working_copy_ids:
            profile_ready_pairs = {
                (str(working_copy_id), str(document_version_id))
                for working_copy_id, document_version_id in self.db.query(
                    DocumentSearchProfile.working_copy_id,
                    DocumentSearchProfile.document_version_id,
                )
                .filter(
                    DocumentSearchProfile.working_copy_id.in_(working_copy_ids),
                    DocumentSearchProfile.status == "ACTIVE",
                )
                .all()
            }
        if version_ids:
            index_ready_versions = {
                str(row.document_version_id)
                for row in self.db.query(DocumentIndexRun.document_version_id)
                .filter(
                    DocumentIndexRun.document_version_id.in_(version_ids),
                    DocumentIndexRun.status == "COMPLETED",
                    DocumentIndexRun.index_version == INDEX_VERSION,
                )
                .all()
            }
            extraction_rows = self.db.query(
                DocumentExtractionRun.document_version_id,
                DocumentExtractionRun.status,
            ).filter(
                DocumentExtractionRun.document_version_id.in_(version_ids),
                DocumentExtractionRun.status.in_(("COMPLETED", "FAILED")),
            ).all()
            successful_extraction_versions = {
                str(version_id)
                for version_id, status in extraction_rows
                if version_id is not None and status == "COMPLETED"
            }
            failed_versions = {
                str(version_id)
                for version_id, status in extraction_rows
                if version_id is not None and status == "FAILED"
            }

        ready_file_count = 0
        failed_file_count = 0
        for copy in copies:
            version_id = str(copy.current_version_id or "")
            ready = (
                (str(copy.id), version_id) in profile_ready_pairs
                and bool(version_id)
                and version_id in index_ready_versions
            )
            if ready:
                ready_file_count += 1
                continue
            # 只有当前版本没有成功解析、同时已出现失败解析才视为已知失败；历史失败
            # 不能掩盖后续成功重处理。
            if (
                version_id
                and version_id in failed_versions
                and version_id not in successful_extraction_versions
                and document_by_version.get(version_id)
            ):
                failed_file_count += 1

        eligible_file_count = len(copies)
        pending_file_count = max(
            0,
            eligible_file_count - ready_file_count - failed_file_count,
        )
        candidate_limit_reached = bool(result.get("candidate_limit_reached"))
        degraded = bool(result.get("partial"))
        if candidate_limit_reached or degraded or failed_file_count:
            status = "PARTIAL"
            message = self._partial_message(
                scope_label=scope_label,
                eligible_file_count=eligible_file_count,
                ready_file_count=ready_file_count,
                failed_file_count=failed_file_count,
                candidate_limit_reached=candidate_limit_reached,
                degraded=degraded,
            )
        elif pending_file_count:
            status = "PROCESSING"
            message = (
                f"已检索 {scope_label}中的 {ready_file_count}/{eligible_file_count} 份文件；"
                f"另有 {pending_file_count} 份文件正在准备检索，暂时不能确认结果已找全。"
            )
        else:
            status = "COMPLETE"
            message = (
                f"已完成{scope_label}中 {eligible_file_count} 份活动文件的检索；"
                "当前条件下结果已找全。"
            )
        payload = self._payload(
            status=status,
            scope_label=scope_label,
            eligible_file_count=eligible_file_count,
            ready_file_count=ready_file_count,
            pending_file_count=pending_file_count,
            failed_file_count=failed_file_count,
            candidate_limit_reached=candidate_limit_reached,
            message=message,
        )
        self._log_assessment(payload)
        return payload

    def _log_assessment(self, payload: dict[str, Any]) -> None:
        """记录运维可读的覆盖结论，不写入查询正文、文件名或内部物理路径。"""

        log_event(
            "retrieval.completeness.assessed",
            tool_name="hybrid-search",
            status=str(payload["status"]),
            workspace_id=self.workspace_id,
            eligible_file_count=int(payload["eligible_file_count"]),
            ready_file_count=int(payload["ready_file_count"]),
            pending_file_count=int(payload["pending_file_count"]),
            failed_file_count=int(payload["failed_file_count"]),
            candidate_limit_reached=bool(payload["candidate_limit_reached"]),
            message="文件检索完整性评估完成",
            event_title="文件检索完整性",
            stage="SEARCH",
            operator_message=str(payload["message"]),
        )

    @staticmethod
    def _payload(
        *,
        status: str,
        scope_label: str,
        eligible_file_count: int,
        ready_file_count: int,
        pending_file_count: int,
        failed_file_count: int,
        candidate_limit_reached: bool,
        message: str,
    ) -> dict[str, Any]:
        """构造不含文件 ID、路径、任务 ID 的普通用户安全投影。"""

        return {
            "status": status,
            "can_claim_complete": status == "COMPLETE",
            "scope_label": scope_label,
            "eligible_file_count": eligible_file_count,
            "ready_file_count": ready_file_count,
            "pending_file_count": pending_file_count,
            "failed_file_count": failed_file_count,
            "candidate_limit_reached": candidate_limit_reached,
            "message": message,
        }

    @staticmethod
    def _partial_message(
        *,
        scope_label: str,
        eligible_file_count: int,
        ready_file_count: int,
        failed_file_count: int,
        candidate_limit_reached: bool,
        degraded: bool,
    ) -> str:
        """把确定性缺口合成为可执行但不暴露内部实现的提示。"""

        reasons: list[str] = []
        if failed_file_count:
            reasons.append(f"{failed_file_count} 份文件暂时无法建立检索资料")
        if candidate_limit_reached:
            reasons.append("本次匹配候选达到保护上限")
        if degraded:
            reasons.append("部分检索资料当前不可用")
        reason_text = "；".join(reasons) or "当前检索存在未确认缺口"
        return (
            f"已检索 {scope_label}中的 {ready_file_count}/{eligible_file_count} 份文件；"
            f"{reason_text}，暂时不能确认结果已找全。"
        )
