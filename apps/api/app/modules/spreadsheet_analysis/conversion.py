"""旧版 Excel 转换适配器。

本模块只负责把受控存储中的 `.xls` 原件转换为临时或派生 `.xlsx` 文件，
不会覆盖原始文件，也不会执行宏或外部链接。调用方必须保证传入路径已经过
StorageService 或 Tool handler 权限校验。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterator
import zipfile

from app.core.config import get_settings
from app.modules.files.office_conversion import (
    libreoffice_profile_uri,
    resolve_libreoffice_executable,
    run_libreoffice_command,
)


class SpreadsheetConversionError(RuntimeError):
    """表格格式转换失败时抛出的结构化异常。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码，便于 Tool 返回可审计的失败原因。"""

        super().__init__(message)
        self.code = code
        self.message = message


def convert_xls_to_xlsx(*, source_path: Path, output_dir: Path, timeout_seconds: int | None = None) -> Path:
    """在隔离输入、输出和 profile 中把 XLS 转为经过校验的临时 XLSX。"""

    source_path = source_path.resolve()
    if source_path.suffix.lower() != ".xls" or not source_path.is_file():
        raise SpreadsheetConversionError(
            "XLS_CONVERSION_UNSUPPORTED_SOURCE",
            "转换服务只接受存在的旧版 xls 文件。",
        )
    settings = get_settings()
    resolved_timeout_seconds = timeout_seconds or settings.legacy_office_conversion_timeout_seconds
    converter = resolve_libreoffice_executable(configured=settings.libreoffice_executable)
    if converter is None:
        raise SpreadsheetConversionError(
            "XLS_CONVERTER_NOT_AVAILABLE",
            "缺少可用的 LibreOffice 转换器，无法读取旧版 xls 文件。请在服务器安装 LibreOffice，或将文件另存为 xlsx 后重新上传。",
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    input_dir = output_dir / "input"
    converted_dir = output_dir / "output"
    profile_dir = output_dir / "profile"
    input_dir.mkdir(parents=True, exist_ok=True)
    converted_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    isolated_source = input_dir / "source.xls"
    shutil.copy2(source_path, isolated_source)

    try:
        result = run_libreoffice_command(
            [
                str(converter),
                "--headless",
                "--nologo",
                "--nofirststartwizard",
                "--nodefault",
                "--nolockcheck",
                f"-env:UserInstallation={libreoffice_profile_uri(profile_dir)}",
                "--convert-to",
                "xlsx:Calc MS Excel 2007 XML",
                "--outdir",
                str(converted_dir),
                str(isolated_source),
            ],
            timeout_seconds=resolved_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise SpreadsheetConversionError(
            "XLS_CONVERSION_TIMEOUT",
            "LibreOffice 转换 xls 超时。",
        ) from exc
    except OSError as exc:
        raise SpreadsheetConversionError(
            "XLS_CONVERSION_FAILED",
            f"无法启动 LibreOffice：{exc}",
        ) from exc

    if result.returncode != 0:
        error_message = result.stderr.decode("utf-8", errors="ignore").strip()
        raise SpreadsheetConversionError(
            "XLS_CONVERSION_FAILED",
            f"LibreOffice 转换 xls 失败：{error_message or '未知错误'}",
        )

    converted_path = converted_dir / "source.xlsx"
    if not converted_path.is_file():
        raise SpreadsheetConversionError(
            "XLS_CONVERSION_OUTPUT_MISSING",
            "LibreOffice 未生成 xlsx 转换结果。",
        )
    _validate_xlsx(converted_path)
    return converted_path


def _validate_xlsx(path: Path) -> None:
    """校验 OOXML 必要结构并确认 openpyxl 能够只读打开。"""

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise SpreadsheetConversionError(
                    "XLSX_CONVERSION_OUTPUT_INVALID",
                    "LibreOffice 生成的 xlsx 结果无效。",
                )
        import openpyxl

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        workbook.close()
    except SpreadsheetConversionError:
        raise
    except Exception as exc:
        raise SpreadsheetConversionError(
            "XLSX_CONVERSION_OUTPUT_INVALID",
            "LibreOffice 生成的 xlsx 结果无效。",
        ) from exc


@contextmanager
def prepared_spreadsheet_path(*, file_path: Path) -> Iterator[Path]:
    """为表格读取准备可被 openpyxl 处理的路径；`.xls` 会转换到临时 `.xlsx`。"""

    if file_path.suffix.lower() != ".xls":
        yield file_path
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        converted_path = convert_xls_to_xlsx(
            source_path=file_path,
            output_dir=Path(temp_dir),
        )
        yield converted_path
