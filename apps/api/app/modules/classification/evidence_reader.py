"""当前文件版本的分类建议与可定位证据读取服务。

本服务统一解决活动工作副本、当前 DocumentVersion 和最新成功分类运行。调用方不得直接按 document_id
读取全部历史建议，否则旧版本分类可能污染用户当前看到的文件卡和解释回答。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentCategory,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentVersion,
    WorkingCopy,
)


class CurrentClassificationEvidenceReader:
    """读取当前版本最新成功分类建议及原文 evidence_items。"""

    def __init__(
        self,
        *,
        db: Session,
        user_id: str | None,
        workspace_id: str | None = None,
    ) -> None:
        """保存请求级权限范围；调用方必须提供用户或已验证的共享工作区。"""

        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id

    def read(self, *, document_ids: list[str]) -> list[dict[str, Any]]:
        """按输入顺序返回当前版本分类，找不到时返回明确空结果。"""

        normalized_ids = list(dict.fromkeys(str(item) for item in document_ids if str(item)))
        query = self.db.query(Document).filter(Document.id.in_(normalized_ids))
        if self.user_id is not None:
            query = query.filter(Document.user_id == self.user_id)
        documents = {document.id: document for document in query.all()}
        return [
            self._read_document(document)
            for document_id in normalized_ids
            if (document := documents.get(document_id)) is not None
        ]

    def _read_document(self, document: Document) -> dict[str, Any]:
        """解析活动工作副本当前版本，并选择该版本最新成功分类运行。"""

        working_copy_query = self.db.query(WorkingCopy).filter(
            WorkingCopy.document_id == document.id,
            WorkingCopy.status == "ACTIVE",
        )
        if self.workspace_id is not None:
            # 共享文件读取已经由调用方按 workspace 授权；这里必须继续固定同一范围，
            # 不能因历史遗留用户工作区中存在另一条 ACTIVE 副本而选错版本。
            working_copy_query = working_copy_query.filter(
                WorkingCopy.workspace_id == self.workspace_id
            )
        working_copy = (
            working_copy_query
            .order_by(WorkingCopy.updated_at.desc(), WorkingCopy.id.desc())
            .first()
        )
        document_version_id = (
            str(working_copy.current_version_id)
            if working_copy is not None and working_copy.current_version_id
            else self._latest_document_version_id(document.id)
        )
        if not document_version_id:
            return self._empty_result(
                document=document,
                working_copy=working_copy,
                error_code="NO_CURRENT_DOCUMENT_VERSION",
            )
        run = (
            self.db.query(DocumentClassificationRun)
            .join(
                DocumentCategorySuggestion,
                DocumentCategorySuggestion.classification_run_id
                == DocumentClassificationRun.id,
            )
            .filter(
                DocumentClassificationRun.document_id == document.id,
                DocumentClassificationRun.status == "COMPLETED",
                DocumentCategorySuggestion.document_version_id
                == document_version_id,
            )
            .order_by(
                DocumentClassificationRun.created_at.desc(),
                DocumentClassificationRun.id.desc(),
            )
            .first()
        )
        relations = self._active_relations(
            working_copy=working_copy,
            document_version_id=document_version_id,
        )
        if run is None and not relations:
            return self._empty_result(
                document=document,
                working_copy=working_copy,
                document_version_id=document_version_id,
                error_code="NO_CURRENT_CLASSIFICATION_EVIDENCE",
            )
        suggestions = []
        if run is not None:
            suggestions = (
                self.db.query(DocumentCategorySuggestion)
                .filter(
                    DocumentCategorySuggestion.classification_run_id == run.id,
                    DocumentCategorySuggestion.document_version_id
                    == document_version_id,
                )
                .order_by(
                    DocumentCategorySuggestion.rank.asc(),
                    DocumentCategorySuggestion.confidence.desc(),
                )
                .all()
            )
        categories = self._merge_categories(
            suggestions=suggestions,
            relations=relations,
        )
        decision = dict(run.decision_json or {}) if run is not None else {}
        reference_relation = relations[0] if relations else None
        return {
            "document_id": document.id,
            "document_version_id": document_version_id,
            "working_copy_id": working_copy.id if working_copy is not None else None,
            "filename": (
                working_copy.filename
                if working_copy is not None
                else document.original_filename
            ),
            "classification_run_id": run.id if run is not None else None,
            "taxonomy_key": (
                run.taxonomy_key
                if run is not None
                else reference_relation.taxonomy_key
                if reference_relation is not None
                else None
            ),
            "taxonomy_version": (
                run.taxonomy_version
                if run is not None
                else reference_relation.taxonomy_version
                if reference_relation is not None
                else None
            ),
            "classifier_version": (
                run.classifier_version
                if run is not None
                else reference_relation.classifier_version
                if reference_relation is not None
                else None
            ),
            "classification_basis": run.classification_basis if run is not None else None,
            "summary_status": run.summary_status if run is not None else None,
            "classification_outcome": str(decision.get("classification_outcome") or ""),
            "classification_quality": str(decision.get("classification_quality") or ""),
            "selection_basis": str(decision.get("selection_basis") or ""),
            "reason_codes": list(decision.get("reason_codes") or []),
            "status": "COMPLETED",
            "categories": categories,
        }

    def _active_relations(
        self,
        *,
        working_copy: WorkingCopy | None,
        document_version_id: str,
    ) -> list[DocumentCategory]:
        """读取当前工作副本版本的正式分类关系，不把历史或已撤销关系混入结果。"""

        if working_copy is None:
            return []
        return (
            self.db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id == working_copy.id,
                DocumentCategory.document_version_id == document_version_id,
                DocumentCategory.status.in_({"AUTO_APPLIED", "CONFIRMED"}),
            )
            .order_by(
                DocumentCategory.relation_role.asc(),
                DocumentCategory.created_at.asc(),
            )
            .all()
        )

    @staticmethod
    def _merge_categories(
        *,
        suggestions: list[DocumentCategorySuggestion],
        relations: list[DocumentCategory],
    ) -> list[dict[str, Any]]:
        """合并建议角色和正式角色，使调用方能解释候选与最终落位的差异。"""

        relation_by_category: dict[str, DocumentCategory] = {}
        for relation in relations:
            current = relation_by_category.get(relation.category_id)
            if current is None or relation.relation_role == "PRIMARY":
                relation_by_category[relation.category_id] = relation

        rows: list[dict[str, Any]] = []
        suggested_category_ids: set[str] = set()
        for suggestion in suggestions:
            suggested_category_ids.add(suggestion.category_id)
            scores = dict(suggestion.candidate_scores_json or {})
            relation = relation_by_category.get(suggestion.category_id)
            rows.append(
                {
                    "suggestion_id": suggestion.id,
                    "category_id": suggestion.category_id,
                    "name": suggestion.category_name,
                    "category_path": list(suggestion.category_path_json or []),
                    "rank": suggestion.rank,
                    "confidence": float(suggestion.confidence or 0),
                    # 保留旧 Tool 契约中的 status，同时提供语义更明确的 suggestion_status。
                    "status": suggestion.status,
                    "suggestion_status": suggestion.status,
                    "suggested_role": str(
                        scores.get("relation_role")
                        or ("PRIMARY" if suggestion.rank == 1 else "SECONDARY")
                    ),
                    "effective_role": relation.relation_role if relation is not None else None,
                    "effective_status": relation.status if relation is not None else None,
                    "source": suggestion.source,
                    "evidence_items": CurrentClassificationEvidenceReader._normalize_evidence_items(
                        suggestion.evidence_json
                    ),
                }
            )

        for relation in relations:
            if relation.category_id in suggested_category_ids:
                continue
            category_path = list(relation.category_path_json or [])
            rows.append(
                {
                    "suggestion_id": None,
                    "category_id": relation.category_id,
                    "name": category_path[-1] if category_path else relation.category_id,
                    "category_path": category_path,
                    "rank": None,
                    "confidence": None,
                    "status": relation.status,
                    "suggestion_status": None,
                    "suggested_role": None,
                    "effective_role": relation.relation_role,
                    "effective_status": relation.status,
                    "source": relation.source,
                    "evidence_items": CurrentClassificationEvidenceReader._normalize_evidence_items(
                        relation.evidence_json
                    ),
                }
            )
        return CurrentClassificationEvidenceReader._hide_inactive_system_other(
            rows=rows
        )

    @staticmethod
    def _hide_inactive_system_other(*, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """已有有效业务主类时隐藏历史遗留的零分 system.other 建议。

        历史运行可能在人工或自动业务主类落位后保留早期兜底建议。该行既未生效
        也没有业务证据，继续展示会误导用户认为文件最终归入“其他”。正式 PRIMARY
        为 system.other 或没有业务 PRIMARY 时，仍完整保留该兜底行。
        """

        has_business_primary = any(
            row.get("effective_role") == "PRIMARY"
            and str(row.get("category_id") or "") != "system.other"
            for row in rows
        )
        if not has_business_primary:
            return rows
        return [
            row
            for row in rows
            if not (
                str(row.get("category_id") or "") == "system.other"
                and row.get("effective_role") is None
                and float(row.get("confidence") or 0) == 0.0
            )
        ]

    @staticmethod
    def _normalize_evidence_items(raw: Any) -> list[dict[str, Any]]:
        """只返回可审计的结构化证据，兼容早期保存的字符串信号数组。

        历史分类建议曾把命中信号直接保存为字符串列表。字符串没有页码、Sheet 或原文 quote，
        不能冒充可定位原文依据；读取时忽略这些旧信号，使分类事实仍可返回，并由调用方明确展示
        “暂无可定位原文依据”。数据库原值保持不变。
        """

        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, dict)]

    def _latest_document_version_id(self, document_id: str) -> str | None:
        """没有活动工作副本时读取上传文档的最新内容版本。"""

        version = (
            self.db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == document_id)
            .order_by(
                DocumentVersion.version_number.desc(),
                DocumentVersion.created_at.desc(),
            )
            .first()
        )
        return str(version.id) if version is not None else None

    @staticmethod
    def _empty_result(
        *,
        document: Document,
        working_copy: WorkingCopy | None,
        error_code: str,
        document_version_id: str | None = None,
    ) -> dict[str, Any]:
        """返回不伪造历史建议的当前版本空结果。"""

        return {
            "document_id": document.id,
            "document_version_id": document_version_id,
            "working_copy_id": working_copy.id if working_copy is not None else None,
            "filename": (
                working_copy.filename
                if working_copy is not None
                else document.original_filename
            ),
            "classification_run_id": None,
            "status": "NO_EVIDENCE",
            "error_code": error_code,
            "categories": [],
        }
