"""结构化抽取 CSV/XLSX 派生件生成与安全下载投影。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from app.core.config import Settings, get_settings
from app.db.models import DocumentArtifact, DocumentVersion, StructuredExtractionRun
from app.modules.files.artifact_repository import DocumentArtifactRepository
from app.modules.structured_extraction.schemas import StructuredExtractionResult


class StructuredExtractionExportService:
    """只把规范化 JSON 投影成派生文件，不读取或改写原件。"""

    def __init__(self, *, db: Any, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self.repository = DocumentArtifactRepository(db)

    def ensure_export(
        self,
        *,
        run: StructuredExtractionRun,
        result: StructuredExtractionResult,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """按运行配置幂等创建 CSV 或 XLSX，并返回不含本地路径的投影。"""

        presentation = str(run.presentation or "AUTO").upper()
        if presentation not in {"CSV", "XLSX"}:
            return None
        version = self.db.get(DocumentVersion, run.document_version_id)
        if version is None:
            raise RuntimeError("结构化导出缺少文档版本。")
        artifact_type = f"STRUCTURED_EXTRACTION_{presentation}"
        config_hash = _export_config_hash(run=run, presentation=presentation)
        existing = self.repository.get_for_document(
            document_id=run.document_id,
            artifact_type=artifact_type,
            source_sha256=version.sha256,
            converter_config_hash=config_hash,
        )
        if not force and existing is not None and self._artifact_path(existing).is_file():
            return _artifact_projection(existing, presentation=presentation, reused=True)

        suffix = ".csv" if presentation == "CSV" else ".xlsx"
        relative_dir = Path("derivatives") / "structured-extraction" / run.document_id
        storage_root = Path(self.settings.file_storage_root).resolve()
        target_dir = (storage_root / relative_dir).resolve()
        if not _is_relative_to(target_dir, storage_root):
            raise RuntimeError("结构化导出目录越界。")
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{run.id}{suffix}"
        rows = _tabular_rows(result)
        headers = [
            _safe_spreadsheet_text(str(item.get("label") or item.get("key") or ""))
            for item in result.field_schema
        ]
        if presentation == "CSV":
            _write_csv_atomic(target_path=target_path, headers=headers, rows=rows)
            content_type = "text/csv; charset=utf-8"
        else:
            _write_xlsx_atomic(target_path=target_path, headers=headers, rows=rows)
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        digest = hashlib.sha256(target_path.read_bytes()).hexdigest()
        artifact = self.repository.upsert_link(
            document_id=run.document_id,
            artifact_type=artifact_type,
            storage_path=(relative_dir / target_path.name).as_posix(),
            content_type=content_type,
            size_bytes=target_path.stat().st_size,
            sha256=digest,
            source_sha256=version.sha256,
            converter_name="structured-extraction-export",
            converter_version="1",
            converter_config_hash=config_hash,
        )
        return _artifact_projection(artifact, presentation=presentation, reused=False)

    def get_existing(self, *, run: StructuredExtractionRun) -> dict[str, Any] | None:
        """从持久化派生件恢复刷新后的下载信息。"""

        presentation = str(run.presentation or "AUTO").upper()
        if presentation not in {"CSV", "XLSX"}:
            return None
        version = self.db.get(DocumentVersion, run.document_version_id)
        if version is None:
            return None
        artifact = self.repository.get_for_document(
            document_id=run.document_id,
            artifact_type=f"STRUCTURED_EXTRACTION_{presentation}",
            source_sha256=version.sha256,
            converter_config_hash=_export_config_hash(run=run, presentation=presentation),
        )
        if artifact is None or not self._artifact_path(artifact).is_file():
            return None
        return _artifact_projection(artifact, presentation=presentation, reused=True)

    def _artifact_path(self, artifact: DocumentArtifact) -> Path:
        storage_root = Path(self.settings.file_storage_root).resolve()
        path = (storage_root / artifact.storage_path).resolve()
        if not _is_relative_to(path, storage_root):
            raise RuntimeError("结构化导出文件路径越界。")
        return path


def _tabular_rows(result: StructuredExtractionResult) -> list[list[Any]]:
    keys = [str(item.get("key") or "") for item in result.field_schema]
    rows: list[list[Any]] = []
    for record in result.records:
        fields = dict(record.get("fields") or {})
        rows.append([_cell_value((fields.get(key) or {}).get("normalized_value")) for key in keys])
    return rows


def _cell_value(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_spreadsheet_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _safe_spreadsheet_text(value: str) -> str:
    """阻止用户字段和识别文本在 CSV/XLSX 中被解释为公式。"""

    return f"'{value}" if re.match(r"^[\t\r ]*[=+\-@]", value) else value


def _write_csv_atomic(*, target_path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix="structured-", suffix=".csv", dir=target_path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        os.replace(temporary, target_path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_xlsx_atomic(*, target_path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix="structured-", suffix=".xlsx", dir=target_path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet(title="结构化抽取")
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(temporary)
        os.replace(temporary, target_path)
    finally:
        temporary.unlink(missing_ok=True)


def _export_config_hash(*, run: StructuredExtractionRun, presentation: str) -> str:
    payload = {
        "schema_fingerprint": run.schema_fingerprint,
        "prompt_version": run.prompt_version,
        "presentation": presentation,
        "export_version": "1",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _artifact_projection(
    artifact: DocumentArtifact,
    *,
    presentation: str,
    reused: bool,
) -> dict[str, Any]:
    suffix = ".csv" if presentation == "CSV" else ".xlsx"
    return {
        "artifact_id": artifact.id,
        "format": presentation,
        "filename": f"structured-extraction-{artifact.document_id}{suffix}",
        "content_type": artifact.content_type,
        "size_bytes": artifact.size_bytes,
        "download_url": f"/api/files/{artifact.document_id}/artifacts/{artifact.id}",
        "reused": reused,
    }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
