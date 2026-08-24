"""图片结构化抽取的权限、异步提交、执行、归一化和质量编排服务。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any

from PIL import Image

from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.db.models import (
    DocumentPage,
    DocumentVersion,
    StructuredExtractionField,
    StructuredExtractionRun,
)
from app.modules.agent.tool_schemas import StructuredFieldSpec, StructuredImageExtractionInput
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.structured_extraction.evidence import (
    EvidenceElement,
    merge_evidence_bbox,
    validate_field_evidence,
)
from app.modules.structured_extraction.export import StructuredExtractionExportService
from app.modules.structured_extraction.llm_provider import (
    StructuredExtractionProviderProtocol,
    build_structured_extraction_provider,
)
from app.modules.structured_extraction.normalization import (
    mask_sensitive_value,
    normalize_field_value,
)
from app.modules.structured_extraction.pp_structure_provider import (
    LayoutParsingProviderProtocol,
    PpStructureV3Provider,
)
from app.modules.structured_extraction.repository import StructuredExtractionRepository
from app.modules.structured_extraction.schemas import (
    CandidateExtraction,
    CandidateRecord,
    NormalizedField,
    StructuredExtractionResult,
)


SUPPORTED_IMAGE_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/bmp",
    "image/tiff",
}


class StructuredExtractionService:
    """受控执行 PP-StructureV3 与动态字段映射的应用服务。"""

    def __init__(
        self,
        *,
        db: Any,
        user_id: str,
        conversation_id: str | None = None,
        agent_run_id: str | None = None,
        settings: Settings | None = None,
        layout_provider: LayoutParsingProviderProtocol | None = None,
        extraction_provider: StructuredExtractionProviderProtocol | None = None,
    ) -> None:
        """注入请求或 worker 级依赖，服务对象不进入 AgentGraphState。"""

        self.db = db
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.agent_run_id = agent_run_id
        self.settings = settings or get_settings()
        self.layout_provider = layout_provider or PpStructureV3Provider(settings=self.settings)
        self.extraction_provider = extraction_provider or build_structured_extraction_provider(
            settings=self.settings
        )
        self.repository = StructuredExtractionRepository(db)
        self.file_repository = FileExtractionRepository(db, user_id)

    def enqueue(self, tool_input: StructuredImageExtractionInput) -> dict[str, Any]:
        """校验授权文件，创建或复用运行，并提交独立推理队列。"""

        if not self.settings.structured_extraction_enabled or not self.settings.pp_structure_enabled:
            return _failure_output(
                document_id=tool_input.document_id,
                code="STRUCTURED_EXTRACTION_DISABLED",
                message="图片结构化抽取尚未由部署启用。",
            )
        if len(tool_input.fields) > self.settings.structured_extraction_max_fields:
            return _failure_output(
                document_id=tool_input.document_id,
                code="STRUCTURED_EXTRACTION_FIELD_LIMIT_EXCEEDED",
                message="请求字段数量超过当前结构化抽取上限，请拆分后重试。",
            )
        resolved = self.file_repository.resolve_original_file(tool_input.document_id)
        if not resolved.get("ok"):
            error = dict(resolved.get("error") or {})
            return _failure_output(
                document_id=tool_input.document_id,
                code=str(error.get("code") or "FILE_RESOLUTION_FAILED"),
                message=str(error.get("message") or "无法读取已授权文件。"),
            )
        document = resolved["document"]
        if document.content_type not in SUPPORTED_IMAGE_CONTENT_TYPES | {"application/pdf"}:
            return _failure_output(
                document_id=document.id,
                code="UNSUPPORTED_STRUCTURED_IMAGE_TYPE",
                message="仅支持图片和 PDF 扫描件的结构化抽取。",
            )
        try:
            self._validate_source_limits(
                file_path=resolved["file_path"],
                content_type=document.content_type,
            )
        except ValueError as exc:
            return _failure_output(
                document_id=document.id,
                code="STRUCTURED_EXTRACTION_SOURCE_LIMIT_EXCEEDED",
                message=str(exc),
            )
        file_object = resolved["file_object"]
        version = self.repository.latest_document_version(
            document.id,
            sha256=str(file_object.sha256 or ""),
        )
        if version is None:
            return _failure_output(
                document_id=document.id,
                code="DOCUMENT_VERSION_REQUIRED",
                message="文件尚未形成可追溯内容版本，请稍后重试。",
            )
        fingerprint = structured_extraction_fingerprint(tool_input)
        reusable = self.repository.find_reusable_run(
            document_version_id=version.id,
            schema_fingerprint=fingerprint,
            provider=self.layout_provider.name,
            model_name=self.extraction_provider.model_name,
            prompt_version=self.settings.structured_extraction_prompt_version,
            retry_strategy=tool_input.retry_strategy,
        )
        if reusable is not None:
            result = self.result_for_run(reusable)
            output = self._tool_output(run=reusable, result=result, reused=True)
            changeset_id = self.repository.record_changeset(
                run=reusable,
                agent_run_id=self.agent_run_id,
                conversation_id=self.conversation_id,
                user_id=self.user_id,
                review_count=result.review_count,
                field_count=result.field_count,
                record_count=result.record_count,
                export_artifact=output.get("export_artifact"),
                reused=True,
            )
            output["changeset_id"] = changeset_id
            return output
        run = self.repository.create_run(
            tool_input=tool_input,
            document_version_id=version.id,
            schema_fingerprint=fingerprint,
            provider=self.layout_provider.name,
            model_name=self.extraction_provider.model_name,
            prompt_version=self.settings.structured_extraction_prompt_version,
            agent_run_id=self.agent_run_id,
        )
        job = FilesystemJobQueue(self.db).create_job(
            job_type="STRUCTURED_IMAGE_EXTRACTION",
            queue_name="STRUCTURED_EXTRACTION",
            root_id=None,
            created_by=self.user_id,
            payload={
                "structured_extraction_run_id": run.id,
                "user_id": self.user_id,
                "conversation_id": self.conversation_id,
                "agent_run_id": self.agent_run_id,
            },
            deduplication_key=f"structured-extraction:{run.id}",
            max_attempts=1,
        )
        log_event(
            "structured_extraction.queued",
            settings=self.settings,
            agent_run_id=self.agent_run_id,
            document_id=document.id,
            status="WAITING_FOR_ASYNC_JOB",
            job_id=job.id,
        )
        return {
            "kind": "filesystem_job",
            "ok": True,
            "status": "WAITING_FOR_ASYNC_JOB",
            "document_id": document.id,
            "structured_extraction_run_id": run.id,
            "schema_mode": run.schema_mode,
            "record_mode": run.record_mode,
            "presentation": run.presentation,
            "async_job_id": job.id,
            "record_count": 0,
            "field_count": len(run.field_schema_json or []),
            "review_count": 0,
            "missing_required_field_count": 0,
            "retryable": False,
            "recommended_retry_strategy": "NONE",
            "low_confidence_field_keys": [],
            "field_schema": list(run.field_schema_json or []),
            "records": [],
            "review_items": [],
            "original_unchanged": True,
        }

    def execute_run(self, run: StructuredExtractionRun) -> dict[str, Any]:
        """在专用 worker 中执行版面解析、字段映射、证据校验和持久化。"""

        started = time.perf_counter()
        if run.status in {"COMPLETED", "PARTIAL", "NEEDS_REVIEW"}:
            return self._tool_output(run=run, result=self.result_for_run(run), reused=True)
        resolved = self.file_repository.resolve_original_file(run.document_id)
        if not resolved.get("ok"):
            error = dict(resolved.get("error") or {})
            raise RuntimeError(str(error.get("message") or "无法解析结构化抽取原件。"))
        document = resolved["document"]
        version = self.db.get(DocumentVersion, run.document_version_id)
        file_object = resolved["file_object"]
        source_path = Path(resolved["file_path"])
        if (
            version is None
            or str(file_object.sha256) != str(version.sha256)
            or _sha256_file(source_path) != str(version.sha256)
        ):
            raise RuntimeError("文件内容版本已变化，已停止结构化抽取。")
        parser_config_hash = _layout_config_fingerprint(
            provider=self.layout_provider,
            settings=self.settings,
        )
        layout = self.layout_provider.parse(file_path=source_path)
        if not layout.pages:
            raise RuntimeError("PP-StructureV3 未返回可用页面。")
        layout_run, elements = self.repository.create_layout_extraction(
            run=run,
            layout=layout,
            parser_config_hash=parser_config_hash,
        )
        fields = [StructuredFieldSpec.model_validate(item) for item in run.field_schema_json or []]
        extraction_arguments = {
            "fields": fields,
            "schema_mode": run.schema_mode,
            "record_mode": run.record_mode,
            "elements": elements,
            "max_records": self.settings.structured_extraction_max_records,
        }
        if run.retry_strategy == "VISION_CROP":
            if not self._vision_retry_available():
                raise RuntimeError("当前部署未授权结构化抽取发送局部图片。")
            image_url = self._targeted_crop_data_url(
                run=run,
                source_path=source_path,
                content_type=document.content_type,
            )
            candidates = self.extraction_provider.extract_with_image(
                **extraction_arguments,
                image_url=image_url,
            )
            elements.extend(
                self.repository.append_vision_candidate_evidence(
                    run=run,
                    layout_extraction_run_id=layout_run.id,
                    candidates=candidates,
                )
            )
        else:
            candidates = self.extraction_provider.extract(**extraction_arguments)
        if run.record_mode == "SINGLE_RECORD" and len(candidates.records) > 1:
            raise RuntimeError("结构化抽取模型返回了超出单记录模式的记录数量。")
        if run.schema_mode == "AUTO_DISCOVER":
            fields = [
                StructuredFieldSpec.model_validate(item)
                for item in candidates.discovered_fields
            ]
            if len(fields) > self.settings.structured_extraction_max_fields:
                raise RuntimeError("自动发现字段数量超过当前结构化抽取上限。")
        result, normalized_fields = self._normalize_candidates(
            run=run,
            layout_extraction_run_id=layout_run.id,
            fields=fields,
            candidates=candidates,
            elements=elements,
        )
        self.repository.complete_run(
            run=run,
            fields=normalized_fields,
            field_schema=result.field_schema,
            record_count=result.record_count,
            review_count=result.review_count,
            missing_required_field_count=result.missing_required_field_count,
            quality_score=result.quality_score,
            quality_band=result.quality_band,
        )
        export_artifact = StructuredExtractionExportService(
            db=self.db,
            settings=self.settings,
        ).ensure_export(run=run, result=result)
        changeset_id = self.repository.record_changeset(
            run=run,
            agent_run_id=self.agent_run_id,
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            review_count=result.review_count,
            field_count=result.field_count,
            record_count=result.record_count,
            export_artifact=export_artifact,
        )
        output = self._tool_output(
            run=run,
            result=result,
            changeset_id=changeset_id,
            reused=False,
            export_artifact=export_artifact,
        )
        log_event(
            "structured_extraction.completed",
            settings=self.settings,
            agent_run_id=self.agent_run_id,
            document_id=run.document_id,
            status=output["status"],
            duration_ms=int((time.perf_counter() - started) * 1000),
            record_count=result.record_count,
            field_count=result.field_count,
            review_count=result.review_count,
            quality_band=result.quality_band,
        )
        return output

    def result_for_run(self, run: StructuredExtractionRun) -> StructuredExtractionResult:
        """从长期事实重建统一结果，供异步恢复和会话刷新使用。"""

        rows = self.repository.load_fields(run.id)
        records_by_index: dict[int, dict[str, Any]] = {}
        review_items: list[dict[str, Any]] = []
        low_confidence_keys: list[str] = []
        located_retry_keys: list[str] = []
        located_retry_pages: set[int] = set()
        for row in rows:
            field_payload = {
                "raw_text": mask_sensitive_value(field_type=row.field_type, value=row.raw_text),
                "normalized_value": mask_sensitive_value(
                    field_type=row.field_type,
                    value=row.normalized_value_json,
                ),
                "confidence": row.confidence,
                "status": row.status,
                "evidence": {
                    "page_number": row.page_number,
                    "bbox": dict(row.bbox_json or {}),
                },
                "warnings": list(row.warning_codes_json or []),
            }
            records_by_index.setdefault(
                row.record_index,
                {"record_index": row.record_index, "fields": {}},
            )["fields"][row.field_key] = field_payload
            if row.status in {"NEEDS_REVIEW", "MISSING", "CONFLICTED"}:
                if row.field_key not in low_confidence_keys:
                    low_confidence_keys.append(row.field_key)
                if row.page_number and _valid_bbox(row.bbox_json) and row.field_key not in located_retry_keys:
                    located_retry_keys.append(row.field_key)
                if row.page_number and _valid_bbox(row.bbox_json):
                    located_retry_pages.add(int(row.page_number))
                review_items.append(
                    {
                        "record_index": row.record_index,
                        "field_key": row.field_key,
                        "field_label": row.field_label,
                        "raw_text": mask_sensitive_value(
                            field_type=row.field_type,
                            value=row.raw_text,
                        ),
                        "status": row.status,
                        "reason_codes": list(row.warning_codes_json or []),
                        "page_number": row.page_number,
                    }
                )
        retryable = bool(
            run.retry_strategy == "INITIAL"
            and run.quality_band == "MEDIUM"
            and located_retry_keys
            and len(located_retry_pages) == 1
            and len(located_retry_keys) <= self.settings.structured_extraction_max_retry_fields
            and self._vision_retry_available()
        )
        result = StructuredExtractionResult(
            field_schema=list(run.field_schema_json or []),
            records=list(records_by_index.values()),
            review_items=review_items,
            record_count=int(run.record_count or len(records_by_index)),
            field_count=len(run.field_schema_json or []),
            review_count=int(run.review_count or len(review_items)),
            missing_required_field_count=int(run.missing_required_field_count or 0),
            quality_score=float(run.quality_score or 0),
            quality_band=str(run.quality_band or "LOW"),
            retryable=retryable,
            recommended_retry_strategy="VISION_CROP" if retryable else "NONE",
            low_confidence_field_keys=(located_retry_keys if retryable else low_confidence_keys)[:20],
            original_unchanged=True,
        )
        if run.retry_strategy != "INITIAL":
            return result
        child = (
            self.db.query(StructuredExtractionRun)
            .filter(
                StructuredExtractionRun.parent_run_id == run.id,
                StructuredExtractionRun.status.in_({"COMPLETED", "PARTIAL", "NEEDS_REVIEW"}),
            )
            .order_by(StructuredExtractionRun.updated_at.desc())
            .first()
        )
        if child is None:
            return result
        # 运行恢复和缓存复用必须得到与首次回执相同的父子合并事实。
        from app.modules.structured_extraction.autonomous_loop import merge_structured_outputs

        merged = merge_structured_outputs(
            initial=result.model_dump(),
            enhanced=self.result_for_run(child).model_dump(),
            target_field_keys=list(child.target_field_keys_json or []),
        )
        return StructuredExtractionResult.model_validate(
            {
                key: merged[key]
                for key in StructuredExtractionResult.model_fields
                if key in merged
            }
        )

    def _normalize_candidates(
        self,
        *,
        run: StructuredExtractionRun,
        layout_extraction_run_id: str,
        fields: list[StructuredFieldSpec],
        candidates: CandidateExtraction,
        elements: list[EvidenceElement],
    ) -> tuple[StructuredExtractionResult, list[tuple[int, NormalizedField]]]:
        """补齐缺失字段并执行确定性归一化、证据和质量判定。"""

        evidence_by_id = {element.id: element for element in elements}
        normalized_rows: list[tuple[int, NormalizedField]] = []
        record_payloads: list[dict[str, Any]] = []
        review_items: list[dict[str, Any]] = []
        confidence_values: list[float] = []
        missing_required = 0
        low_keys: list[str] = []
        candidate_records = list(candidates.records or [])
        if not candidate_records and fields:
            # 零输出也要形成显式缺失/复核事实，不能伪装成成功。
            candidate_records = [CandidateRecord(record_index=1, fields={})]
        for candidate_record in candidate_records:
            record_fields: dict[str, Any] = {}
            for field in fields:
                candidate = candidate_record.fields.get(field.key)
                raw_text = candidate.raw_text if candidate else None
                normalized_value, status, warnings = normalize_field_value(
                    field=field,
                    raw_text=raw_text,
                    candidate_value=candidate.value if candidate else None,
                )
                accepted, evidence_warnings = validate_field_evidence(
                    raw_text=raw_text,
                    evidence_element_ids=(candidate.evidence_element_ids if candidate else []),
                    allowed_elements=evidence_by_id,
                    document_id=run.document_id,
                    extraction_run_id=layout_extraction_run_id,
                )
                warnings = list(dict.fromkeys([*warnings, *evidence_warnings]))
                confidence = float(candidate.confidence if candidate else 0)
                if raw_text and evidence_warnings:
                    status = "NEEDS_REVIEW"
                if (
                    raw_text
                    and status in {"NORMALIZED", "EXTRACTED"}
                    and confidence < self.settings.structured_extraction_high_confidence
                ):
                    status = "NEEDS_REVIEW"
                    warnings = list(dict.fromkeys([*warnings, "LOW_CONFIDENCE"]))
                if status == "MISSING" and field.required:
                    missing_required += 1
                if raw_text and accepted:
                    confidence_values.append(confidence)
                if status in {"NEEDS_REVIEW", "MISSING", "CONFLICTED"}:
                    if field.key not in low_keys:
                        low_keys.append(field.key)
                normalized = NormalizedField(
                    key=field.key,
                    label=field.label,
                    field_type=field.field_type,
                    raw_text=raw_text,
                    normalized_value=normalized_value,
                    confidence=confidence,
                    status=status,
                    page_number=accepted[0].page_number if accepted else None,
                    bbox=merge_evidence_bbox(accepted),
                    evidence_element_ids=[element.id for element in accepted],
                    warning_codes=warnings,
                )
                normalized_rows.append((candidate_record.record_index, normalized))
                record_fields[field.key] = {
                    "raw_text": mask_sensitive_value(
                        field_type=field.field_type,
                        value=raw_text,
                    ),
                    "normalized_value": mask_sensitive_value(
                        field_type=field.field_type,
                        value=normalized_value,
                    ),
                    "confidence": confidence,
                    "status": status,
                    "evidence": {
                        "page_number": normalized.page_number,
                        "bbox": normalized.bbox,
                    },
                    "warnings": warnings,
                }
                if status in {"NEEDS_REVIEW", "MISSING", "CONFLICTED"}:
                    review_items.append(
                        {
                            "record_index": candidate_record.record_index,
                            "field_key": field.key,
                            "field_label": field.label,
                            "raw_text": mask_sensitive_value(
                                field_type=field.field_type,
                                value=raw_text,
                            ),
                            "status": status,
                            "reason_codes": warnings,
                            "page_number": normalized.page_number,
                        }
                    )
            record_payloads.append(
                {"record_index": candidate_record.record_index, "fields": record_fields}
            )
        average = (
            sum(confidence_values) / len(confidence_values)
            if confidence_values
            else 0.0
        )
        located_retry_keys = list(
            dict.fromkeys(
                field.key
                for _, field in normalized_rows
                if field.key in low_keys and field.page_number and _valid_bbox(field.bbox)
            )
        )
        located_retry_pages = {
            int(field.page_number)
            for _, field in normalized_rows
            if field.key in low_keys and field.page_number and _valid_bbox(field.bbox)
        }
        has_bounded_vision_retry = bool(
            run.retry_strategy == "INITIAL"
            and located_retry_keys
            and len(located_retry_pages) == 1
            and len(located_retry_keys) <= self.settings.structured_extraction_max_retry_fields
            and self._vision_retry_available()
        )
        if not candidate_records:
            quality_band = "LOW"
        elif not review_items and not missing_required and average >= self.settings.structured_extraction_high_confidence:
            quality_band = "HIGH"
        elif has_bounded_vision_retry or average >= self.settings.structured_extraction_retry_confidence:
            quality_band = "MEDIUM"
        else:
            quality_band = "LOW"
        retryable = bool(
            quality_band == "MEDIUM" and has_bounded_vision_retry
        )
        return (
            StructuredExtractionResult(
                field_schema=[field.model_dump() for field in fields],
                records=record_payloads,
                review_items=review_items,
                record_count=len(record_payloads),
                field_count=len(fields),
                review_count=len(review_items),
                missing_required_field_count=missing_required,
                quality_score=round(average, 4),
                quality_band=quality_band,
                retryable=retryable,
                recommended_retry_strategy="VISION_CROP" if retryable else "NONE",
                low_confidence_field_keys=(located_retry_keys if retryable else low_keys)[:20],
                original_unchanged=True,
            ),
            normalized_rows,
        )

    def _tool_output(
        self,
        *,
        run: StructuredExtractionRun,
        result: StructuredExtractionResult,
        reused: bool,
        changeset_id: str | None = None,
        export_artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """生成严格 Tool 输出；复用事实不改变原件。"""

        status = (
            "COMPLETED"
            if result.quality_band == "HIGH"
            else "PARTIAL"
            if result.quality_band == "MEDIUM"
            else "NEEDS_REVIEW"
        )
        if export_artifact is None:
            exporter = StructuredExtractionExportService(
                db=self.db,
                settings=self.settings,
            )
            export_artifact = (
                exporter.ensure_export(run=run, result=result)
                if str(run.presentation).upper() in {"CSV", "XLSX"}
                else exporter.get_existing(run=run)
            )
        return {
            "kind": "structured_image_extraction",
            "ok": True,
            "status": status,
            "changeset_id": changeset_id,
            "document_id": run.document_id,
            "structured_extraction_run_id": run.id,
            "schema_mode": run.schema_mode,
            "record_mode": run.record_mode,
            "presentation": _resolve_presentation(
                requested=run.presentation,
                record_count=result.record_count,
            ),
            **result.model_dump(),
            "export_artifact": export_artifact,
            "reused": reused,
        }

    def _validate_source_limits(self, *, file_path: Path, content_type: str) -> None:
        """在入队前限制图片像素与 PDF 页数。"""

        if content_type in SUPPORTED_IMAGE_CONTENT_TYPES:
            try:
                with Image.open(file_path) as image:
                    frame_count = int(getattr(image, "n_frames", 1) or 1)
                    if frame_count > self.settings.pp_structure_max_pdf_pages:
                        raise ValueError("多页图片页数超过结构化抽取上限，请拆分后重试。")
                    pixels = 0
                    for frame_index in range(frame_count):
                        image.seek(frame_index)
                        pixels += int(image.width) * int(image.height)
                        if pixels > self.settings.pp_structure_max_image_pixels:
                            break
            except ValueError:
                raise
            except (OSError, Image.DecompressionBombError) as exc:
                raise ValueError("图片文件无法安全读取，请检查格式后重试。") from exc
            if pixels > self.settings.pp_structure_max_image_pixels:
                raise ValueError("图片像素超过结构化抽取上限，请压缩或拆分后重试。")
            return
        if content_type == "application/pdf":
            try:
                import fitz
            except ImportError as exc:
                raise ValueError("当前环境缺少 PDF 页数检查依赖。") from exc
            try:
                with fitz.open(file_path) as document:
                    if document.page_count > self.settings.pp_structure_max_pdf_pages:
                        raise ValueError("PDF 页数超过结构化抽取上限，请拆分后重试。")
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError("PDF 文件无法安全读取，请检查格式后重试。") from exc

    def _vision_retry_available(self) -> bool:
        """局部图片只有在部署显式授权且 Provider 支持时才能离开文件边界。"""

        return bool(
            getattr(self.settings, "structured_extraction_external_images_authorized", False)
            and getattr(self.extraction_provider, "supports_vision_retry", False)
            and hasattr(self.extraction_provider, "extract_with_image")
        )

    def _targeted_crop_data_url(
        self,
        *,
        run: StructuredExtractionRun,
        source_path: Path,
        content_type: str,
    ) -> str:
        """只使用父运行持久化 bbox 生成局部 PNG；Planner 无法提供坐标或路径。"""

        if not run.parent_run_id or not run.target_field_keys_json:
            raise RuntimeError("局部视觉增强缺少父运行或目标字段。")
        rows = (
            self.db.query(StructuredExtractionField)
            .filter(
                StructuredExtractionField.structured_extraction_run_id == run.parent_run_id,
                StructuredExtractionField.field_key.in_(run.target_field_keys_json),
            )
            .all()
        )
        located = [row for row in rows if row.page_number and _valid_bbox(row.bbox_json)]
        if not located:
            raise RuntimeError("低置信度字段没有可用于局部增强的持久化 bbox。")
        page_numbers = {int(row.page_number) for row in located}
        if len(page_numbers) != 1:
            raise RuntimeError("局部视觉增强目标跨页，已保留初始识别结果。")
        page_number = next(iter(page_numbers))
        if content_type == "application/pdf":
            image = _render_pdf_page(source_path=source_path, page_number=page_number)
            parent = self.db.get(StructuredExtractionRun, run.parent_run_id)
            page = (
                self.db.query(DocumentPage)
                .filter(
                    DocumentPage.extraction_run_id == parent.layout_extraction_run_id,
                    DocumentPage.page_number == page_number,
                )
                .one_or_none()
                if parent is not None and parent.layout_extraction_run_id
                else None
            )
            metadata = dict(page.metadata_json or {}) if page is not None else {}
            source_width = float(metadata.get("width") or image.width)
            source_height = float(metadata.get("height") or image.height)
            scale_x = image.width / max(1.0, source_width)
            scale_y = image.height / max(1.0, source_height)
        else:
            image = Image.open(source_path).convert("RGB")
            scale_x = scale_y = 1.0
        boxes = [dict(row.bbox_json or {}) for row in located]
        left = min(float(box["left"]) for box in boxes) * scale_x
        top = min(float(box["top"]) for box in boxes) * scale_y
        right = max(float(box["right"]) for box in boxes) * scale_x
        bottom = max(float(box["bottom"]) for box in boxes) * scale_y
        padding = 24
        crop = image.crop(
            (
                max(0, int(left) - padding),
                max(0, int(top) - padding),
                min(image.width, int(right) + padding),
                min(image.height, int(bottom) + padding),
            )
        )
        if crop.width <= 0 or crop.height <= 0:
            raise RuntimeError("局部视觉增强 bbox 无效。")
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG", optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"


def _valid_bbox(value: Any) -> bool:
    """只接受后端持久化的完整有限矩形。"""

    if not isinstance(value, dict):
        return False
    coordinates = [value.get(key) for key in ("left", "top", "right", "bottom")]
    return (
        all(
            isinstance(item, (int, float))
            and item == item
            and item not in {float("inf"), float("-inf")}
            for item in coordinates
        )
        and float(value["right"]) > float(value["left"])
        and float(value["bottom"]) > float(value["top"])
    )


def _render_pdf_page(*, source_path: Path, page_number: int) -> Image.Image:
    """把指定 PDF 页渲染为局部增强的内存 RGB 图像。"""

    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("当前环境缺少 PDF 局部增强依赖。") from exc
    with fitz.open(source_path) as document:
        if page_number < 1 or page_number > document.page_count:
            raise RuntimeError("局部增强页码越界。")
        pixmap = document.load_page(page_number - 1).get_pixmap(
            matrix=fitz.Matrix(2, 2),
            alpha=False,
        )
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def structured_extraction_fingerprint(tool_input: StructuredImageExtractionInput) -> str:
    """为动态 Schema、记录模式和重试目标生成稳定指纹。"""

    payload = tool_input.model_dump()
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    """在 Worker 执行前校验磁盘字节仍对应不可变 DocumentVersion。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _layout_config_fingerprint(
    *,
    provider: LayoutParsingProviderProtocol,
    settings: Settings,
) -> str:
    """生成 PP-Structure Provider 配置指纹。"""

    identity = "|".join(
        [
            provider.name,
            provider.version,
            settings.pp_structure_pipeline_config,
            settings.pp_structure_device,
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _resolve_presentation(*, requested: str, record_count: int) -> str:
    """仅在用户未明确格式时选择安全默认展示。"""

    if requested != "AUTO":
        return requested
    return "TABLE" if record_count > 1 else "JSON"


def _failure_output(*, document_id: str, code: str, message: str) -> dict[str, Any]:
    """返回严格且不含路径的失败输出。"""

    return {
        "kind": "structured_image_extraction",
        "ok": False,
        "status": "FAILED",
        "document_id": document_id,
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
            "user_action_required": code
            in {
                "STRUCTURED_EXTRACTION_SOURCE_LIMIT_EXCEEDED",
                "STRUCTURED_EXTRACTION_FIELD_LIMIT_EXCEEDED",
                "UNSUPPORTED_STRUCTURED_IMAGE_TYPE",
            },
        },
        "record_count": 0,
        "field_count": 0,
        "review_count": 0,
        "missing_required_field_count": 0,
        "retryable": False,
        "recommended_retry_strategy": "NONE",
        "low_confidence_field_keys": [],
        "field_schema": [],
        "records": [],
        "review_items": [],
        "original_unchanged": True,
    }
