"""文件检索完整性评估服务。

本模块基于活动工作副本和当前受管原始文件修订的可重建检索资料给出确定性覆盖
结论。它不会根据 LLM 推测“业务文件是否全部相关”，也不会读取文件正文或修改
任何业务事实；因此“已找全”仅表示当前唯一确定的范围、检索条件和索引能力下
没有已知缺口。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, aliased

from app.core.logging import format_exception_traceback, log_event
from app.db.models import (
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentSearchProfile,
    WorkingCopy,
    ManagedFile,
    ManagedFileRevision,
    ManagedFileSearchProfile,
    ManagedRoot,
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

    def attach_safely(
        self,
        *,
        result: dict[str, Any],
        scope: Any,
        unresolved_document_count: int = 0,
    ) -> dict[str, Any]:
        """隔离完整性统计故障，避免辅助统计把有效检索升级为 HTTP 500。"""

        try:
            # PostgreSQL 的 statement timeout 会把当前事务标记为 aborted。
            # 完整性统计属于只读辅助信息，必须在 savepoint 内运行，失败后才能
            # 继续保存 ToolInvocation、AgentRun 和已有检索结果。
            with self.db.begin_nested():
                return self.attach(
                    result=result,
                    scope=scope,
                    unresolved_document_count=unresolved_document_count,
                )
        except Exception as exc:
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
            completeness = self._payload(
                status="UNVERIFIABLE",
                scope_label=scope_label,
                eligible_file_count=0,
                ready_file_count=0,
                pending_file_count=0,
                failed_file_count=0,
                candidate_limit_reached=bool(result.get("candidate_limit_reached")),
                message=(
                    "当前命中结果已返回，但完整性统计暂时不可用，"
                    "因此不能确认结果已找全。"
                ),
            )
            log_event(
                "retrieval.completeness.failed",
                level="ERROR",
                tool_name="hybrid-search",
                status="DEGRADED",
                workspace_id=self.workspace_id,
                error_code=exc.__class__.__name__,
                exception_traceback=format_exception_traceback(exc),
                message="文件检索完整性统计失败，保留已有命中并安全降级",
            )
            return {
                **result,
                "partial": True,
                "search_completeness": completeness,
            }

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

        copy_filters = [
            WorkingCopy.workspace_id == self.workspace_id,
            WorkingCopy.status == "ACTIVE",
        ]
        if scope_mode == "strict":
            copy_filters.append(
                WorkingCopy.document_id.in_(strict_document_ids)
            )
        copy_count = int(
            self.db.query(sa.func.count(WorkingCopy.id))
            .filter(*copy_filters)
            .scalar()
            or 0
        )
        # 使用显式 JOIN + COUNT DISTINCT，让 PostgreSQL 能按现有外键/状态索引
        # 制定集合计划；不能对每个工作副本执行相关 EXISTS 子查询。
        ready_file_count = int(
            self.db.query(sa.func.count(sa.distinct(WorkingCopy.id)))
            .join(
                DocumentSearchProfile,
                sa.and_(
                    DocumentSearchProfile.working_copy_id == WorkingCopy.id,
                    DocumentSearchProfile.document_version_id
                    == WorkingCopy.current_version_id,
                    DocumentSearchProfile.status == "ACTIVE",
                ),
            )
            .join(
                DocumentIndexRun,
                sa.and_(
                    DocumentIndexRun.document_version_id
                    == WorkingCopy.current_version_id,
                    DocumentIndexRun.status == "COMPLETED",
                    DocumentIndexRun.index_version == INDEX_VERSION,
                ),
            )
            .filter(*copy_filters)
            .scalar()
            or 0
        )
        extraction_state = (
            self.db.query(
                DocumentExtractionRun.document_version_id.label("version_id"),
                sa.func.max(
                    sa.case(
                        (DocumentExtractionRun.status == "FAILED", 1),
                        else_=0,
                    )
                ).label("has_failed"),
                sa.func.max(
                    sa.case(
                        (DocumentExtractionRun.status == "COMPLETED", 1),
                        else_=0,
                    )
                ).label("has_completed"),
            )
            .filter(DocumentExtractionRun.document_version_id.is_not(None))
            .group_by(DocumentExtractionRun.document_version_id)
            .subquery()
        )
        failed_file_count = int(
            self.db.query(sa.func.count(WorkingCopy.id))
            .join(
                extraction_state,
                extraction_state.c.version_id == WorkingCopy.current_version_id,
            )
            .filter(
                *copy_filters,
                extraction_state.c.has_failed == 1,
                extraction_state.c.has_completed == 0,
            )
            .scalar()
            or 0
        )

        # 全局检索同时覆盖工作副本和当前源侧修订。严格附件范围仍只以用户已
        # 确认的工作副本为准，不能把附件外的原始文件引入完整性承诺。
        source_eligible = 0
        source_ready = 0
        source_pending = 0
        source_failed = 0
        if scope_mode != "strict":
            # 新受管根在首次命中前不会创建 ``WorkingCopyRoot``；完整性统计同样
            # 必须覆盖这些只读源文件，不能以工作副本映射是否存在作为可检索前提。
            covered_copy = aliased(WorkingCopy)
            source_base = (
                self.db.query(ManagedFileRevision.id)
                .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
                .join(ManagedRoot, ManagedRoot.id == ManagedFile.root_id)
                .outerjoin(
                    covered_copy,
                    sa.and_(
                        covered_copy.workspace_id == self.workspace_id,
                        covered_copy.status == "ACTIVE",
                        covered_copy.managed_file_id == ManagedFile.id,
                        covered_copy.imported_source_sha256
                        == ManagedFileRevision.content_sha256,
                    ),
                )
                .filter(
                    ManagedFile.status == "ACTIVE",
                    ManagedRoot.enabled.is_(True),
                    ManagedFileRevision.is_current.is_(True),
                    covered_copy.id.is_(None),
                )
            )
            source_eligible = int(
                source_base.with_entities(
                    sa.func.count(sa.distinct(ManagedFileRevision.id))
                ).scalar()
                or 0
            )
            source_ready = int(
                source_base.join(
                    ManagedFileSearchProfile,
                    sa.and_(
                        ManagedFileSearchProfile.managed_file_revision_id
                        == ManagedFileRevision.id,
                        ManagedFileSearchProfile.status == "ACTIVE",
                    ),
                )
                .filter(ManagedFileRevision.status == "READY")
                .with_entities(
                    sa.func.count(sa.distinct(ManagedFileRevision.id))
                )
                .scalar()
                or 0
            )
            source_failed = int(
                source_base.filter(ManagedFileRevision.status == "FAILED")
                .with_entities(
                    sa.func.count(sa.distinct(ManagedFileRevision.id))
                )
                .scalar()
                or 0
            )
            source_pending = max(
                0,
                source_eligible - source_ready - source_failed,
            )

        eligible_file_count = copy_count + source_eligible
        ready_file_count += source_ready
        failed_file_count += source_failed
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
