"""相关文件集合与回答后工作副本优先同步协调服务。

集合只接收检索最终结果或已验证证据来源，不能接收扩大召回候选。该服务复用并提升
全量物化任务，因而不会阻塞聊天回复，也不会让 Planner 直接操作受管原始目录。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.db.models import FilesystemJob, RelevantFileSet, RelevantFileSetItem
from app.modules.managed_files.jobs import FilesystemJobQueue


class RelevantFileSetService:
    """固化最终相关文件，并提升未物化源修订的幂等任务。"""

    def __init__(self, *, db: Session, settings: Settings | None = None) -> None:
        """保存请求事务和配置；任务仅在当前事务提交后由 worker 消费。"""

        self.db = db
        self.settings = settings or get_settings()

    def persist_and_enqueue(
        self,
        *,
        workspace_id: str,
        user_id: str,
        conversation_id: str | None,
        agent_run_id: str | None,
        query: str,
        results: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """仅将最终相关结果持久化，并提交或提升对应源修订的物化任务。"""

        final_results = [
            item for item in results
            if isinstance(item, dict)
            # 没有分级字段的普通检索结果仅是基础召回，不能自动批量复制；
            # 只有已验证相关、或明确文件名精确命中的单文件结果才可物化。
            # 用户可见的“可能相关”已经完成本轮受控召回和排序，属于本方案定义
            # 的最终结果；它与仅用于内部扩大召回、从未返回给用户的候选不同。
            and str(item.get("relevance_tier") or "") in {"SUPPORTED", "RELATED", "POSSIBLE"}
            and (item.get("working_copy_id") or item.get("managed_file_revision_id"))
        ]
        if not final_results:
            return None
        record = RelevantFileSet(
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            query_fingerprint=hashlib.sha256(str(query).strip().casefold().encode("utf-8")).hexdigest(),
            status="READY",
        )
        self.db.add(record)
        self.db.flush()
        revisions: list[str] = []
        seen_revisions: set[str] = set()
        items_by_revision: dict[str, RelevantFileSetItem] = {}
        for rank, item in enumerate(final_results, start=1):
            revision_id = str(item.get("managed_file_revision_id") or "") or None
            if revision_id and revision_id in seen_revisions:
                continue
            if revision_id:
                seen_revisions.add(revision_id)
                revisions.append(revision_id)
            relevant_item = RelevantFileSetItem(
                relevant_file_set_id=record.id,
                managed_file_id=str(item.get("managed_file_id") or "") or None,
                managed_file_revision_id=revision_id,
                working_copy_id=str(item.get("working_copy_id") or "") or None,
                resource_type=str(item.get("resource_type") or "WORKING_COPY"),
                relevance_tier=str(item.get("relevance_tier") or "RELATED"),
                rank=rank,
                status="READY",
            )
            self.db.add(relevant_item)
            if revision_id:
                items_by_revision[revision_id] = relevant_item
        self.db.flush()
        job_ids: list[str] = []
        if self.settings.materialize_relevant_files_after_response:
            queue = FilesystemJobQueue(self.db)
            batch_size = max(1, int(self.settings.materialize_relevant_files_batch_size))
            # 分批入队仅控制一次数据库事务内的对象数量；集合本身不截断，确保
            # 翻页未展示的最终相关文件同样会得到工作副本。
            for start in range(0, len(revisions), batch_size):
                for revision_id in revisions[start : start + batch_size]:
                    job = queue.create_job(
                        job_type="MATERIALIZE_WORKING_COPY",
                        root_id=None,
                        created_by=user_id,
                        payload={
                            "managed_file_revision_id": revision_id,
                            "relevant_file_set_id": record.id,
                        },
                        queue_name="MATERIALIZE",
                        deduplication_key=f"working-copy-materialize:{workspace_id}:{revision_id}",
                        priority=self.settings.materialize_working_copy_priority,
                    )
                    if (
                        self.db.bind is not None
                        and self.db.bind.dialect.name == "postgresql"
                    ):
                        # 后台任务可能已经 RUNNING。锁住任务行直到本请求提交，保证
                        # worker 的完成更新发生在 RelevantFileSetItem 对其他事务可见
                        # 之后；否则极小文件可能先完成，集合项会永久停在处理中。
                        job = (
                            self.db.query(FilesystemJob)
                            .filter(FilesystemJob.id == job.id)
                            .with_for_update()
                            .one()
                        )
                    # 启动全量同步可能已经创建同一幂等任务。用户命中后必须提升
                    # 原任务而非并发复制；只有尚未领取时才补入集合 ID 供完成回写。
                    queue.promote_pending_job(
                        job=job,
                        priority=self.settings.materialize_working_copy_priority,
                    )
                    relevant_item = items_by_revision.get(revision_id)
                    if job.status == "PENDING":
                        job.payload_json = {
                            **dict(job.payload_json or {}),
                            "managed_file_revision_id": revision_id,
                            "relevant_file_set_id": record.id,
                            "materialization_reason": "user-relevant",
                        }
                    elif relevant_item is not None and job.status == "RUNNING":
                        relevant_item.status = "MATERIALIZING"
                    elif relevant_item is not None and job.status == "COMPLETED":
                        relevant_item.status = "MATERIALIZED"
                        relevant_item.working_copy_id = str(
                            (job.result_json or {}).get("working_copy_id") or ""
                        ) or relevant_item.working_copy_id
                    elif relevant_item is not None and job.status == "FAILED":
                        relevant_item.status = "FAILED"
                    job_ids.append(job.id)
        log_event(
            "working_copy.materialization.queued",
            settings=self.settings,
            status="PENDING",
            workspace_id=workspace_id,
            relevant_file_set_id=record.id,
            relevant_file_count=len(final_results),
            source_revision_count=len(revisions),
            job_count=len(job_ids),
            message="最终相关文件集合已固化，源文件工作副本物化任务已入队",
        )
        return {"relevant_file_set_id": record.id, "materialization_job_ids": job_ids}
