"""首次分类与重新分类的组织决策、自动主分类关系和幂等查询仓库。"""

from __future__ import annotations

import hashlib

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    ClassificationGraphOutbox,
    DocumentCategory,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentOrganizationDecision,
    WorkingCopy,
    utcnow,
)
from app.modules.classification.auto_placement_policy import AutoPlacementPolicyResult


class OrganizationDecisionRepository:
    """封装自动组织事实写入，调用方仍负责文件系统与事务边界。"""

    def __init__(self, db: Session) -> None:
        """保存当前 Worker 事务的数据库会话。"""

        self.db = db

    def latest_classification(
        self,
        *,
        agent_run_id: str,
        document_id: str,
    ) -> tuple[DocumentClassificationRun | None, DocumentCategorySuggestion | None]:
        """读取本次审计运行的首条候选，不能跨 AgentRun 猜测来源。"""

        run = (
            self.db.query(DocumentClassificationRun)
            .filter(
                DocumentClassificationRun.agent_run_id == agent_run_id,
                DocumentClassificationRun.document_id == document_id,
            )
            .order_by(DocumentClassificationRun.created_at.desc())
            .first()
        )
        if run is None:
            return None, None
        suggestion = (
            self.db.query(DocumentCategorySuggestion)
            .filter(DocumentCategorySuggestion.classification_run_id == run.id)
            .order_by(
                DocumentCategorySuggestion.rank.asc(),
                DocumentCategorySuggestion.confidence.desc(),
            )
            .first()
        )
        return run, suggestion

    def create_or_update_decision(
        self,
        *,
        working_copy: WorkingCopy,
        classification_run: DocumentClassificationRun | None,
        primary_suggestion: DocumentCategorySuggestion | None,
        policy_result: AutoPlacementPolicyResult,
        policy_version: str,
        calibration_version: str,
        decision: str,
        reason_codes: list[str],
        target_relative_path: str | None = None,
        shadow_only: bool = False,
        decision_scope: str = "initial-organization",
    ) -> DocumentOrganizationDecision:
        """按文件版本和策略版本幂等写入组织决策快照。"""

        raw_idempotency_key = (
            f"{decision_scope}:{working_copy.id}:{working_copy.current_version_id}:"
            f"{policy_version}:{calibration_version}"
        )
        idempotency_key = (
            raw_idempotency_key
            if len(raw_idempotency_key) <= 160
            else (
                f"{decision_scope[:40]}:"
                f"{hashlib.sha256(raw_idempotency_key.encode('utf-8')).hexdigest()}"
            )
        )
        row = (
            self.db.query(DocumentOrganizationDecision)
            .filter(DocumentOrganizationDecision.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if row is None:
            row = DocumentOrganizationDecision(
                working_copy_id=working_copy.id,
                document_id=working_copy.document_id,
                document_version_id=str(working_copy.current_version_id or ""),
                idempotency_key=idempotency_key,
                policy_version=policy_version,
            )
            self.db.add(row)
        row.classification_run_id = classification_run.id if classification_run else None
        row.primary_suggestion_id = primary_suggestion.id if primary_suggestion else None
        row.category_id = (
            primary_suggestion.category_id if primary_suggestion and primary_suggestion.category_id else None
        )
        row.taxonomy_key = classification_run.taxonomy_key if classification_run else ""
        row.taxonomy_version = classification_run.taxonomy_version if classification_run else ""
        row.classifier_version = classification_run.classifier_version if classification_run else ""
        row.calibration_version = calibration_version
        row.decision = decision
        row.calibrated_confidence = policy_result.calibrated_confidence
        row.required_threshold = policy_result.required_threshold
        row.top_margin = policy_result.top_margin
        row.required_margin = policy_result.required_margin
        row.feature_snapshot_json = {
            **dict(policy_result.feature_snapshot),
            "evaluated_decision": policy_result.evaluated_decision,
            "shadow_only": shadow_only,
        }
        row.reason_codes_json = list(dict.fromkeys(reason_codes))
        row.target_relative_path_snapshot = target_relative_path
        row.completed_at = utcnow()
        self.db.flush()
        return row

    def create_auto_applied_primary(
        self,
        *,
        working_copy: WorkingCopy,
        classification_run: DocumentClassificationRun,
        suggestion: DocumentCategorySuggestion,
    ) -> DocumentCategory:
        """为首次落位创建唯一 ``AUTO_APPLIED`` 主分类，不伪造用户确认来源。"""

        existing = (
            self.db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id == working_copy.id,
                DocumentCategory.document_version_id == working_copy.current_version_id,
                DocumentCategory.relation_role == "PRIMARY",
                DocumentCategory.status.in_(["AUTO_APPLIED", "CONFIRMED"]),
            )
            .one_or_none()
        )
        if existing is not None:
            return existing
        relation = DocumentCategory(
            working_copy_id=working_copy.id,
            document_id=working_copy.document_id,
            document_version_id=str(working_copy.current_version_id or ""),
            category_id=suggestion.category_id,
            category_path_json=list(suggestion.category_path_json or []),
            relation_role="PRIMARY",
            status="AUTO_APPLIED",
            taxonomy_key=classification_run.taxonomy_key,
            taxonomy_version=classification_run.taxonomy_version,
            classifier_version=classification_run.classifier_version,
            source="auto_placement_policy",
            source_suggestion_id=suggestion.id,
            evidence_json=list(suggestion.evidence_json or []),
        )
        self.db.add(relation)
        self.db.flush()
        self.db.add(
            ClassificationGraphOutbox(
                document_category_id=relation.id,
                working_copy_id=relation.working_copy_id,
                document_version_id=relation.document_version_id,
                expected_status="AUTO_APPLIED",
                state_version=1,
                deduplication_key=f"{relation.id}:1:AUTO_APPLIED:auto-placement",
                status="PENDING",
            )
        )
        return relation

    def replace_auto_applied_primary(
        self,
        *,
        working_copy: WorkingCopy,
        classification_run: DocumentClassificationRun,
        suggestion: DocumentCategorySuggestion,
    ) -> tuple[DocumentCategory, list[DocumentCategory]]:
        """以新自动结果替换旧自动主分类，人工确认关系绝不自动覆盖。"""

        active = (
            self.db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id == working_copy.id,
                DocumentCategory.document_version_id == working_copy.current_version_id,
                DocumentCategory.relation_role == "PRIMARY",
                DocumentCategory.status.in_(["AUTO_APPLIED", "CONFIRMED"]),
            )
            .with_for_update()
            .all()
        )
        same = next(
            (item for item in active if item.category_id == suggestion.category_id),
            None,
        )
        if same is not None:
            return same, []
        if any(item.status == "CONFIRMED" for item in active):
            raise ValueError("人工确认的主分类不能被自动重新分类覆盖。")

        ended: list[DocumentCategory] = []
        now = utcnow()
        for relation in active:
            relation.status = "ENDED"
            relation.ended_at = now
            relation.updated_at = now
            ended.append(relation)
            self._enqueue_graph_projection(
                relation=relation,
                source_key=f"auto-reclassification-ended:{suggestion.id}",
            )

        relation = DocumentCategory(
            working_copy_id=working_copy.id,
            document_id=working_copy.document_id,
            document_version_id=str(working_copy.current_version_id or ""),
            category_id=suggestion.category_id,
            category_path_json=list(suggestion.category_path_json or []),
            relation_role="PRIMARY",
            status="AUTO_APPLIED",
            taxonomy_key=classification_run.taxonomy_key,
            taxonomy_version=classification_run.taxonomy_version,
            classifier_version=classification_run.classifier_version,
            source="auto_reclassification_policy",
            source_suggestion_id=suggestion.id,
            evidence_json=list(suggestion.evidence_json or []),
        )
        self.db.add(relation)
        self.db.flush()
        self._enqueue_graph_projection(
            relation=relation,
            source_key=f"auto-reclassification-applied:{suggestion.id}",
        )
        return relation, ended

    def _enqueue_graph_projection(
        self,
        *,
        relation: DocumentCategory,
        source_key: str,
    ) -> None:
        """为自动关系状态变化写入幂等图谱投影待办。"""

        latest_version = (
            self.db.query(func.max(ClassificationGraphOutbox.state_version))
            .filter(
                ClassificationGraphOutbox.document_category_id == relation.id
            )
            .scalar()
            or 0
        )
        state_version = int(latest_version) + 1
        deduplication_key = (
            f"{relation.id}:{state_version}:{relation.status}:{source_key}"
        )
        if (
            self.db.query(ClassificationGraphOutbox)
            .filter(
                ClassificationGraphOutbox.deduplication_key == deduplication_key
            )
            .first()
            is not None
        ):
            return
        self.db.add(
            ClassificationGraphOutbox(
                document_category_id=relation.id,
                working_copy_id=relation.working_copy_id,
                document_version_id=relation.document_version_id,
                expected_status=relation.status,
                state_version=state_version,
                deduplication_key=deduplication_key,
                status="PENDING",
            )
        )
