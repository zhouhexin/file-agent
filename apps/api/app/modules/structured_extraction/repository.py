"""结构化抽取运行、版面证据和字段结果的数据库仓库。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    ChangeItem,
    ChangeSet,
    DocumentElement,
    DocumentExtractionRun,
    DocumentPage,
    DocumentVersion,
    StructuredExtractionField,
    StructuredExtractionRun,
    utcnow,
)
from app.modules.agent.tool_schemas import StructuredImageExtractionInput
from app.modules.structured_extraction.evidence import EvidenceElement
from app.modules.structured_extraction.schemas import (
    CandidateExtraction,
    LayoutParseResult,
    NormalizedField,
)


class StructuredExtractionRepository:
    """封装图片结构化抽取的持久化和审计写入。"""

    def __init__(self, db: Session) -> None:
        """保存请求或 worker 级数据库会话。"""

        self.db = db

    def latest_document_version(
        self,
        document_id: str,
        *,
        sha256: str | None = None,
    ) -> DocumentVersion | None:
        """读取与本次实际文件对象一致的最新不可变内容版本。"""

        query = self.db.query(DocumentVersion).filter(
            DocumentVersion.document_id == document_id
        )
        if sha256:
            query = query.filter(DocumentVersion.sha256 == sha256)
        return query.order_by(
            DocumentVersion.version_number.desc(),
            DocumentVersion.created_at.desc(),
        ).first()

    def find_reusable_run(
        self,
        *,
        document_version_id: str,
        schema_fingerprint: str,
        provider: str,
        model_name: str,
        prompt_version: str,
        retry_strategy: str,
    ) -> StructuredExtractionRun | None:
        """按不可变版本和完整配置指纹复用成功结果。"""

        return (
            self.db.query(StructuredExtractionRun)
            .filter(
                StructuredExtractionRun.document_version_id == document_version_id,
                StructuredExtractionRun.schema_fingerprint == schema_fingerprint,
                StructuredExtractionRun.provider == provider,
                StructuredExtractionRun.model_name == model_name,
                StructuredExtractionRun.prompt_version == prompt_version,
                StructuredExtractionRun.retry_strategy == retry_strategy,
                StructuredExtractionRun.status.in_({"COMPLETED", "PARTIAL", "NEEDS_REVIEW"}),
            )
            .order_by(StructuredExtractionRun.updated_at.desc())
            .first()
        )

    def create_run(
        self,
        *,
        tool_input: StructuredImageExtractionInput,
        document_version_id: str,
        schema_fingerprint: str,
        provider: str,
        model_name: str,
        prompt_version: str,
        agent_run_id: str | None,
        parent_run_id: str | None = None,
    ) -> StructuredExtractionRun:
        """创建 PENDING 运行；SDK 和模型对象绝不写入该记录。"""

        run = StructuredExtractionRun(
            document_id=tool_input.document_id,
            document_version_id=document_version_id,
            agent_run_id=agent_run_id,
            schema_mode=tool_input.schema_mode,
            field_schema_json=[item.model_dump() for item in tool_input.fields],
            schema_fingerprint=schema_fingerprint,
            record_mode=tool_input.record_mode,
            presentation=tool_input.presentation,
            provider=provider,
            model_name=model_name,
            prompt_version=prompt_version,
            retry_strategy=tool_input.retry_strategy,
            target_field_keys_json=list(tool_input.target_field_keys),
            parent_run_id=parent_run_id,
            status="PENDING",
        )
        self.db.add(run)
        self.db.flush()
        return run

    def create_layout_extraction(
        self,
        *,
        run: StructuredExtractionRun,
        layout: LayoutParseResult,
        parser_config_hash: str,
    ) -> tuple[DocumentExtractionRun, list[EvidenceElement]]:
        """把 Provider 版面结果写入既有 document_pages/document_elements。"""

        extraction = DocumentExtractionRun(
            document_id=run.document_id,
            document_version_id=run.document_version_id,
            status="RUNNING",
            extractor=layout.provider,
            parser_name=layout.provider,
            parser_version=layout.provider_version,
            parser_config_hash=parser_config_hash,
        )
        self.db.add(extraction)
        self.db.flush()
        persisted: list[tuple[DocumentElement, dict[str, Any]]] = []
        for page in layout.pages:
            page_text = "\n".join(
                element.text for element in page.elements if element.text.strip()
            )
            self.db.add(
                DocumentPage(
                    document_id=run.document_id,
                    extraction_run_id=extraction.id,
                    page_number=page.page_number,
                    text_content=page_text,
                    metadata_json={
                        "provider": layout.provider,
                        "provider_version": layout.provider_version,
                        "width": page.width,
                        "height": page.height,
                        "rotation": page.rotation,
                        "read_quality": "GOOD" if page_text else "PARTIAL",
                    },
                )
            )
            for element in page.elements:
                metadata = {
                    "element_type": element.element_type,
                    "confidence": element.confidence,
                    "reading_order": element.reading_order,
                    "table_id": element.table_id,
                    "row_start": element.row_start,
                    "row_end": element.row_end,
                    "column_start": element.column_start,
                    "column_end": element.column_end,
                }
                row = DocumentElement(
                    document_id=run.document_id,
                    extraction_run_id=extraction.id,
                    element_index=element.element_index,
                    label=element.element_type,
                    text_content=element.text,
                    page_number=page.page_number,
                    bbox_json=element.bbox.model_dump() if element.bbox else {},
                    content_layer="body",
                    parent_ref=element.parent_ref,
                    metadata_json=metadata,
                )
                self.db.add(row)
                persisted.append((row, metadata))
        extraction.status = "COMPLETED"
        extraction.updated_at = utcnow()
        run.layout_extraction_run_id = extraction.id
        run.status = "RUNNING"
        run.updated_at = utcnow()
        self.db.flush()
        evidence = [
            EvidenceElement(
                id=str(row.id),
                document_id=str(row.document_id),
                extraction_run_id=str(row.extraction_run_id),
                text=str(row.text_content or ""),
                page_number=row.page_number,
                bbox=dict(row.bbox_json or {}),
                metadata=metadata,
            )
            for row, metadata in persisted
        ]
        return extraction, evidence

    def complete_run(
        self,
        *,
        run: StructuredExtractionRun,
        fields: list[tuple[int, NormalizedField]],
        field_schema: list[dict[str, Any]],
        record_count: int,
        review_count: int,
        missing_required_field_count: int,
        quality_score: float,
        quality_band: str,
    ) -> None:
        """一次性持久化字段结果并推进运行终态。"""

        run.field_schema_json = field_schema
        for record_index, field in fields:
            self.db.add(
                StructuredExtractionField(
                    structured_extraction_run_id=run.id,
                    record_index=record_index,
                    field_key=field.key,
                    field_label=field.label,
                    field_type=field.field_type,
                    raw_text=field.raw_text,
                    normalized_value_json=field.normalized_value,
                    confidence=field.confidence,
                    status=field.status,
                    page_number=field.page_number,
                    bbox_json=field.bbox,
                    evidence_element_ids_json=field.evidence_element_ids,
                    warning_codes_json=field.warning_codes,
                )
            )
        run.record_count = record_count
        run.review_count = review_count
        run.missing_required_field_count = missing_required_field_count
        run.quality_score = quality_score
        run.quality_band = quality_band
        run.status = (
            "COMPLETED"
            if quality_band == "HIGH"
            else "PARTIAL"
            if quality_band == "MEDIUM"
            else "NEEDS_REVIEW"
        )
        run.updated_at = utcnow()
        self.db.flush()

    def append_vision_candidate_evidence(
        self,
        *,
        run: StructuredExtractionRun,
        layout_extraction_run_id: str,
        candidates: CandidateExtraction,
    ) -> list[EvidenceElement]:
        """把局部图片识别值绑定到父运行 bbox，形成可审计派生证据元素。"""

        if not run.parent_run_id:
            return []
        parent_rows = (
            self.db.query(StructuredExtractionField)
            .filter(
                StructuredExtractionField.structured_extraction_run_id == run.parent_run_id,
                StructuredExtractionField.field_key.in_(run.target_field_keys_json or []),
            )
            .all()
        )
        by_record_key = {
            (row.record_index, row.field_key): row
            for row in parent_rows
            if row.page_number and row.bbox_json
        }
        maximum_index = (
            self.db.query(func.max(DocumentElement.element_index))
            .filter(DocumentElement.extraction_run_id == layout_extraction_run_id)
            .scalar()
        )
        next_index = int(maximum_index if maximum_index is not None else -1) + 1
        target_keys = set(run.target_field_keys_json or [])
        pending: list[tuple[DocumentElement, Any]] = []
        for record in candidates.records:
            for field_key, candidate in record.fields.items():
                if field_key not in target_keys or not candidate.raw_text:
                    continue
                parent = by_record_key.get((record.record_index, field_key))
                if parent is None:
                    continue
                row = DocumentElement(
                    document_id=run.document_id,
                    extraction_run_id=layout_extraction_run_id,
                    element_index=next_index,
                    label="vision_crop_text",
                    text_content=candidate.raw_text,
                    page_number=parent.page_number,
                    bbox_json=dict(parent.bbox_json or {}),
                    content_layer="body",
                    parent_ref=f"structured-extraction:{run.parent_run_id}",
                    metadata_json={
                        "element_type": "vision_crop_text",
                        "source": "vision_crop",
                        "field_key": field_key,
                        "confidence": candidate.confidence,
                    },
                )
                next_index += 1
                self.db.add(row)
                pending.append((row, candidate))
        self.db.flush()
        evidence: list[EvidenceElement] = []
        for row, candidate in pending:
            candidate.evidence_element_ids = [str(row.id)]
            evidence.append(
                EvidenceElement(
                    id=str(row.id),
                    document_id=run.document_id,
                    extraction_run_id=layout_extraction_run_id,
                    text=str(row.text_content or ""),
                    page_number=row.page_number,
                    bbox=dict(row.bbox_json or {}),
                    metadata=dict(row.metadata_json or {}),
                )
            )
        return evidence

    def fail_run(self, *, run: StructuredExtractionRun, code: str, message: str) -> None:
        """保存脱敏失败原因，不写 Provider 堆栈或文件路径。"""

        run.status = "FAILED"
        run.error_code = code[:120]
        run.error_message = message[:2000]
        run.updated_at = utcnow()
        self.db.flush()

    def load_fields(self, run_id: str) -> list[StructuredExtractionField]:
        """按记录与字段顺序读取持久化结果。"""

        return (
            self.db.query(StructuredExtractionField)
            .filter(StructuredExtractionField.structured_extraction_run_id == run_id)
            .order_by(
                StructuredExtractionField.record_index.asc(),
                StructuredExtractionField.created_at.asc(),
            )
            .all()
        )

    def record_changeset(
        self,
        *,
        run: StructuredExtractionRun,
        agent_run_id: str | None,
        conversation_id: str | None,
        user_id: str,
        review_count: int,
        field_count: int,
        record_count: int,
        export_artifact: dict[str, Any] | None = None,
        reused: bool = False,
    ) -> str | None:
        """记录版面和字段候选变更，不把字段原值写入 ChangeSet。"""

        agent_run = self.db.get(AgentRun, agent_run_id) if agent_run_id else None
        if (
            agent_run is None
            or not conversation_id
            or str(agent_run.conversation_id) != str(conversation_id)
            or str(agent_run.user_id) != str(user_id)
        ):
            return None
        changeset = ChangeSet(
            conversation_id=agent_run.conversation_id,
            agent_run_id=agent_run.id,
            user_id=agent_run.user_id,
            status="COMPLETED" if review_count == 0 else "NEEDS_REVIEW",
            summary=(
                f"已复用 1 个文件的 {record_count} 条结构化记录，{review_count} 个字段待复核。"
                if reused
                else f"已从 1 个文件提取 {record_count} 条结构化记录，{review_count} 个字段待复核。"
            ),
        )
        self.db.add(changeset)
        self.db.flush()
        items = (
            [
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="DOCUMENT",
                    target_id=run.document_id,
                    target_document_id=run.document_id,
                    change_type="STRUCTURED_EXTRACTION_REUSED",
                    after_value_json={
                        "structured_extraction_run_id": run.id,
                        "schema_fingerprint": run.schema_fingerprint,
                        "record_count": record_count,
                        "field_count": field_count,
                        "review_count": review_count,
                        "original_unchanged": True,
                    },
                    source="image-structured-extraction",
                    confidence=float(run.quality_score or 0),
                    execution_status="COMPLETED" if review_count == 0 else "NEEDS_REVIEW",
                )
            ]
            if reused
            else [
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="DOCUMENT",
                    target_id=run.document_id,
                    target_document_id=run.document_id,
                    change_type="IMAGE_LAYOUT_PARSED",
                    after_value_json={
                        "layout_extraction_run_id": run.layout_extraction_run_id,
                        "original_unchanged": True,
                    },
                    source=run.provider,
                    confidence=float(run.quality_score or 0),
                    execution_status="COMPLETED",
                ),
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="DOCUMENT",
                    target_id=run.document_id,
                    target_document_id=run.document_id,
                    change_type="STRUCTURED_FIELDS_EXTRACTED",
                    after_value_json={
                        "structured_extraction_run_id": run.id,
                        "schema_fingerprint": run.schema_fingerprint,
                        "record_count": record_count,
                        "field_count": field_count,
                        "review_count": review_count,
                        "original_unchanged": True,
                    },
                    source="image-structured-extraction",
                    confidence=float(run.quality_score or 0),
                    execution_status="COMPLETED" if review_count == 0 else "NEEDS_REVIEW",
                ),
            ]
        )
        if export_artifact and (
            not reused or export_artifact.get("reused") is False
        ):
            items.append(
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="ARTIFACT",
                    target_id=str(export_artifact["artifact_id"]),
                    target_document_id=run.document_id,
                    change_type="EXPORT_CREATED",
                    after_value_json={
                        "artifact_id": export_artifact["artifact_id"],
                        "format": export_artifact["format"],
                        "original_unchanged": True,
                    },
                    source="image-structured-extraction",
                    confidence=float(run.quality_score or 0),
                    execution_status="COMPLETED",
                )
            )
        self.db.add_all(items)
        self.db.flush()
        return str(changeset.id)

    def record_failure_changeset(
        self,
        *,
        run: StructuredExtractionRun,
        agent_run: AgentRun,
        error_code: str,
    ) -> str:
        """幂等记录异步结构化抽取失败，不保存异常正文或内部路径。"""

        existing = (
            self.db.query(ChangeSet)
            .join(ChangeItem, ChangeItem.changeset_id == ChangeSet.id)
            .filter(
                ChangeSet.agent_run_id == agent_run.id,
                ChangeItem.target_document_id == run.document_id,
                ChangeItem.change_type == "STRUCTURED_EXTRACTION_FAILED",
            )
            .order_by(ChangeSet.created_at.desc())
            .first()
        )
        if existing is not None:
            return str(existing.id)
        changeset = ChangeSet(
            conversation_id=agent_run.conversation_id,
            agent_run_id=agent_run.id,
            user_id=agent_run.user_id,
            status="FAILED",
            summary="图片结构化抽取失败，原始文件未修改。",
        )
        self.db.add(changeset)
        self.db.flush()
        self.db.add(
            ChangeItem(
                changeset_id=changeset.id,
                target_type="DOCUMENT",
                target_id=run.document_id,
                target_document_id=run.document_id,
                change_type="STRUCTURED_EXTRACTION_FAILED",
                after_value_json={
                    "structured_extraction_run_id": run.id,
                    "error_code": error_code[:120],
                    "original_unchanged": True,
                },
                source="image-structured-extraction",
                confidence=0,
                execution_status="FAILED",
            )
        )
        self.db.flush()
        return str(changeset.id)
