"""旧版 Office 文件的跨平台转换和派生件复用。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePath
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping
import zipfile

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import format_exception_traceback, log_event
from app.db.models import Document, DocumentArtifact, DocumentVersion
from app.modules.files.artifact_repository import DocumentArtifactRepository


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CONVERTED_DOCX_ARTIFACT_TYPE = "CONVERTED_DOCX"
CONVERTED_XLSX_ARTIFACT_TYPE = "CONVERTED_XLSX"
DOC_CONVERSION_RULE_VERSION = "legacy-doc-to-docx-v2"
XLS_CONVERSION_RULE_VERSION = "legacy-xls-to-xlsx-v1"
# 兼容既有导入方；新代码应使用格式规格中的 rule_version。
CONVERSION_RULE_VERSION = DOC_CONVERSION_RULE_VERSION


class OfficeConversionError(RuntimeError):
    """携带稳定错误码的 Office 转换异常。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        """保存错误码、用户可读信息和重试属性。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ConvertedDocumentArtifact:
    """供后续解析器使用的旧版 Office 持久化派生件。"""

    artifact_id: str
    file_path: Path
    storage_path: str
    sha256: str
    source_sha256: str
    converter_name: str
    converter_version: str
    converter_config_hash: str
    artifact_type: str
    source_format: str
    parsed_format: str
    reused: bool


@dataclass(frozen=True)
class OfficeConversionSpec:
    """定义一种受控旧版 Office 转换的格式、校验和审计边界。"""

    source_suffix: str
    target_suffix: str
    artifact_type: str
    content_type: str
    export_filter: str
    rule_version: str
    validation: str


DOC_TO_DOCX = OfficeConversionSpec(
    source_suffix=".doc",
    target_suffix=".docx",
    artifact_type=CONVERTED_DOCX_ARTIFACT_TYPE,
    content_type=DOCX_CONTENT_TYPE,
    export_filter="docx:Office Open XML Text",
    rule_version=DOC_CONVERSION_RULE_VERSION,
    validation="ooxml+python-docx",
)
XLS_TO_XLSX = OfficeConversionSpec(
    source_suffix=".xls",
    target_suffix=".xlsx",
    artifact_type=CONVERTED_XLSX_ARTIFACT_TYPE,
    content_type=XLSX_CONTENT_TYPE,
    export_filter="xlsx:Calc MS Excel 2007 XML",
    rule_version=XLS_CONVERSION_RULE_VERSION,
    validation="ooxml+openpyxl",
)


CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class OfficeDerivativeService:
    """统一创建、校验和复用 DOCX/XLSX 版本级持久化派生件。"""

    def __init__(
        self,
        *,
        db: Session,
        storage_root: Path | None = None,
        executable: Path | None = None,
        command_runner: CommandRunner | None = None,
        converter_version: str | None = None,
    ) -> None:
        """注入数据库、存储目录和可替换命令执行器。"""

        settings = get_settings()
        self.db = db
        self.repository = DocumentArtifactRepository(db)
        self.storage_root = (storage_root or Path(settings.file_storage_root)).resolve()
        self.executable = executable or resolve_libreoffice_executable(
            configured=settings.libreoffice_executable,
        )
        self.command_runner = command_runner or run_libreoffice_command
        self.timeout_seconds = settings.legacy_office_conversion_timeout_seconds
        self.max_file_size_bytes = settings.legacy_office_max_file_size_mb * 1024 * 1024
        self.derivative_dir = _validated_derivative_dir(settings.legacy_office_derivative_dir)
        self.converter_version = converter_version or libreoffice_runtime_version(self.executable)

    def get_or_create(
        self,
        *,
        document: Document,
        document_version: DocumentVersion | None,
        source_path: Path,
        spec: OfficeConversionSpec,
        force_reconvert: bool = False,
    ) -> ConvertedDocumentArtifact:
        """返回有效派生件；新链路必须传入与原件哈希一致的 DocumentVersion。"""

        settings = get_settings()
        started = time.perf_counter()
        if not settings.legacy_office_conversion_enabled:
            raise OfficeConversionError(
                _format_error_code(spec, "CONVERSION_DISABLED"),
                f"旧版 {spec.source_suffix.lstrip('.').upper()} 转换能力已关闭。",
            )
        source_path = source_path.resolve()
        if source_path.suffix.lower() != spec.source_suffix:
            raise OfficeConversionError(
                _format_error_code(spec, "CONVERSION_UNSUPPORTED_SOURCE"),
                f"转换服务只接受旧版 {spec.source_suffix.lstrip('.').upper()} 文件。",
            )
        if not source_path.is_file():
            raise OfficeConversionError("FILE_NOT_FOUND_ON_DISK", "原始 Office 文件不存在。")
        if source_path.stat().st_size > self.max_file_size_bytes:
            raise OfficeConversionError(
                _format_error_code(spec, "CONVERSION_FILE_TOO_LARGE"),
                f"{spec.source_suffix.lstrip('.').upper()} 文件超过当前允许的转换大小。",
            )
        source_sha256 = _file_sha256(source_path)
        resolved_version = document_version or self._resolve_legacy_version(
            document=document,
            source_sha256=source_sha256,
        )
        expected_sha256 = resolved_version.sha256 if resolved_version is not None else document.sha256
        if resolved_version is not None and resolved_version.document_id != document.id:
            raise OfficeConversionError("SOURCE_HASH_MISMATCH", "内容版本不属于当前文档。")
        if source_sha256 != expected_sha256:
            raise OfficeConversionError("SOURCE_HASH_MISMATCH", "Office 文件内容已变化，请重新登记文件版本。")

        config_hash = office_converter_config_hash(
            spec=spec,
            converter_name=settings.legacy_office_converter,
            converter_version=self.converter_version,
        )
        if not force_reconvert:
            current = (
                self.repository.get_for_version(
                    document_version_id=resolved_version.id,
                    artifact_type=spec.artifact_type,
                    source_sha256=source_sha256,
                    converter_config_hash=config_hash,
                )
                if resolved_version is not None
                else self.repository.get_for_document(
                    document_id=document.id,
                    artifact_type=spec.artifact_type,
                    source_sha256=source_sha256,
                    converter_config_hash=config_hash,
                )
            )
            reused = self._reuse_record(
                current,
                document=document,
                document_version=resolved_version,
                spec=spec,
            )
            if reused is not None:
                self._log_reused(reused, document=document, document_version=resolved_version, started=started)
                return reused

            # LibreOffice 暂时不可用时无法重新探测运行时版本。此时只允许复用
            # 当前规则版本创建且通过完整物理校验的历史派生件，不能把旧规则产物
            # 当成当前转换配置的等价结果。
            if self.executable is None:
                fallback = (
                    self.repository.get_latest_for_version_source(
                        document_version_id=resolved_version.id,
                        artifact_type=spec.artifact_type,
                        source_sha256=source_sha256,
                    )
                    if resolved_version is not None
                    else self.repository.get_latest_reusable_source_artifact(
                        artifact_type=spec.artifact_type,
                        source_sha256=source_sha256,
                    )
                )
                reused = self._reuse_record(
                    fallback,
                    document=document,
                    document_version=resolved_version,
                    spec=spec,
                    create_link=bool(
                        fallback is not None
                        and resolved_version is not None
                        and fallback.document_version_id != resolved_version.id
                    ),
                    require_current_rule=True,
                )
                if reused is not None:
                    self._log_reused(
                        reused,
                        document=document,
                        document_version=resolved_version,
                        started=started,
                    )
                    return reused
                if resolved_version is not None:
                    shared_fallback = self.repository.get_latest_reusable_source_artifact(
                        artifact_type=spec.artifact_type,
                        source_sha256=source_sha256,
                    )
                    reused = self._reuse_record(
                        shared_fallback,
                        document=document,
                        document_version=resolved_version,
                        spec=spec,
                        create_link=True,
                        require_current_rule=True,
                    )
                    if reused is not None:
                        self._log_reused(
                            reused,
                            document=document,
                            document_version=resolved_version,
                            started=started,
                        )
                        return reused

            shared = self.repository.get_reusable_physical_artifact(
                artifact_type=spec.artifact_type,
                source_sha256=source_sha256,
                converter_config_hash=config_hash,
            )
            reused = self._reuse_record(
                shared,
                document=document,
                document_version=resolved_version,
                spec=spec,
                create_link=True,
            )
            if reused is not None:
                self._log_reused(reused, document=document, document_version=resolved_version, started=started)
                return reused

        # 已有有效派生件可在 LibreOffice 暂时不可用时继续复用；只有确实需要转换时才报错。
        if self.executable is None:
            raise OfficeConversionError(
                "LIBREOFFICE_NOT_AVAILABLE",
                f"未找到 LibreOffice，无法生成 {spec.target_suffix.lstrip('.').upper()} 派生件。",
            )

        log_event(
            "file.derivative.convert.started",
            document_id=document.id,
            document_version_id=resolved_version.id if resolved_version else None,
            status="RUNNING",
            source_format=spec.source_suffix.lstrip("."),
            parsed_format=spec.target_suffix.lstrip("."),
            converter="libreoffice",
            converter_version=self.converter_version,
        )
        try:
            result = self._convert(
                document=document,
                document_version=resolved_version,
                source_path=source_path,
                source_sha256=source_sha256,
                config_hash=config_hash,
                spec=spec,
                force_reconvert=force_reconvert,
            )
        except OfficeConversionError as exc:
            log_event(
                "file.derivative.convert.failed",
                level="ERROR",
                document_id=document.id,
                document_version_id=resolved_version.id if resolved_version else None,
                status="FAILED",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code=exc.code,
                message=exc.message,
                source_format=spec.source_suffix.lstrip("."),
                parsed_format=spec.target_suffix.lstrip("."),
                converter="libreoffice",
                converter_version=self.converter_version,
                exception_traceback=format_exception_traceback(exc),
            )
            raise
        log_event(
            "file.derivative.convert.completed",
            document_id=document.id,
            document_version_id=resolved_version.id if resolved_version else None,
            status="COMPLETED",
            duration_ms=int((time.perf_counter() - started) * 1000),
            artifact_id=result.artifact_id,
            source_format=spec.source_suffix.lstrip("."),
            parsed_format=spec.target_suffix.lstrip("."),
            converter="libreoffice",
            converter_version=self.converter_version,
        )
        return result

    def _convert(
        self,
        *,
        document: Document,
        document_version: DocumentVersion | None,
        source_path: Path,
        source_sha256: str,
        config_hash: str,
        spec: OfficeConversionSpec,
        force_reconvert: bool,
    ) -> ConvertedDocumentArtifact:
        """在隔离目录完成转换、校验和原子落盘。"""

        assert self.executable is not None
        with tempfile.TemporaryDirectory(prefix="file-agent-office-") as temp_dir_value:
            temp_dir = Path(temp_dir_value)
            input_dir = temp_dir / "input"
            output_dir = temp_dir / "output"
            profile_dir = temp_dir / "profile"
            input_dir.mkdir()
            output_dir.mkdir()
            profile_dir.mkdir()
            temp_source = input_dir / f"source{spec.source_suffix}"
            shutil.copy2(source_path, temp_source)
            command = [
                str(self.executable),
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--nofirststartwizard",
                f"-env:UserInstallation={libreoffice_profile_uri(profile_dir)}",
                "--convert-to",
                spec.export_filter,
                "--outdir",
                str(output_dir),
                str(temp_source),
            ]
            try:
                completed = self.command_runner(command, timeout_seconds=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise OfficeConversionError(
                    _format_error_code(spec, "CONVERSION_TIMEOUT"),
                    f"LibreOffice 转换 {spec.source_suffix.lstrip('.').upper()} 超时。",
                    retryable=True,
                ) from exc
            except OSError as exc:
                raise OfficeConversionError(
                    _format_error_code(spec, "CONVERSION_FAILED"),
                    f"无法启动 LibreOffice：{exc}",
                    retryable=True,
                ) from exc
            if completed.returncode != 0:
                error_message = (completed.stderr or b"").decode("utf-8", errors="ignore").strip()
                raise OfficeConversionError(
                    _format_error_code(spec, "CONVERSION_FAILED"),
                    f"LibreOffice 转换 {spec.source_suffix.lstrip('.').upper()} 失败：{error_message or '未知错误'}",
                    retryable=True,
                )
            output_path = output_dir / f"source{spec.target_suffix}"
            if not output_path.is_file():
                raise OfficeConversionError(
                    _output_error_code(spec, "MISSING"),
                    f"LibreOffice 未生成 {spec.target_suffix.lstrip('.').upper()} 转换结果。",
                )
            _validate_office_output(output_path, spec=spec)
            relative_path = (
                self.derivative_dir
                / source_sha256[:2]
                / source_sha256
                / f"{config_hash}{spec.target_suffix}"
            )
            final_path = (self.storage_root / relative_path).resolve()
            if not _is_relative_to(final_path, self.storage_root):
                raise OfficeConversionError("DERIVATIVE_WRITE_FAILED", "派生件存储路径越界。")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if final_path.exists() and not force_reconvert:
                try:
                    _validate_office_output(final_path, spec=spec)
                except OfficeConversionError:
                    _replace_with_retry(output_path, final_path)
            elif force_reconvert or not final_path.exists():
                _replace_with_retry(output_path, final_path)
            _validate_office_output(final_path, spec=spec)
            persisted_sha256 = _file_sha256(final_path)
            storage_path = relative_path.as_posix()
            self.repository.update_physical_facts(
                storage_path=storage_path,
                size_bytes=final_path.stat().st_size,
                sha256=persisted_sha256,
            )
            metadata = _artifact_metadata(spec)
            if document_version is not None:
                artifact = self.repository.upsert_version_link(
                    document_id=document.id,
                    document_version_id=document_version.id,
                    artifact_type=spec.artifact_type,
                    storage_path=storage_path,
                    content_type=spec.content_type,
                    size_bytes=final_path.stat().st_size,
                    sha256=persisted_sha256,
                    source_sha256=source_sha256,
                    converter_name="libreoffice",
                    converter_version=self.converter_version,
                    converter_config_hash=config_hash,
                    metadata_json=metadata,
                )
            else:
                artifact = self.repository.upsert_link(
                    document_id=document.id,
                    artifact_type=spec.artifact_type,
                    storage_path=storage_path,
                    content_type=spec.content_type,
                    size_bytes=final_path.stat().st_size,
                    sha256=persisted_sha256,
                    source_sha256=source_sha256,
                    converter_name="libreoffice",
                    converter_version=self.converter_version,
                    converter_config_hash=config_hash,
                    metadata_json=metadata,
                )
            return _artifact_result(artifact=artifact, file_path=final_path, spec=spec, reused=False)

    def _reuse_record(
        self,
        artifact: DocumentArtifact | None,
        *,
        document: Document,
        document_version: DocumentVersion | None,
        spec: OfficeConversionSpec,
        create_link: bool = False,
        require_current_rule: bool = False,
    ) -> ConvertedDocumentArtifact | None:
        """校验记录和物理文件，必要时为当前 Document 建立链接。"""

        if artifact is None or artifact.storage_backend != "local":
            return None
        metadata = dict(artifact.metadata_json or {})
        if require_current_rule and metadata.get("conversion_rule_version") != spec.rule_version:
            return None
        file_path = (self.storage_root / artifact.storage_path).resolve()
        if not _is_relative_to(file_path, self.storage_root) or not file_path.is_file():
            return None
        if file_path.stat().st_size != artifact.size_bytes or _file_sha256(file_path) != artifact.sha256:
            return None
        try:
            _validate_office_output(file_path, spec=spec)
        except OfficeConversionError:
            return None
        if create_link:
            if document_version is not None and artifact.document_version_id != document_version.id:
                artifact = self.repository.upsert_version_link(
                    document_id=document.id,
                    document_version_id=document_version.id,
                    artifact_type=artifact.artifact_type,
                    storage_path=artifact.storage_path,
                    content_type=artifact.content_type,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                    source_sha256=artifact.source_sha256,
                    converter_name=artifact.converter_name,
                    converter_version=artifact.converter_version,
                    converter_config_hash=artifact.converter_config_hash,
                    metadata_json=dict(artifact.metadata_json or _artifact_metadata(spec)),
                )
            elif document_version is None and artifact.document_id != document.id:
                artifact = self.repository.upsert_link(
                    document_id=document.id,
                    artifact_type=artifact.artifact_type,
                    storage_path=artifact.storage_path,
                    content_type=artifact.content_type,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                    source_sha256=artifact.source_sha256,
                    converter_name=artifact.converter_name,
                    converter_version=artifact.converter_version,
                    converter_config_hash=artifact.converter_config_hash,
                    metadata_json=dict(artifact.metadata_json or _artifact_metadata(spec)),
                )
        return _artifact_result(artifact=artifact, file_path=file_path, spec=spec, reused=True)

    def reusable_converter_config_hash(
        self,
        *,
        document: Document,
        document_version: DocumentVersion | None,
        spec: OfficeConversionSpec,
    ) -> str | None:
        """在运行时不可探测时返回当前规则历史派生件的真实转换指纹。"""

        if self.executable is not None:
            return office_converter_config_hash(
                spec=spec,
                converter_name=get_settings().legacy_office_converter,
                converter_version=self.converter_version,
            )
        source_sha256 = document_version.sha256 if document_version is not None else document.sha256
        artifact = (
            self.repository.get_latest_for_version_source(
                document_version_id=document_version.id,
                artifact_type=spec.artifact_type,
                source_sha256=source_sha256,
            )
            if document_version is not None
            else self.repository.get_latest_reusable_source_artifact(
                artifact_type=spec.artifact_type,
                source_sha256=source_sha256,
            )
        )
        if artifact is None and document_version is not None:
            artifact = self.repository.get_latest_reusable_source_artifact(
                artifact_type=spec.artifact_type,
                source_sha256=source_sha256,
            )
        if artifact is None:
            return None
        metadata = dict(artifact.metadata_json or {})
        if metadata.get("conversion_rule_version") != spec.rule_version:
            return None
        return artifact.converter_config_hash

    @staticmethod
    def _log_reused(
        result: ConvertedDocumentArtifact,
        *,
        document: Document,
        document_version: DocumentVersion | None,
        started: float,
    ) -> None:
        """记录不包含服务器绝对路径的派生件复用日志。"""

        log_event(
            "file.derivative.convert.reused",
            document_id=document.id,
            document_version_id=document_version.id if document_version else None,
            status="REUSED",
            duration_ms=int((time.perf_counter() - started) * 1000),
            artifact_id=result.artifact_id,
            source_format=result.source_format,
            parsed_format=result.parsed_format,
            converter=result.converter_name,
            converter_version=result.converter_version,
        )

    def _resolve_legacy_version(
        self,
        *,
        document: Document,
        source_sha256: str,
    ) -> DocumentVersion | None:
        """兼容旧 DOC 调用；只在版本哈希能够唯一确认时自动绑定。"""

        versions = (
            self.db.query(DocumentVersion)
            .filter(
                DocumentVersion.document_id == document.id,
                DocumentVersion.sha256 == source_sha256,
            )
            .all()
        )
        return versions[0] if len(versions) == 1 else None


class LegacyOfficeConversionService(OfficeDerivativeService):
    """保留旧类名，同时把 DOC/XLS 都委托给统一版本级派生件服务。"""

    def get_or_create_docx(
        self,
        *,
        document: Document,
        source_path: Path,
        document_version: DocumentVersion | None = None,
        force_reconvert: bool = False,
    ) -> ConvertedDocumentArtifact:
        """返回持久化 DOCX 派生件。"""

        return self.get_or_create(
            document=document,
            document_version=document_version,
            source_path=source_path,
            spec=DOC_TO_DOCX,
            force_reconvert=force_reconvert,
        )

    def get_or_create_xlsx(
        self,
        *,
        document: Document,
        document_version: DocumentVersion,
        source_path: Path,
        force_reconvert: bool = False,
    ) -> ConvertedDocumentArtifact:
        """返回强制绑定内容版本的持久化 XLSX 派生件。"""

        return self.get_or_create(
            document=document,
            document_version=document_version,
            source_path=source_path,
            spec=XLS_TO_XLSX,
            force_reconvert=force_reconvert,
        )


def resolve_libreoffice_executable(
    *,
    configured: str = "",
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """按显式配置、PATH 和平台默认目录查找 LibreOffice。"""

    platform_name = platform_name or sys.platform
    environ = environ or os.environ
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            # Windows 的 soffice.exe 是 GUI 启动器，可能在真正转换完成前退出；
            # 同目录 soffice.com 才是可等待并可读取退出码的控制台入口。部署配置
            # 即使写了常见的 exe 路径，也应保留其目录授权并优先切换到 com。
            if (
                platform_name.startswith("win")
                and configured_path.name.casefold() == "soffice.exe"
            ):
                console_path = configured_path.with_suffix(".com")
                if console_path.is_file():
                    return console_path
            return configured_path
        located = which(configured)
        if located:
            return Path(located)

    command_names = ["soffice.com", "soffice.exe", "soffice"] if platform_name.startswith("win") else ["soffice", "libreoffice"]
    for command_name in command_names:
        located = which(command_name)
        if located:
            return Path(located)

    candidates: list[Path] = []
    if platform_name == "darwin":
        candidates.append(Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"))
    elif platform_name.startswith("win"):
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            base = environ.get(env_name)
            if base:
                candidates.extend(
                    [
                        Path(base) / "LibreOffice" / "program" / "soffice.com",
                        Path(base) / "LibreOffice" / "program" / "soffice.exe",
                    ]
                )
    else:
        candidates.extend(
            [
                Path("/usr/bin/soffice"),
                Path("/usr/bin/libreoffice"),
                Path("/opt/libreoffice/program/soffice"),
            ]
        )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def libreoffice_profile_uri(path: PurePath) -> str:
    """把本地 profile 路径转换为 LibreOffice 接受的 file URI。"""

    if path.is_absolute():
        return path.as_uri()
    return Path(path).resolve().as_uri()


def libreoffice_runtime_version(executable: Path | None) -> str:
    """读取 LibreOffice 版本，失败时返回稳定占位。"""

    if executable is None:
        return "unavailable"
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    output = (result.stdout or result.stderr or b"").decode("utf-8", errors="ignore").strip()
    return output[:120] or "unknown"


def office_converter_config_hash(
    *,
    spec: OfficeConversionSpec,
    converter_name: str,
    converter_version: str,
) -> str:
    """按源格式、目标格式、过滤器、校验器和运行时版本生成稳定复用指纹。"""

    identity = "|".join(
        [
            spec.rule_version,
            f"converter={converter_name}",
            f"version={converter_version}",
            f"source={spec.source_suffix}",
            f"target={spec.target_suffix}",
            f"output={spec.export_filter}",
            f"validation={spec.validation}",
            "arguments-schema=office-headless-v1",
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def legacy_office_converter_config_hash(*, converter_name: str, converter_version: str) -> str:
    """兼容既有 DOC 调用方的转换指纹入口。"""

    return office_converter_config_hash(
        spec=DOC_TO_DOCX,
        converter_name=converter_name,
        converter_version=converter_version,
    )


def run_libreoffice_command(
    command: list[str],
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    """在独立进程组中运行 LibreOffice，并在超时时清理进程树。"""

    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout_seconds, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout, stderr=stderr)


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """按平台终止 LibreOffice 进程组，避免超时后残留后台进程。"""

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=10,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _validate_docx(path: Path) -> None:
    """校验 OOXML 必要结构，并确认 python-docx 可以打开。"""

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise OfficeConversionError("DOCX_OUTPUT_INVALID", "转换结果不是有效的 DOCX 文档。")
        from docx import Document as DocxDocument

        DocxDocument(path)
    except OfficeConversionError:
        raise
    except Exception as exc:
        raise OfficeConversionError("DOCX_OUTPUT_INVALID", "转换结果不是有效的 DOCX 文档。") from exc


def _validate_xlsx(path: Path) -> None:
    """校验 XLSX 必要结构，并确认 openpyxl 能够只读打开全部工作簿元数据。"""

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise OfficeConversionError(
                    "XLSX_CONVERSION_OUTPUT_INVALID",
                    "转换结果不是有效的 XLSX 工作簿。",
                )
        import openpyxl

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        workbook.close()
    except OfficeConversionError:
        raise
    except Exception as exc:
        raise OfficeConversionError(
            "XLSX_CONVERSION_OUTPUT_INVALID",
            "转换结果不是有效的 XLSX 工作簿。",
        ) from exc


def _validate_office_output(path: Path, *, spec: OfficeConversionSpec) -> None:
    """按受控格式规格选择确定性结构校验器。"""

    if spec.artifact_type == CONVERTED_DOCX_ARTIFACT_TYPE:
        _validate_docx(path)
        return
    if spec.artifact_type == CONVERTED_XLSX_ARTIFACT_TYPE:
        _validate_xlsx(path)
        return
    raise OfficeConversionError("OFFICE_OUTPUT_INVALID", "未知的 Office 派生件规格。")


def _file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256，避免大文件一次性进入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_with_retry(source: Path, target: Path) -> None:
    """在目标卷内暂存后原子提交，兼容 Windows 跨盘符和短暂文件占用。

    系统临时目录通常位于 C 盘，而 FILE_STORAGE_ROOT 可能位于其他盘符；
    `os.replace(source, target)` 无法跨卷执行。因此先把内容复制到目标同目录的
    `.part` 文件，再使用同卷 `os.replace` 原子提交，任何失败都清理残留暂存件。
    """

    last_error: OSError | None = None
    for attempt in range(3):
        temporary: Path | None = None
        temporary_fd: int | None = None
        try:
            # 不能把包含转换配置哈希的完整目标文件名再次拼进临时文件名；在
            # Windows 深层 storage 目录中，这会让原本合法的目标路径超过 MAX_PATH。
            # mkstemp 既保证同目录同卷，也用固定短前后缀避免路径长度随目标名增长。
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=".fa-",
                suffix=".part",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(temporary_fd, "wb") as target_handle:
                temporary_fd = None
                with source.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
            # temporary 与 target 位于同一目录，因此 Windows 上不会发生跨盘符移动。
            os.replace(temporary, target)
            source.unlink(missing_ok=True)
            return
        except OSError as exc:
            last_error = exc
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))
    raise OfficeConversionError("DERIVATIVE_WRITE_FAILED", "无法写入 Office 派生件。", retryable=True) from last_error


def _artifact_result(
    *,
    artifact: DocumentArtifact,
    file_path: Path,
    spec: OfficeConversionSpec,
    reused: bool,
) -> ConvertedDocumentArtifact:
    """把 ORM 派生件转换为不可变服务结果。"""

    return ConvertedDocumentArtifact(
        artifact_id=artifact.id,
        file_path=file_path,
        storage_path=artifact.storage_path,
        sha256=artifact.sha256,
        source_sha256=artifact.source_sha256,
        converter_name=artifact.converter_name,
        converter_version=artifact.converter_version,
        converter_config_hash=artifact.converter_config_hash,
        artifact_type=artifact.artifact_type,
        source_format=spec.source_suffix.lstrip("."),
        parsed_format=spec.target_suffix.lstrip("."),
        reused=reused,
    )


def _artifact_metadata(spec: OfficeConversionSpec) -> dict[str, str]:
    """生成不含正文和绝对路径的派生件审计元数据。"""

    return {
        "source_format": spec.source_suffix.lstrip("."),
        "parsed_format": spec.target_suffix.lstrip("."),
        "conversion_rule_version": spec.rule_version,
        "validation": spec.validation,
    }


def _format_error_code(spec: OfficeConversionSpec, suffix: str) -> str:
    """保留 DOC/XLS 既有细分错误码，避免破坏 Tool 和运维告警契约。"""

    return f"{spec.source_suffix.lstrip('.').upper()}_{suffix}"


def _output_error_code(spec: OfficeConversionSpec, kind: str) -> str:
    """返回与既有转换模块兼容的输出缺失或校验失败错误码。"""

    target = spec.target_suffix.lstrip(".").upper()
    if kind == "MISSING":
        return "DOCX_OUTPUT_MISSING" if target == "DOCX" else "XLS_CONVERSION_OUTPUT_MISSING"
    return f"{target}_CONVERSION_OUTPUT_INVALID"


def _validated_derivative_dir(value: str) -> Path:
    """限制派生目录为存储根下的安全相对路径。"""

    path = Path(value or "derivatives/office")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("LEGACY_OFFICE_DERIVATIVE_DIR 必须是安全相对路径。")
    return path


def _is_relative_to(path: Path, root: Path) -> bool:
    """判断目标路径是否位于存储根内。"""

    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
