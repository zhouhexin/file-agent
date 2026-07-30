"""当前文件版本的分类建议与可定位证据读取服务。

本服务统一解决活动工作副本、当前 DocumentVersion 和最新成功分类运行。调用方不得直接按 document_id
读取全部历史建议，否则旧版本分类可能污染用户当前看到的文件卡和解释回答。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentVersion,
    WorkingCopy,
)


class CurrentClassificationEvidenceReader:
    """读取当前版本最新成功分类建议及原文 evidence_items。"""

    def __init__(self, *, db: Session, user_id: str | None) -> None:
        """保存请求级数据库会话；Tool 调用时必须传 user_id 校验所有权。"""

        self.db = db
        self.user_id = user_id

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

        working_copy = (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.document_id == document.id,
                WorkingCopy.status == "ACTIVE",
            )
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
        if run is None:
            return self._empty_result(
                document=document,
                working_copy=working_copy,
                document_version_id=document_version_id,
                error_code="NO_CURRENT_CLASSIFICATION_EVIDENCE",
            )
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
        return {
            "document_id": document.id,
            "document_version_id": document_version_id,
            "working_copy_id": working_copy.id if working_copy is not None else None,
            "filename": (
                working_copy.filename
                if working_copy is not None
                else document.original_filename
            ),
            "classification_run_id": run.id,
            "taxonomy_key": run.taxonomy_key,
            "taxonomy_version": run.taxonomy_version,
            "classifier_version": run.classifier_version,
            "classification_basis": run.classification_basis,
            "summary_status": run.summary_status,
            "status": "COMPLETED",
            "categories": [
                {
                    "category_id": suggestion.category_id,
                    "name": suggestion.category_name,
                    "category_path": list(suggestion.category_path_json or []),
                    "confidence": float(suggestion.confidence or 0),
                    "status": suggestion.status,
                    "source": suggestion.source,
                    "evidence_items": list(suggestion.evidence_json or []),
                }
                for suggestion in suggestions
            ],
        }

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

