"""共享累计表格 Demo：复用工作副本、结构化事实、证据规范化和 XLSX writer。"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.db.models import (
    ChangeItem, DocumentElement, DocumentExtractionRun, DocumentPage, DocumentVersion, StructuredDataset,
    StructuredDatasetExport, StructuredDatasetMember, StructuredExtractionRun,
)
from app.modules.agent.tool_schemas import StructuredFieldSpec, StructuredImageExtractionInput
from app.modules.file_lifecycle.shared_access import CanonicalWorkingFileResolver, CanonicalWorkingFileError
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.structured_extraction.export import _safe_spreadsheet_text, _write_xlsx_atomic
from app.modules.structured_extraction.repository import StructuredExtractionRepository
from app.modules.structured_extraction.normalization import normalize_field_value
from app.modules.structured_extraction.schemas import (
    CandidateExtraction, CandidateRecord, CandidateFieldValue,
    LayoutParseResult, LayoutPage, LayoutElement,
)
from app.modules.structured_extraction.service import StructuredExtractionService
from .schemas import DatasetCreate, DatasetAppend, DatasetQuery


def fail(code: str, message: str, status: int = 409):
    """通过现有异常响应转换输出稳定业务错误。"""
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def digest(value) -> str:
    """对完整请求语义生成摘要，正文不进入审计字段。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class StructuredDatasetService:
    """所有登录用户共享数据集；写入由 runtime 白名单节点调用并提交事务。"""

    def __init__(self, db, user, agent_run=None):
        """依赖限于本次请求，不缓存用户或数据库会话。"""
        self.db, self.user, self.agent_run = db, user, agent_run
        self.settings = get_settings()
        self.repository = StructuredExtractionRepository(db)
        self.storage = FileLifecycleStorageService(self.settings)

    def dataset(self, dataset_id: str, *, lock=False):
        """按共享工作区读取，创建人不构成访问限制。"""
        query = self.db.query(StructuredDataset).filter(
            StructuredDataset.id == dataset_id,
            StructuredDataset.workspace_id == get_shared_workspace_id(self.db),
        )
        result = (query.with_for_update() if lock else query).one_or_none()
        if result is None:
            fail("DATASET_NOT_FOUND", "累计表格不存在。", 404)
        return result

    def create(self, request: DatasetCreate):
        """固定共享 schema，同用户同创建键重试复用原对象。"""
        key = digest([self.user.id, "dataset-create", request.idempotency_key])
        payload_digest = digest(request.model_dump(mode="json", exclude={"idempotency_key"}))
        existing = self.db.query(StructuredDataset).filter_by(creation_key=key).one_or_none()
        if existing:
            if existing.creation_digest != payload_digest:
                fail("IDEMPOTENCY_CONFLICT", "此创建键已用于不同表格。")
            return {**self.get(existing.id), "reused": True}
        row = StructuredDataset(
            workspace_id=get_shared_workspace_id(self.db), created_by=self.user.id,
            name=request.name, schema_json=[f.model_dump() for f in request.fields],
            creation_key=key, creation_digest=payload_digest,
        )
        self.db.add(row)
        self.db.flush()
        return {**self.get(row.id), "reused": False}

    def list_datasets(self):
        """列出小 Demo 的共享数据集，重连后仍可找到稳定 ID。"""
        rows = self.db.query(StructuredDataset).filter_by(
            workspace_id=get_shared_workspace_id(self.db)
        ).order_by(StructuredDataset.created_at.desc()).limit(100).all()
        return {"datasets": [{"dataset_id": r.id, "name": r.name, "revision": r.revision} for r in rows]}

    def get(self, dataset_id):
        """读取 schema、累计状态及最新导出，不暴露操作用户和本地路径。"""
        row = self.dataset(dataset_id)
        members = self._members(row)
        versions = {
            version.id: version
            for version in self.db.query(DocumentVersion).filter(
                DocumentVersion.id.in_([member.document_version_id for member, _ in members])
            ).all()
        } if members else {}
        format_counts = {"image": 0, "word": 0, "pdf": 0}
        for member, _ in members:
            suffix = Path(versions[member.document_version_id].filename).suffix.lower()
            category = "image" if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"} else "pdf" if suffix == ".pdf" else "word"
            format_counts[category] += 1
        return {
            "dataset_id": row.id, "name": row.name, "revision": row.revision,
            "fields": row.schema_json, "document_count": len(members),
            "image_count": format_counts["image"], "format_counts": format_counts,
            "record_count": sum(run.record_count for _, run in members),
        }

    def source(self, document_id, *, version_id=None, extraction_run_id=None, content_origin=None):
        """读取共享图片或宿主全文事实，绝不再次 OCR、转换或解析原件。"""
        try:
            source = CanonicalWorkingFileResolver(self.db).resolve_document(
                document_id=document_id, document_version_id=version_id,
            )
        except CanonicalWorkingFileError as exc:
            fail(exc.code, str(exc))
        version = source.document_version
        if version.document_id != document_id:
            fail("USE_WORKING_DOCUMENT", "请使用已整理工作副本的 Document ID。")
        suffix = Path(version.filename).suffix.lower()
        is_image = suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".pdf", ".doc", ".docx"}:
            fail("DEMO_DOCUMENT_REQUIRED", "Demo 仅支持图片、PDF、DOC 或 DOCX 工作副本。", 422)
        query = self.db.query(DocumentExtractionRun).filter_by(
            document_id=document_id, document_version_id=version.id, status="COMPLETED",
        )
        if extraction_run_id:
            query = query.filter(DocumentExtractionRun.id == extraction_run_id)
        elif content_origin == "WORKBUDDY" or not is_image:
            query = query.filter(DocumentExtractionRun.extractor == "workbuddy-external-document")
        elif is_image:
            query = query.filter(DocumentExtractionRun.extractor != "workbuddy-dataset-reuse")
        extraction = query.order_by(DocumentExtractionRun.created_at.desc()).first()
        pages = self.db.query(DocumentPage).filter_by(extraction_run_id=extraction.id).all() if extraction else []
        if not pages or not any(page.text_content.strip() for page in pages):
            fail("EXTRACTION_NOT_READY", "请先完成宿主内容提取和工作副本整理流程。")
        if is_image and (len(pages) != 1 or pages[0].page_number != 1):
            fail("OCR_NOT_READY", "图片必须具有现有单页 OCR 结果。")
        if content_origin == "WORKBUDDY" and extraction.extractor != "workbuddy-external-document":
            fail("WORKBUDDY_CONTENT_REQUIRED", "该来源没有合格的 WorkBuddy 文档提取正文。")
        if sum(len(page.text_content) for page in pages) > 100_000:
            fail("DEMO_SOURCE_LIMIT", "文件正文超过 Demo 处理范围。", 422)
        ordered_pages = sorted(
            pages,
            key=lambda page: (
                int((page.metadata_json or {}).get("unit_order", 0)),
                page.created_at,
            ),
        )
        source_elements = self.db.query(DocumentElement).filter_by(extraction_run_id=extraction.id).all()
        elements_by_unit: dict[str, list[str]] = {}
        for element in source_elements:
            external_unit_id = str((element.metadata_json or {}).get("source_unit_id") or "")
            if external_unit_id:
                elements_by_unit.setdefault(external_unit_id, []).append(element.id)
        source_units = [
            {
                "source_unit_id": page.id,
                "kind": str((page.metadata_json or {}).get("unit_kind") or ("IMAGE_PAGE" if is_image else "TEXT_SECTION")),
                "order": int((page.metadata_json or {}).get("unit_order", offset)),
                "page_number": page.page_number,
                "text": page.text_content,
                "source_element_ids": elements_by_unit.get(str((page.metadata_json or {}).get("source_unit_id") or ""), []),
            }
            for offset, page in enumerate(ordered_pages)
        ]
        return {
            "document_id": document_id, "document_version_id": version.id,
            "filename": source.working_copy.filename, "source_sha256": version.sha256,
            "extraction_run_id": extraction.id,
            "content_origin": "WORKBUDDY" if extraction.extractor == "workbuddy-external-document" else "LEGACY_IMAGE",
            "source_kind": "IMAGE" if is_image else "WORKBUDDY_DOCUMENT",
            "coverage": dict((ordered_pages[0].metadata_json or {}).get("coverage") or {}) if ordered_pages else {},
            "source_units": source_units,
            # 旧图片调用方继续得到原 pages 形状；新 Skill 使用 source_units 的正式 UUID。
            "pages": [{"page_number": page.page_number, "text": page.text_content} for page in ordered_pages] if is_image else [],
        }

    def append(self, dataset_id, request: DatasetAppend):
        """在同一事务锁定数据集，保存单图事实及成员，再推进 revision。"""
        dataset = self.dataset(dataset_id, lock=True)
        payload_for_digest = request.model_dump(mode="json", exclude={"idempotency_key"})
        if payload_for_digest.get("content_origin") is None:
            payload_for_digest.pop("content_origin", None)
        for record in payload_for_digest.get("records", []):
            for value in record.get("fields", {}).values():
                if not value.get("evidence_refs"):
                    value.pop("evidence_refs", None)
        payload_digest = digest(payload_for_digest)
        existing = self.db.query(StructuredDatasetMember).filter_by(
            dataset_id=dataset.id, append_key=request.idempotency_key,
        ).one_or_none()
        if existing:
            if existing.payload_digest != payload_digest:
                fail("IDEMPOTENCY_CONFLICT", "此提交键已用于不同结构化结果。")
            return {**self.get(dataset.id), "appended_revision": existing.appended_revision, "reused": True}
        if self.db.query(StructuredDatasetMember.id).filter_by(
            dataset_id=dataset.id, document_version_id=str(request.document_version_id)
        ).first():
            fail("SOURCE_ALREADY_APPENDED", "此文件版本已经加入该表格。")
        if self.get(dataset.id)["record_count"] + len(request.records) > 1000:
            fail("DEMO_RECORD_LIMIT", "Demo 每个表格最多累计 1000 条记录。", 422)
        source = self.source(str(request.document_id), version_id=str(request.document_version_id),
                             extraction_run_id=str(request.extraction_run_id), content_origin=request.content_origin)
        if source["source_sha256"] != request.source_sha256:
            fail("SOURCE_SNAPSHOT_CHANGED", "文件内容哈希与提交时不一致。")
        if source["source_kind"] == "WORKBUDDY_DOCUMENT" and request.content_origin != "WORKBUDDY":
            fail("WORKBUDDY_CONTENT_REQUIRED", "Word/PDF 追加必须明确固定 WorkBuddy 正文来源。")
        if not request.records:
            return {**self.get(dataset.id), "appended_revision": None, "review_field_count": 0,
                    "record_count_added": 0, "message": "未找到匹配记录", "original_unchanged": True, "reused": False}
        fields = [StructuredFieldSpec.model_validate(f) for f in dataset.schema_json]
        keys = {f.key for f in fields}
        if any(set(row.fields) - keys for row in request.records):
            fail("UNKNOWN_FIELD", "提交包含当前表格未定义的字段。", 422)
        run = self.repository.create_run(
            tool_input=StructuredImageExtractionInput(document_id=source["document_id"],
                schema_mode="EXPLICIT_FIELDS", fields=fields, record_mode="TABLE_ROWS", presentation="TABLE"),
            document_version_id=source["document_version_id"], schema_fingerprint=digest(dataset.schema_json),
            provider="workbuddy-dataset-demo", model_name="external-field-mapping",
            prompt_version="workbuddy-dataset-demo-v1", agent_run_id=self.agent_run.id,
        )
        # 完整原文来自已有事实；新运行只表达结构化证据派生，不覆盖基础 OCR 和索引。
        # Word 无真实页码时这里只生成内部 layout 序号，字段证据仍按 source unit_id 校验。
        layout_pages = [
            LayoutPage(page_number=index + 1, elements=[LayoutElement(element_index=index, text=unit["text"])])
            for index, unit in enumerate(source["source_units"])
        ]
        layout, elements = self.repository.create_layout_extraction(
            run=run, parser_config_hash=digest([source["extraction_run_id"], "dataset-demo-v1"]),
            layout=LayoutParseResult(provider="workbuddy-dataset-reuse", provider_version="1",
                pages=layout_pages),
        )
        persisted_elements = {
            element.id: element
            for element in self.db.query(DocumentElement).filter_by(extraction_run_id=layout.id).all()
        }
        derived_pages = self.db.query(DocumentPage).filter_by(extraction_run_id=layout.id).order_by(DocumentPage.page_number).all()
        for index, page in enumerate(derived_pages):
            page.metadata_json = {**page.metadata_json, "source_extraction_run_id": source["extraction_run_id"],
                                  "source_unit_id": source["source_units"][index]["source_unit_id"]}
        candidates = []
        for index, record in enumerate(request.records, 1):
            values = {}
            for key, value in record.fields.items():
                if source["source_kind"] == "WORKBUDDY_DOCUMENT" and value.raw_text and not value.evidence_refs:
                    fail("EVIDENCE_SCOPE_MISMATCH", "Word/PDF 字段必须提供正式 source_unit_id 和原文 quote。", 422)
                matched_offsets = []
                validated_refs = []
                for reference in value.evidence_refs:
                    matching = [(offset, unit) for offset, unit in enumerate(source["source_units"])
                                if unit["source_unit_id"] == str(reference.source_unit_id)]
                    if len(matching) != 1:
                        fail("EVIDENCE_SCOPE_MISMATCH", "字段证据不属于指定正文运行。", 422)
                    source_offset, source_unit = matching[0]
                    quote = reference.quote
                    if reference.start is not None:
                        if reference.end > len(source_unit["text"]) or source_unit["text"][reference.start:reference.end] != quote:
                            fail("EVIDENCE_SCOPE_MISMATCH", "字段引文范围与正式正文不一致。", 422)
                    elif source_unit["text"].count(quote) != 1:
                        fail("EVIDENCE_AMBIGUOUS", "字段引文在指定区段中不唯一，请提供字符范围。")
                    if value.raw_text and value.raw_text not in quote:
                        fail("EVIDENCE_SCOPE_MISMATCH", "字段原值没有被证据引文完整支持。", 422)
                    matched_offsets.append(source_offset)
                    validated_refs.append({
                        "_source_offset": source_offset,
                        "source_unit_id": str(reference.source_unit_id),
                        "quote": quote,
                        "start": reference.start,
                        "end": reference.end,
                        "source_element_ids": source_unit.get("source_element_ids", []),
                    })
                raw = re.sub(r"\s+", "", value.raw_text or "").casefold()
                if matched_offsets:
                    supported = all(raw in re.sub(r"\s+", "", source["source_units"][offset]["text"]).casefold()
                                    for offset in matched_offsets) if raw else False
                    evidence_ids = [elements[offset].id for offset in matched_offsets] if supported else []
                    if supported:
                        for offset in set(matched_offsets):
                            persisted_element = persisted_elements[elements[offset].id]
                            current = list((persisted_element.metadata_json or {}).get("source_evidence_refs") or [])
                            for evidence_ref in (
                                {key: item for key, item in evidence_ref.items() if key != "_source_offset"}
                                for evidence_ref in validated_refs
                                if evidence_ref["_source_offset"] == offset
                            ):
                                if evidence_ref not in current:
                                    current.append(evidence_ref)
                            persisted_element.metadata_json = {
                                **(persisted_element.metadata_json or {}),
                                "source_extraction_run_id": source["extraction_run_id"],
                                "source_evidence_refs": current,
                            }
                else:
                    # 旧图片 payload 沿用 page_number=1 的兼容定位。
                    matches = [(offset, unit) for offset, unit in enumerate(source["source_units"])
                               if unit["page_number"] == value.page_number]
                    evidence_ids = [elements[matches[0][0]].id] if len(matches) == 1 and raw in re.sub(r"\s+", "", matches[0][1]["text"]).casefold() else []
                values[key] = CandidateFieldValue(
                    raw_text=value.raw_text, value=None, confidence=value.confidence or 0,
                    evidence_element_ids=evidence_ids,
                )
            candidates.append(CandidateRecord(record_index=index, fields=values))
        # 禁用 Provider 配置，仅调用公共 finalizer；不触发网络和第二次 OCR。
        settings = self.settings.model_copy(update={
            "structured_extraction_layout_provider": "pp_structure_v3",
            "structured_extraction_llm_provider": "disabled", "structured_extraction_vision_provider": "disabled",
        })
        finalizer = StructuredExtractionService(db=self.db, user_id=self.user.id, settings=settings)
        result = finalizer.finalize_candidates(run=run, layout_extraction_run_id=layout.id,
            fields=fields, candidates=CandidateExtraction(records=candidates), elements=elements)
        changeset_id = self.repository.record_changeset(
            run=run, agent_run_id=self.agent_run.id, conversation_id=self.agent_run.conversation_id,
            user_id=self.user.id, review_count=result.review_count,
            field_count=result.field_count, record_count=result.record_count,
        )
        dataset.revision += 1
        member = StructuredDatasetMember(dataset_id=dataset.id, structured_extraction_run_id=run.id,
            document_version_id=source["document_version_id"], append_key=request.idempotency_key,
            payload_digest=payload_digest, appended_revision=dataset.revision)
        self.db.add(member)
        self.db.add(ChangeItem(changeset_id=changeset_id, target_type="DATASET", target_id=dataset.id,
            target_document_id=source["document_id"], change_type="STRUCTURED_DATASET_APPENDED",
            after_value_json={"revision": dataset.revision, "record_count": result.record_count},
            source="workbuddy-dataset-demo", execution_status="COMPLETED"))
        self.db.flush()
        return {**self.get(dataset.id), "appended_revision": dataset.revision,
                "review_field_count": result.review_count, "changeset_id": changeset_id,
                "original_unchanged": True, "reused": False}

    def _members(self, dataset):
        """固定 revision，历史运行不可被后续追加改写。"""
        return self.db.query(StructuredDatasetMember, StructuredExtractionRun).join(
            StructuredExtractionRun, StructuredExtractionRun.id == StructuredDatasetMember.structured_extraction_run_id
        ).filter(StructuredDatasetMember.dataset_id == dataset.id,
                 StructuredDatasetMember.appended_revision <= dataset.revision
        ).order_by(StructuredDatasetMember.appended_revision).all()

    def _records(self, dataset):
        """逐文件聚合字段，缺失可选字段不阻止整条记录进入正式结果。"""
        required = {f["key"] for f in dataset.schema_json if f.get("required")}
        records = []
        for member, run in self._members(dataset):
            grouped = {}
            for field in self.repository.load_fields(run.id):
                row = grouped.setdefault(field.record_index, {
                    "record_id": f"{member.id}:{field.record_index}", "document_id": run.document_id,
                    "document_version_id": run.document_version_id, "fields": {}, "needs_review": False,
                })
                invalid = field.status not in {"EXTRACTED", "NORMALIZED"} and not (
                    field.status == "MISSING" and field.field_key not in required)
                row["needs_review"] |= invalid
                row["fields"][field.field_key] = {
                    "value": field.normalized_value_json, "raw_text": field.raw_text,
                    "status": field.status, "confidence": field.confidence,
                    "page_number": field.page_number, "bbox": field.bbox_json,
                }
            records.extend(grouped.values())
        return records

    def query(self, dataset_id, request: DatasetQuery):
        """过滤和聚合使用完整合格集合，再截取返回页。"""
        dataset = self.dataset(dataset_id)
        fields = {f["key"]: f for f in dataset.schema_json}
        for condition in request.filters:
            if condition.field not in fields:
                fail("UNKNOWN_FIELD", "查询字段不存在。", 422)
            self._comparable(fields[condition.field], condition.value)
        if request.sum_field and (request.sum_field not in fields or fields[request.sum_field]["field_type"] not in {"integer", "decimal"}):
            fail("INVALID_SUM_FIELD", "只能对整数或小数字段求和。", 422)
        records = self._records(dataset)
        excluded = sum(row["needs_review"] for row in records)
        matched = []
        for row in records:
            if row["needs_review"] and not request.include_review:
                continue
            if all(self._matches(fields[c.field], row["fields"][c.field]["value"], c.op, c.value) for c in request.filters):
                matched.append(row)
        total = None
        if request.sum_field:
            # 即使查看待复核记录，合计也只纳入合格记录，避免未验证金额进入结论。
            total = str(sum((Decimal(str(r["fields"][request.sum_field]["value"])) for r in matched
                if not r["needs_review"] and r["fields"][request.sum_field]["value"] is not None), Decimal(0)))
        return {"dataset_id": dataset.id, "revision": dataset.revision, "total_records": len(records),
                "review_records": excluded, "matched_records": len(matched), "sum": total,
                "records": matched[request.offset:request.offset + request.limit],
                "truncated": request.offset + request.limit < len(matched)}

    def _comparable(self, field, value):
        """字符串金额不能按字典序比较；非法数值返回契约错误。"""
        if field["field_type"] == "date":
            normalized, status, _ = normalize_field_value(
                field=StructuredFieldSpec.model_validate(field), raw_text=str(value), candidate_value=None,
            )
            if status != "NORMALIZED":
                fail("INVALID_FILTER_VALUE", "日期过滤条件必须包含明确年月日。", 422)
            return normalized
        if field["field_type"] in {"integer", "decimal"}:
            try:
                number = Decimal(str(value))
                if not number.is_finite():
                    raise InvalidOperation
                return number
            except InvalidOperation:
                fail("INVALID_FILTER_VALUE", "数值过滤条件必须是有限数字。", 422)
        return str(value)

    def _matches(self, field, value, op, expected):
        """仅执行已校验的固定比较，不计算表达式。"""
        if value is None:
            return False
        left, right = self._comparable(field, value), self._comparable(field, expected)
        return {"eq": lambda: left == right, "contains": lambda: str(right) in str(left),
                "gte": lambda: left >= right, "lte": lambda: left <= right}[op]()

    def export(self, dataset_id):
        """小 Demo 按需同步导出；数据库记录和文件均以独立 export ID 发布。"""
        dataset = self.dataset(dataset_id, lock=True)
        export = self.db.query(StructuredDatasetExport).filter_by(
            dataset_id=dataset.id, dataset_revision=dataset.revision).one_or_none()
        if export:
            path = self.storage.dataset_export_path(dataset.id, export.id)
            if path.is_file() and self.storage.sha256_file(path) == export.sha256:
                return self._export_result(export, reused=True)
            fail("EXPORT_MISSING", "已发布快照缺失或损坏，需要检查存储。")
        records = [r for r in self._records(dataset) if not r["needs_review"]]
        export_id = str(uuid4())
        path = self.storage.dataset_export_path(dataset.id, export_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        keys = [f["key"] for f in dataset.schema_json]
        rows = [[_safe_spreadsheet_text(value) if isinstance(value, str) else value
                 for value in [r["fields"][k]["value"] for k in keys]] for r in records]
        _write_xlsx_atomic(target_path=path, headers=[_safe_spreadsheet_text(f["label"]) for f in dataset.schema_json], rows=rows)
        export = StructuredDatasetExport(id=export_id, dataset_id=dataset.id,
            dataset_revision=dataset.revision, sha256=self.storage.sha256_file(path),
            size_bytes=path.stat().st_size, storage_path=path.relative_to(Path(self.settings.file_storage_root).resolve()).as_posix())
        self.db.add(export)
        self.db.flush()
        return self._export_result(export, reused=False)

    def _export_result(self, export, *, reused):
        """导出只返回稳定对象引用，不暴露存储路径。"""
        return {"dataset_id": export.dataset_id, "revision": export.dataset_revision,
                "export_id": export.id, "sha256": export.sha256, "size_bytes": export.size_bytes,
                "download_url": f"/api/structured-datasets/{export.dataset_id}/exports/{export.id}/content",
                "reused": reused}

    def download(self, dataset_id, export_id):
        """登录用户均可下载共享快照，路径只通过 StorageService 生成。"""
        self.dataset(dataset_id)
        export = self.db.query(StructuredDatasetExport).filter_by(id=export_id, dataset_id=dataset_id).one_or_none()
        if export is None:
            fail("EXPORT_NOT_FOUND", "导出不存在。", 404)
        path = self.storage.dataset_export_path(dataset_id, export_id)
        if not path.is_file() or self.storage.sha256_file(path) != export.sha256:
            fail("EXPORT_MISSING", "导出文件缺失或校验失败。", 404)
        return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            filename=f"dataset-{dataset_id}-r{export.dataset_revision}.xlsx")
