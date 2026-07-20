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
from app.core.logging import log_event
from app.db.models import Document, DocumentArtifact
from app.modules.files.artifact_repository import DocumentArtifactRepository


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CONVERTED_DOCX_ARTIFACT_TYPE = "CONVERTED_DOCX"
CONVERSION_RULE_VERSION = "legacy-doc-to-docx-v1"


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
    """供后续解析器使用的 DOCX 派生件。"""

    artifact_id: str
    file_path: Path
    storage_path: str
    sha256: str
    source_sha256: str
    converter_name: str
    converter_version: str
    converter_config_hash: str
    reused: bool


CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class LegacyOfficeConversionService:
    """把旧版 DOC 转为可持久复用的 DOCX 派生件。"""

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

    def get_or_create_docx(
        self,
        *,
        document: Document,
        source_path: Path,
        force_reconvert: bool = False,
    ) -> ConvertedDocumentArtifact:
        """返回有效 DOCX 派生件，不存在时执行一次受控转换。"""

        settings = get_settings()
        started = time.perf_counter()
        if not settings.legacy_office_conversion_enabled:
            raise OfficeConversionError("DOC_CONVERSION_DISABLED", "旧版 Word 转换能力已关闭。")
        if self.executable is None:
            raise OfficeConversionError(
                "LIBREOFFICE_NOT_AVAILABLE",
                "未找到 LibreOffice，无法生成 DOCX 派生件。",
            )
        source_path = source_path.resolve()
        if source_path.suffix.lower() != ".doc":
            raise OfficeConversionError("DOC_CONVERSION_UNSUPPORTED_SOURCE", "转换服务只接受旧版 DOC 文件。")
        if not source_path.is_file():
            raise OfficeConversionError("FILE_NOT_FOUND_ON_DISK", "原始 DOC 文件不存在。")
        if source_path.stat().st_size > self.max_file_size_bytes:
            raise OfficeConversionError(
                "DOC_CONVERSION_FILE_TOO_LARGE",
                "DOC 文件超过当前允许的转换大小。",
            )
        source_sha256 = _file_sha256(source_path)
        if source_sha256 != document.sha256:
            raise OfficeConversionError("SOURCE_HASH_MISMATCH", "DOC 文件内容已变化，请重新登记文件版本。")

        config_hash = legacy_office_converter_config_hash(
            converter_name=settings.legacy_office_converter,
            converter_version=self.converter_version,
        )
        if not force_reconvert:
            current = self.repository.get_for_document(
                document_id=document.id,
                artifact_type=CONVERTED_DOCX_ARTIFACT_TYPE,
                source_sha256=source_sha256,
                converter_config_hash=config_hash,
            )
            reused = self._reuse_record(current, document=document)
            if reused is not None:
                self._log_reused(reused, document_id=document.id, started=started)
                return reused

            shared = self.repository.get_reusable_physical_artifact(
                artifact_type=CONVERTED_DOCX_ARTIFACT_TYPE,
                source_sha256=source_sha256,
                converter_config_hash=config_hash,
            )
            reused = self._reuse_record(shared, document=document, create_link=True)
            if reused is not None:
                self._log_reused(reused, document_id=document.id, started=started)
                return reused

        log_event(
            "file.derivative.convert.started",
            document_id=document.id,
            status="RUNNING",
            source_format="doc",
            parsed_format="docx",
            converter="libreoffice",
            converter_version=self.converter_version,
        )
        try:
            result = self._convert(
                document=document,
                source_path=source_path,
                source_sha256=source_sha256,
                config_hash=config_hash,
                force_reconvert=force_reconvert,
            )
        except OfficeConversionError as exc:
            log_event(
                "file.derivative.convert.failed",
                level="ERROR",
                document_id=document.id,
                status="FAILED",
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_code=exc.code,
                message=exc.message,
                source_format="doc",
                parsed_format="docx",
                converter="libreoffice",
                converter_version=self.converter_version,
            )
            raise
        log_event(
            "file.derivative.convert.completed",
            document_id=document.id,
            status="COMPLETED",
            duration_ms=int((time.perf_counter() - started) * 1000),
            artifact_id=result.artifact_id,
            source_format="doc",
            parsed_format="docx",
            converter="libreoffice",
            converter_version=self.converter_version,
        )
        return result

    def _convert(
        self,
        *,
        document: Document,
        source_path: Path,
        source_sha256: str,
        config_hash: str,
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
            temp_source = input_dir / "source.doc"
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
                "docx:Office Open XML Text",
                "--outdir",
                str(output_dir),
                str(temp_source),
            ]
            try:
                completed = self.command_runner(command, timeout_seconds=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise OfficeConversionError(
                    "DOC_CONVERSION_TIMEOUT",
                    "LibreOffice 转换 DOC 超时。",
                    retryable=True,
                ) from exc
            except OSError as exc:
                raise OfficeConversionError(
                    "DOC_CONVERSION_FAILED",
                    f"无法启动 LibreOffice：{exc}",
                    retryable=True,
                ) from exc
            if completed.returncode != 0:
                error_message = (completed.stderr or b"").decode("utf-8", errors="ignore").strip()
                raise OfficeConversionError(
                    "DOC_CONVERSION_FAILED",
                    f"LibreOffice 转换 DOC 失败：{error_message or '未知错误'}",
                    retryable=True,
                )
            output_path = output_dir / "source.docx"
            if not output_path.is_file():
                raise OfficeConversionError("DOCX_OUTPUT_MISSING", "LibreOffice 未生成 DOCX 转换结果。")
            _validate_docx(output_path)
            output_sha256 = _file_sha256(output_path)
            relative_path = (
                self.derivative_dir
                / source_sha256[:2]
                / source_sha256
                / f"{config_hash}.docx"
            )
            final_path = (self.storage_root / relative_path).resolve()
            if not _is_relative_to(final_path, self.storage_root):
                raise OfficeConversionError("DERIVATIVE_WRITE_FAILED", "派生件存储路径越界。")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if final_path.exists() and not force_reconvert:
                try:
                    _validate_docx(final_path)
                except OfficeConversionError:
                    final_path.unlink(missing_ok=True)
            elif final_path.exists():
                final_path.unlink(missing_ok=True)
            if not final_path.exists():
                _replace_with_retry(output_path, final_path)
            persisted_sha256 = _file_sha256(final_path)
            artifact = self.repository.upsert_link(
                document_id=document.id,
                artifact_type=CONVERTED_DOCX_ARTIFACT_TYPE,
                storage_path=relative_path.as_posix(),
                content_type=DOCX_CONTENT_TYPE,
                size_bytes=final_path.stat().st_size,
                sha256=persisted_sha256,
                source_sha256=source_sha256,
                converter_name="libreoffice",
                converter_version=self.converter_version,
                converter_config_hash=config_hash,
            )
            return _artifact_result(artifact=artifact, file_path=final_path, reused=False)

    def _reuse_record(
        self,
        artifact: DocumentArtifact | None,
        *,
        document: Document,
        create_link: bool = False,
    ) -> ConvertedDocumentArtifact | None:
        """校验记录和物理文件，必要时为当前 Document 建立链接。"""

        if artifact is None or artifact.storage_backend != "local":
            return None
        file_path = (self.storage_root / artifact.storage_path).resolve()
        if not _is_relative_to(file_path, self.storage_root) or not file_path.is_file():
            return None
        if file_path.stat().st_size != artifact.size_bytes or _file_sha256(file_path) != artifact.sha256:
            return None
        try:
            _validate_docx(file_path)
        except OfficeConversionError:
            return None
        if create_link and artifact.document_id != document.id:
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
            )
        return _artifact_result(artifact=artifact, file_path=file_path, reused=True)

    @staticmethod
    def _log_reused(result: ConvertedDocumentArtifact, *, document_id: str, started: float) -> None:
        """记录不包含服务器绝对路径的派生件复用日志。"""

        log_event(
            "file.derivative.convert.reused",
            document_id=document_id,
            status="REUSED",
            duration_ms=int((time.perf_counter() - started) * 1000),
            artifact_id=result.artifact_id,
            source_format="doc",
            parsed_format="docx",
            converter=result.converter_name,
            converter_version=result.converter_version,
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


def legacy_office_converter_config_hash(*, converter_name: str, converter_version: str) -> str:
    """生成决定派生件复用范围的稳定转换指纹。"""

    identity = "|".join(
        [
            CONVERSION_RULE_VERSION,
            f"converter={converter_name}",
            f"version={converter_version}",
            "output=docx:Office Open XML Text",
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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


def _file_sha256(path: Path) -> str:
    """流式计算文件 SHA-256，避免大文件一次性进入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_with_retry(source: Path, target: Path) -> None:
    """兼容 Windows 杀毒软件短暂占用，有限重试原子移动。"""

    last_error: OSError | None = None
    for attempt in range(3):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))
    raise OfficeConversionError("DERIVATIVE_WRITE_FAILED", "无法写入 DOCX 派生件。", retryable=True) from last_error


def _artifact_result(*, artifact: DocumentArtifact, file_path: Path, reused: bool) -> ConvertedDocumentArtifact:
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
        reused=reused,
    )


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
