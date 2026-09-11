"""WorkBuddy 批次重复确认适配服务。

该服务复用现有上传查重候选和归档动作，但以 ``ingest_item_id + review_revision + candidate_id``
固定用户选择。MCP 不得直接提交工作副本 ID，也不能根据自然语言猜测候选。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models import (
    IngestBatch,
    IngestDuplicateGroup,
    IngestDuplicateGroupMember,
    IngestItem,
    UploadDuplicateCandidate,
    UploadDuplicateReview,
    User,
    WorkingCopy,
    utcnow,
)
from app.core.config import get_settings
from app.modules.file_lifecycle.schemas import DuplicateDecisionRequest
from app.modules.file_lifecycle.service import UploadLifecycleService
from app.modules.ingestion.repository import IngestionRepository
from app.modules.ingestion.schemas import (
    IngestDuplicateCandidateResponse,
    IngestDuplicateDecisionRequest,
    IngestDuplicateDecisionResponse,
    IngestDuplicateReviewResponse,
)
from app.modules.ingestion.service import IngestionService
from app.modules.ingestion.workflow import IngestionWorkflow


class IngestionDuplicateService:
    """读取和提交当前用户批次条目的版本化重复决定。"""

    def __init__(self, db: Session) -> None:
        """保存请求级 Session 并复用生命周期 Service。"""

        self.db = db
        self.repository = IngestionRepository(db)
        self.lifecycle = UploadLifecycleService(db)

    def get_review(self, *, item_id: str, current_user: User) -> IngestDuplicateReviewResponse:
        """返回待选择或已解决的结构化候选，并同步最新批次投影。"""

        batch, item, review = self._owned_context(
            item_id=item_id,
            user_id=current_user.id,
            for_update=True,
        )
        workflow_changed = IngestionWorkflow(self.db).synchronize_batch(batch)
        review_changed = False
        if review.status == "WAITING_CONFIRMATION" and review.comparison_phase == "EXACT":
            # 查重任务写完候选后，首次读取把候选集冻结为一个新修订。
            review.comparison_phase = "EXACT_AND_NEAR"
            review.revision += 1
            review.decision_scope_json = {
                **dict(review.decision_scope_json or {}),
                "candidate_ids": [
                    candidate.id
                    for candidate in self._candidates(review_id=review.id)
                ],
            }
            review_changed = True
        if workflow_changed or review_changed:
            if review_changed and not workflow_changed:
                batch.result_revision += 1
            self.db.commit()
            self.db.refresh(review)
        return self._to_review(item=item, review=review)

    def decide(
        self,
        *,
        item_id: str,
        request: IngestDuplicateDecisionRequest,
        current_user: User,
    ) -> IngestDuplicateDecisionResponse:
        """核验修订和候选后记录决定；同键不同载荷必须冲突。"""

        # 集成幂等键以用户为命名空间。先锁用户记录可让“查询旧请求 → 执行业务 → 写入
        # IntegrationRequest”成为串行临界区，避免两个并发请求都查不到记录后撞唯一约束。
        self.db.query(User).filter(User.id == current_user.id).with_for_update().one()
        fingerprint = self._decision_fingerprint(item_id=item_id, request=request)
        existing_request = self.repository.find_integration_request(
            user_id=current_user.id,
            client_id=request.client_id,
            idempotency_key=request.idempotency_key,
        )
        if existing_request is not None:
            if (
                existing_request.operation != "INGEST_DUPLICATE_DECISION"
                or existing_request.payload_digest != fingerprint
                or str(existing_request.target_refs_json.get("ingest_item_id") or "") != item_id
            ):
                self._raise_error(409, "IDEMPOTENCY_CONFLICT", "同一幂等键已经用于不同的重复决定。")
            return self._response_for_replay(
                item_id=item_id,
                user_id=current_user.id,
                reused=True,
            )

        batch, item, review = self._owned_context(
            item_id=item_id,
            user_id=current_user.id,
            for_update=True,
        )
        if review.id != request.review_id:
            self._raise_error(409, "DUPLICATE_REVIEW_CHANGED", "重复确认对象已经变化，请刷新后重新选择。")
        if review.revision != request.review_revision:
            self._raise_error(
                409,
                "DUPLICATE_REVIEW_REVISION_CONFLICT",
                "重复候选已经更新，请刷新后重新选择。",
                details={"current_revision": review.revision},
            )
        self._validate_group_revision(
            review=review,
            requested_revision=request.group_revision,
            requested_member_ids=request.group_member_item_ids,
        )
        candidate = self._selected_candidate(review=review, candidate_id=request.candidate_id)
        if request.decision == "WAIT_AND_REUSE" and (
            candidate is None or not candidate.candidate_ingest_item_id
        ):
            self._raise_error(409, "WAIT_AND_REUSE_NOT_AVAILABLE", "当前候选不是同批主任务，不能等待复用。")
        selected_working_copy_id = (
            candidate.candidate_working_copy_id
            if candidate is not None and request.decision == "USE_EXISTING_FILE"
            else None
        )
        if request.decision == "USE_EXISTING_FILE" and not selected_working_copy_id:
            self._raise_error(409, "EXISTING_FILE_NOT_READY", "所选候选尚未形成可复用文件。")
        if request.decision == "USE_EXISTING_FILE" and candidate is not None:
            self._validate_working_copy_snapshot(candidate)
        if review.status == "RESOLVED":
            stored_scope = dict(review.decision_scope_json or {})
            if review.decision != request.decision or stored_scope.get("candidate_id") != request.candidate_id:
                self._raise_error(409, "DUPLICATE_REVIEW_ALREADY_RESOLVED", "重复确认已经按其他选择处理。")

        if request.decision == "WAIT_AND_REUSE":
            self._validate_wait_decision(item=item, review=review, candidate=candidate)
            review.status = "RESOLVED"
            review.decision = request.decision
            review.decided_at = utcnow()
            item.status = "WAITING_EXISTING_RESULT"
            item.stage = "WAIT_EXISTING"
            item.decision = request.decision
            primary = self.db.get(IngestItem, candidate.candidate_ingest_item_id)
            item.current_job_id = primary.current_job_id if primary else None
            filesystem_job_id = item.current_job_id
            self.db.flush()
        else:
            legacy_result = self.lifecycle.decide(
                upload_version_id=str(item.upload_document_version_id),
                request=DuplicateDecisionRequest(
                    duplicate_review_id=review.id,
                    decision=request.decision,
                    selected_existing_working_copy_id=selected_working_copy_id,
                ),
                current_user=current_user,
            )
            filesystem_job_id = legacy_result.filesystem_job_id
        review = self.db.get(UploadDuplicateReview, review.id)
        if review is None:
            self._raise_error(409, "DUPLICATE_REVIEW_MISSING", "重复确认状态丢失。")
        review.selected_candidate_id = candidate.id if candidate else None
        review.revision += 1
        review.decision_scope_json = {
            **dict(review.decision_scope_json or {}),
            "candidate_id": candidate.id if candidate else None,
            "decision": request.decision,
        }
        item.decision = request.decision
        workflow_changed = IngestionWorkflow(self.db).synchronize_batch(batch)
        self.repository.create_integration_request(
            user_id=current_user.id,
            client_id=request.client_id,
            request_id=request.request_id,
            operation="INGEST_DUPLICATE_DECISION",
            idempotency_key=request.idempotency_key,
            payload_digest=fingerprint,
            target_refs_json={
                "batch_id": batch.id,
                "ingest_item_id": item.id,
                "review_id": review.id,
            },
            status="COMPLETED",
            result_json={
                "decision": request.decision,
                "candidate_id": request.candidate_id,
                "filesystem_job_id": filesystem_job_id,
            },
        )
        if not workflow_changed:
            batch.result_revision += 1
        self.db.commit()
        return self._build_response(
            batch=batch,
            item=item,
            review=review,
            filesystem_job_id=filesystem_job_id,
            reused=False,
        )

    def _response_for_replay(
        self,
        *,
        item_id: str,
        user_id: str,
        reused: bool,
    ) -> IngestDuplicateDecisionResponse:
        """从数据库事实重建幂等响应，不信任旧请求保存的展示 JSON。"""

        batch, item, review = self._owned_context(item_id=item_id, user_id=user_id)
        IngestionWorkflow(self.db).synchronize_batch(batch)
        self.db.commit()
        archive_job_id = None
        if item.archive_record_id:
            from app.db.models import UploadArchiveRecord

            archive = self.db.get(UploadArchiveRecord, item.archive_record_id)
            archive_job_id = archive.filesystem_job_id if archive else None
        return self._build_response(
            batch=batch,
            item=item,
            review=review,
            filesystem_job_id=archive_job_id,
            reused=reused,
        )

    def _build_response(
        self,
        *,
        batch: IngestBatch,
        item: IngestItem,
        review: UploadDuplicateReview,
        filesystem_job_id: str | None,
        reused: bool,
    ) -> IngestDuplicateDecisionResponse:
        """统一投影决定响应。"""

        ingestion = IngestionService(self.db)
        return IngestDuplicateDecisionResponse(
            review=self._to_review(item=item, review=review),
            item=ingestion._to_item_response(item),
            batch=ingestion._to_batch_response(batch),
            filesystem_job_id=filesystem_job_id,
            reused=reused,
        )

    def _owned_context(
        self,
        *,
        item_id: str,
        user_id: str,
        for_update: bool = False,
    ) -> tuple[IngestBatch, IngestItem, UploadDuplicateReview]:
        """加载当前用户条目及其确认记录，并在决定入口串行化修订校验。

        PostgreSQL 下的决定事务必须先锁批次和条目，再锁 review；所有调用保持同一顺序，
        防止并发确认以相同 revision 写入不同结果。只读查询不加锁。
        """

        item_query = self.db.query(IngestItem).filter(IngestItem.id == item_id)
        if for_update:
            item_query = item_query.with_for_update()
        item = item_query.one_or_none()
        batch_query = (
            self.db.query(IngestBatch).filter(IngestBatch.id == item.batch_id)
            if item is not None
            else None
        )
        if batch_query is not None and for_update:
            batch_query = batch_query.with_for_update()
        batch = batch_query.one_or_none() if batch_query is not None else None
        if item is None or batch is None or batch.user_id != user_id or not item.upload_document_version_id:
            self._raise_error(404, "INGEST_ITEM_NOT_FOUND", "导入条目不存在或无权访问。")
        review_query = self.db.query(UploadDuplicateReview).filter(
            UploadDuplicateReview.upload_document_version_id == item.upload_document_version_id
        )
        if for_update:
            review_query = review_query.with_for_update()
        review = review_query.one_or_none()
        if review is None or review.user_id != user_id:
            self._raise_error(404, "DUPLICATE_REVIEW_NOT_FOUND", "当前条目没有可访问的重复确认。")
        return batch, item, review

    def _to_review(
        self,
        *,
        item: IngestItem,
        review: UploadDuplicateReview,
    ) -> IngestDuplicateReviewResponse:
        """返回候选 ID 和脱敏摘要，不暴露上传版本或本地路径。"""

        legacy = self.lifecycle.to_review_response(review)
        scope = dict(review.decision_scope_json or {})
        group_id = str(scope.get("duplicate_group_id") or "") or None
        group = self.db.get(IngestDuplicateGroup, group_id) if group_id else None
        candidates = [
            IngestDuplicateCandidateResponse(
                candidate_id=candidate.id,
                match_type=candidate.match_type,
                match_scope=candidate.match_scope,
                similarity_score=candidate.similarity_score,
                summary=dict(candidate.user_visible_summary_json or {}),
                existing_document_id=next(
                    (
                        visible.existing_document_id
                        for visible in legacy.candidates
                        if visible.id == candidate.id
                    ),
                    None,
                ),
                comparison_available=bool(get_settings().integration_review_web_base_url),
                comparison_unavailable_reason=(
                    None if get_settings().integration_review_web_base_url else "REVIEW_WEB_URL_NOT_CONFIGURED"
                ),
                comparison_url=self._comparison_url(
                    item_id=item.id,
                    review_id=review.id,
                    review_revision=review.revision,
                    candidate_id=candidate.id,
                    group_revision=group.revision if group is not None else None,
                ),
            )
            for candidate in self._candidates(review_id=review.id)
        ]
        allowed = list(legacy.allowed_decisions)
        if any(candidate.candidate_ingest_item_id for candidate in self._candidates(review_id=review.id)):
            allowed.append("WAIT_AND_REUSE")
        members = (
            self.db.query(IngestDuplicateGroupMember)
            .filter(IngestDuplicateGroupMember.group_id == group.id)
            .order_by(
                IngestDuplicateGroupMember.joined_revision.asc(),
                IngestDuplicateGroupMember.id.asc(),
            )
            .all()
            if group is not None
            else []
        )
        return IngestDuplicateReviewResponse(
            item_id=item.id,
            review_id=review.id,
            review_revision=review.revision,
            comparison_phase=review.comparison_phase,
            status=review.status,
            expires_at=review.expires_at,
            duplicate_group_id=group.id if group is not None else None,
            group_revision=group.revision if group is not None else None,
            group_member_item_ids=[member.ingest_item_id for member in members],
            allowed_decisions=allowed,
            candidates=candidates,
        )

    @staticmethod
    def _comparison_url(*, item_id: str, review_id: str, review_revision: int, candidate_id: str, group_revision: int | None) -> str | None:
        """为已冻结候选添加部署者配置的浏览器入口，绝不从请求 Host 或客户端路径推断地址。"""

        from urllib.parse import urlencode

        base = get_settings().integration_review_web_base_url
        if not base:
            return None
        values: dict[str, str | int] = {
            "item_id": item_id, "review_id": review_id, "review_revision": review_revision, "candidate_id": candidate_id,
        }
        if group_revision is not None:
            values["group_revision"] = group_revision
        return f"{base}/duplicate-comparison?{urlencode(values)}"

    def _candidates(self, *, review_id: str) -> list[UploadDuplicateCandidate]:
        """按稳定 rank 和 ID 返回固定候选集。"""

        return (
            self.db.query(UploadDuplicateCandidate)
            .filter(UploadDuplicateCandidate.duplicate_review_id == review_id)
            .order_by(UploadDuplicateCandidate.rank.asc(), UploadDuplicateCandidate.id.asc())
            .all()
        )

    def _selected_candidate(
        self,
        *,
        review: UploadDuplicateReview,
        candidate_id: str | None,
    ) -> UploadDuplicateCandidate | None:
        """验证所选候选确实属于当前固定 review。"""

        if candidate_id is None:
            return None
        candidate = self.db.get(UploadDuplicateCandidate, candidate_id)
        if candidate is None or candidate.duplicate_review_id != review.id:
            self._raise_error(409, "DUPLICATE_CANDIDATE_CHANGED", "重复候选不存在或已经变化。")
        return candidate

    def _validate_group_revision(
        self,
        *,
        review: UploadDuplicateReview,
        requested_revision: int | None,
        requested_member_ids: list[str],
    ) -> None:
        """同批重复决定必须同时绑定用户看到的修订和完整成员集合。"""

        group_id = str((review.decision_scope_json or {}).get("duplicate_group_id") or "")
        if not group_id:
            if requested_revision is not None or requested_member_ids:
                self._raise_error(409, "DUPLICATE_GROUP_CHANGED", "当前候选不属于批内重复组。")
            return
        group = self.db.get(IngestDuplicateGroup, group_id)
        current_member_ids = (
            [
                member.ingest_item_id
                for member in self.db.query(IngestDuplicateGroupMember)
                .filter(IngestDuplicateGroupMember.group_id == group.id)
                .order_by(
                    IngestDuplicateGroupMember.joined_revision.asc(),
                    IngestDuplicateGroupMember.id.asc(),
                )
                .all()
            ]
            if group is not None
            else []
        )
        if (
            group is None
            or requested_revision is None
            or group.revision != requested_revision
            or set(current_member_ids) != set(requested_member_ids)
            or len(current_member_ids) != len(requested_member_ids)
        ):
            self._raise_error(
                409,
                "DUPLICATE_GROUP_REVISION_CONFLICT",
                "批内重复成员已经变化，请刷新后重新选择。",
                details={
                    "current_revision": group.revision if group is not None else None,
                    "current_member_item_ids": current_member_ids,
                },
            )

    def _validate_wait_decision(
        self,
        *,
        item: IngestItem,
        review: UploadDuplicateReview,
        candidate: UploadDuplicateCandidate | None,
    ) -> None:
        """确认等待目标、组修订和 review 期限仍与展示时一致。"""

        if candidate is None or not candidate.candidate_ingest_item_id:
            self._raise_error(409, "WAIT_AND_REUSE_NOT_AVAILABLE", "缺少可等待的同批主条目。")
        expires_at = review.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < utcnow():
            review.status = "EXPIRED"
            review.revision += 1
            self.db.commit()
            self._raise_error(409, "DUPLICATE_REVIEW_EXPIRED", "重复确认已过期。")
        member = (
            self.db.query(IngestDuplicateGroupMember)
            .filter(
                IngestDuplicateGroupMember.ingest_item_id == item.id,
                IngestDuplicateGroupMember.waits_for_item_id == candidate.candidate_ingest_item_id,
            )
            .with_for_update()
            .one_or_none()
        )
        if member is None:
            self._raise_error(409, "DUPLICATE_GROUP_CHANGED", "批内重复组已经变化，请刷新后重试。")
        member.decision = "WAIT_AND_REUSE"

    def _validate_working_copy_snapshot(self, candidate: UploadDuplicateCandidate) -> None:
        """确认展示后的已有工作副本未被改名、移动、换版或替换内容。"""

        working_copy = (
            self.db.get(WorkingCopy, candidate.candidate_working_copy_id)
            if candidate.candidate_working_copy_id
            else None
        )
        if (
            working_copy is None
            or working_copy.status != "ACTIVE"
            or (
                candidate.compared_working_copy_revision is not None
                and working_copy.revision != candidate.compared_working_copy_revision
            )
            or (
                candidate.compared_version_id is not None
                and working_copy.current_version_id != candidate.compared_version_id
            )
            or (
                candidate.compared_sha256 is not None
                and working_copy.content_sha256 != candidate.compared_sha256
            )
        ):
            self._raise_error(
                409,
                "DUPLICATE_CANDIDATE_CHANGED",
                "所选已有文件在确认前发生变化，请刷新候选后重新选择。",
            )

    @staticmethod
    def _decision_fingerprint(*, item_id: str, request: IngestDuplicateDecisionRequest) -> str:
        """计算包含修订、候选和决定的稳定摘要。"""

        payload = {"item_id": item_id, **request.model_dump(mode="json", exclude={"request_id", "idempotency_key"})}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _raise_error(
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict | None = None,
    ) -> None:
        """抛出统一结构化业务错误。"""

        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": message, "details": details},
        )
