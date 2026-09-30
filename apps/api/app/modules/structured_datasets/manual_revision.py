"""用户手工补全已发布 Excel 后，将完整文件发布为同一工作副本的新版本。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
import zipfile

from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries
from sqlalchemy import func
from fastapi import HTTPException

from app.db.models import DocumentVersion, StructuredDatasetExport, StructuredDatasetTask, utcnow
from app.modules.structured_extraction.workbook_layout import WorkbookLayout

from .publication import stage_publication
from .service import StructuredDatasetService, fail
from .xlsx_safety import XlsxSafetyError, inspect_xlsx


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class ManualRevisionService:
    def __init__(self, db, user, agent_run=None):
        self.db = db
        self.user = user
        self.datasets = StructuredDatasetService(db, user, agent_run)

    def publish(self, request, path: Path) -> dict:
        existing = self.db.query(StructuredDatasetTask).filter_by(
            user_id=self.user.id, request_key=f"manual-revision:{request.request_key}").one_or_none()
        if existing is not None:
            snapshot = existing.manifest_json or {}
            if (snapshot.get("filename") != request.filename
                    or snapshot.get("base_document_version_id") != str(request.base_document_version_id)
                    or snapshot.get("base_sha256") != request.base_sha256
                    or snapshot.get("base_revision") != request.base_revision
                    or (existing.progress_json or {}).get("file_sha256") != request.file_sha256):
                fail("MANUAL_EXCEL_REQUEST_CONFLICT", "请求键已用于其他目标、版本或文件。", 409)
            return self.receipt(existing)
        if not path.is_file() or self.datasets.storage.sha256_file(path) != request.file_sha256:
            fail("MANUAL_EXCEL_FILE_CHANGED", "上传文件与声明的哈希不一致。", 422)
        target = self.datasets.resolve_target(
            request.filename, manual_upload_sha256=request.file_sha256)
        dataset = self.datasets.dataset(target["dataset_id"], lock=True)
        working = self.datasets._dataset_working_copy(dataset)
        if working is None or not working.current_version_id:
            fail("MANUAL_EXCEL_TARGET_MISSING", "目标 Excel 尚未发布。", 409)
        self.datasets._assert_task_write_access(dataset)
        current = self.db.get(DocumentVersion, working.current_version_id)
        if (current is None or current.id != str(request.base_document_version_id)
                or current.sha256 != request.base_sha256 or dataset.revision != request.base_revision
                or working.filename != request.filename):
            fail("MANUAL_EXCEL_VERSION_CONFLICT", "目标 Excel 已变化，请下载最新版本后重新补充。", 409)
        if current.sha256 == request.file_sha256:
            fail("MANUAL_EXCEL_UNCHANGED", "上传文件与当前版本相同。", 422)
        visible = self.datasets.storage.working_copy_path(
            self.datasets._working_copy_storage_path(working))
        visible_sha256 = self.datasets.storage.sha256_file(visible) if visible.is_file() else None
        if visible_sha256 not in {current.sha256, request.file_sha256}:
            fail("MANUAL_EXCEL_VISIBLE_CONFLICT", "服务器同名 Excel 与上传文件不一致，请先核对两份修改。", 409)
        base_path = self.datasets.storage.working_copy_path(current.storage_path)
        if not base_path.is_file() or self.datasets.storage.sha256_file(base_path) != current.sha256:
            fail("MANUAL_EXCEL_BASE_MISSING", "目标基线缺失或损坏。", 409)
        changes = self._validate_and_diff(dataset, base_path, path)
        task = StructuredDatasetTask(
            user_id=self.user.id, request_key=f"manual-revision:{request.request_key}",
            result_digest=request.file_sha256, review_mode="DIRECT_SAVE", status="PUBLISHING",
            manifest_json={"mode": "MANUAL_REVISION", "dataset_id": dataset.id,
                           "working_copy_id": working.id, "base_document_version_id": current.id,
                           "base_sha256": current.sha256, "base_revision": dataset.revision,
                           "filename": working.filename},
            payload_json={"changes": changes}, progress_json={
                "file_sha256": request.file_sha256,
                "visible_sha256_before_publish": visible_sha256,
            },
            result_json={}, error_json={},
        )
        self.db.add(task)
        self.db.flush()
        dataset.active_task_id = task.id
        dataset.revision += 1
        export_id = str(uuid4())
        version_number = int(self.db.query(func.max(DocumentVersion.version_number)).filter(
            DocumentVersion.document_id == working.document_id).scalar() or 0) + 1
        relative_path = self.datasets.storage.dataset_working_version_relative_path(
            dataset_id=dataset.id, export_id=export_id, version_number=version_number)
        self.datasets.storage.import_working_copy(
            source=path, relative_path=relative_path, expected_sha256=request.file_sha256)
        working, version = self.datasets._publish_working_version(
            dataset=dataset, working_copy=working, parent_version=current,
            storage_path=relative_path, version_number=version_number,
            sha256=request.file_sha256, size_bytes=path.stat().st_size)
        version.extraction_policy_json = {**dict(version.extraction_policy_json or {}),
            "structured_dataset_export_mode": "package-append-v3", "manual_revision": True}
        export = StructuredDatasetExport(
            id=export_id, dataset_id=dataset.id, dataset_revision=dataset.revision,
            storage_path=relative_path, sha256=request.file_sha256,
            size_bytes=path.stat().st_size, document_version_id=version.id)
        self.db.add(export)
        result = {**self.datasets._export_result(export, reused=False),
                  "manual_changed_cell_count": len(changes), "task_record_count": max(1, len(changes))}
        stage = stage_publication(self.db, dataset=dataset, working_copy=working,
            version=version, base_version_id=current.id, task=task,
            result=result, agent_run=self.datasets.agent_run)
        return {**self.receipt(task), "_publication_id": stage.id}

    def receipt(self, task) -> dict:
        return {"task_id": task.id, "status": task.status,
                "dataset_id": (task.manifest_json or {}).get("dataset_id"),
                "filename": (task.manifest_json or {}).get("filename"),
                "excel_published": task.status == "COMPLETED",
                "error": task.error_json or None, **(task.result_json or {})}

    def _validate_and_diff(self, dataset, base_path: Path, path: Path) -> list[dict]:
        try:
            inspect_xlsx(base_path)
            inspect_xlsx(path)
            with zipfile.ZipFile(path) as archive:
                if any(name.lower().startswith("xl/externallinks/") for name in archive.namelist()):
                    fail("MANUAL_EXCEL_EXTERNAL_LINK", "补全文件不能包含外部工作簿链接。", 422)
            baseline = load_workbook(base_path, read_only=False, data_only=False)
            candidate = load_workbook(path, read_only=False, data_only=False)
        except XlsxSafetyError as exc:
            fail(exc.code, str(exc), 413 if "TOO_LARGE" in exc.code or "TOO_COMPLEX" in exc.code else 422)
        except HTTPException:
            raise
        except Exception:
            fail("MANUAL_EXCEL_INVALID", "Excel 无法安全读取。", 422)
        try:
            if baseline.sheetnames != candidate.sheetnames:
                fail("MANUAL_EXCEL_STRUCTURE_CHANGED", "不能增删或重命名工作表。", 422)
            name = dataset.target_sheet_name or "Sheet1"
            if name not in candidate.sheetnames:
                fail("MANUAL_EXCEL_STRUCTURE_CHANGED", "目标工作表不存在。", 422)
            for old_sheet, new_sheet in zip(baseline.worksheets, candidate.worksheets):
                if {str(item) for item in old_sheet.merged_cells.ranges} != {
                        str(item) for item in new_sheet.merged_cells.ranges}:
                    fail("MANUAL_EXCEL_STRUCTURE_CHANGED", "不能改变工作表合并区域。", 422)
                for coordinate, old_cell in old_sheet._cells.items():
                    new_cell = new_sheet._cells.get(coordinate)
                    if old_cell.value is not None and (new_cell is None or new_cell.value is None):
                        fail("MANUAL_EXCEL_CELL_CLEARED", "不能清空原有非空单元格。", 422,
                             cell=old_cell.coordinate)
                    if old_cell.data_type == "f" and (new_cell is None or new_cell.value != old_cell.value):
                        fail("MANUAL_EXCEL_FORMULA_CHANGED", "不能修改原有公式。", 422,
                             cell=old_cell.coordinate)
                for coordinate, new_cell in new_sheet._cells.items():
                    old_cell = old_sheet._cells.get(coordinate)
                    if new_cell.data_type == "f" and (old_cell is None or old_cell.value != new_cell.value):
                        fail("MANUAL_EXCEL_FORMULA_CHANGED", "不能新增或修改公式。", 422,
                             cell=new_cell.coordinate)
            old_sheet = baseline[name]
            new_sheet = candidate[name]
            records = self.datasets._records(dataset)
            left, top, right, bottom = (range_boundaries(dataset.target_layout_json["data_range"])
                if dataset.target_layout_json else
                (1, 2, len(dataset.schema_json), 1 + len(records)))
            if right - left + 1 != len(dataset.schema_json):
                fail("MANUAL_EXCEL_STRUCTURE_CHANGED", "目标表数据列范围与 schema 不一致。", 422)
            if len(records) > bottom - top + 1:
                fail("MANUAL_EXCEL_STRUCTURE_CHANGED", "目标表数据行范围与已登记记录不一致。", 422)
            for (row, column), cell in new_sheet._cells.items():
                if (row >= top + len(records) and left <= column <= right and cell.value is not None
                        and old_sheet.cell(row, column).value is None):
                    fail("MANUAL_EXCEL_NEW_ROW", "新增人员须使用来源追加流程。", 422)
            changes = []
            for index, record in enumerate(records):
                row = top + index
                for offset, spec in enumerate(dataset.schema_json):
                    column = left + offset
                    before = old_sheet.cell(row, column).value
                    after = new_sheet.cell(row, column).value
                    if before == after:
                        continue
                    field_type = spec.get("field_type")
                    if after is not None and field_type in {"integer", "decimal"}:
                        try:
                            number = Decimal(str(after))
                            if isinstance(after, bool) or not number.is_finite() or (
                                    field_type == "integer" and number != number.to_integral_value()):
                                raise InvalidOperation
                        except (InvalidOperation, ValueError):
                            fail("MANUAL_EXCEL_FIELD_TYPE", "补充值不符合目标列的数值类型。", 422,
                                 cell=new_sheet.cell(row, column).coordinate)
                    if after is not None and field_type == "date":
                        try:
                            self.datasets._comparable(spec, after)
                        except HTTPException:
                            fail("MANUAL_EXCEL_FIELD_TYPE", "补充值不符合目标列的日期类型。", 422,
                                 cell=new_sheet.cell(row, column).coordinate)
                    if len(changes) >= 1000:
                        fail("MANUAL_EXCEL_TOO_MANY_CHANGES", "一次最多补充 1000 个现有数据格。", 422)
                    changes.append({"record_id": record["record_id"], "row_number": row,
                                    "field_key": spec["key"], "before": _json_value(before),
                                    "after": _json_value(after)})
            # 标题需与服务器冻结的字段完全一致；只读 schema 解析也会验证模板定位。
            sheet_name, actual_fields, _ = self.datasets._read_excel_schema(
                path, name, WorkbookLayout.model_validate(dataset.target_layout_json)
                if dataset.target_layout_json else None)
            if (sheet_name != name or [field.label for field in actual_fields]
                    != [field["label"] for field in dataset.schema_json]):
                fail("MANUAL_EXCEL_STRUCTURE_CHANGED", "目标表标题或字段已变化。", 422)
            return changes
        finally:
            baseline.close()
            candidate.close()
