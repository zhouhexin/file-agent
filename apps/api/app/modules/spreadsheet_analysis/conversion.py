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


class SpreadsheetConversionError(RuntimeError):
    """表格格式转换失败时抛出的结构化异常。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码，便于 Tool 返回可审计的失败原因。"""

        super().__init__(message)
        self.code = code
        self.message = message


def convert_xls_to_xlsx(*, source_path: Path, output_dir: Path, timeout_seconds: int = 60) -> Path:
    """使用 LibreOffice headless 将 `.xls` 转为 `.xlsx`，并返回生成文件路径。"""

    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise SpreadsheetConversionError(
            "XLS_CONVERTER_NOT_AVAILABLE",
            "缺少可用的 LibreOffice 转换器，无法读取旧版 xls 文件。请在服务器安装 LibreOffice，或将文件另存为 xlsx 后重新上传。",
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = output_dir / "libreoffice-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            [
                converter,
                "--headless",
                "--nologo",
                "--nofirststartwizard",
                "--nodefault",
                "--nolockcheck",
                f"-env:UserInstallation=file://{profile_dir}",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(output_dir),
                str(source_path),
            ],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SpreadsheetConversionError(
            "XLS_CONVERSION_FAILED",
            f"LibreOffice 转换 xls 失败：{exc}",
        ) from exc

    if result.returncode != 0:
        error_message = result.stderr.decode("utf-8", errors="ignore").strip()
        raise SpreadsheetConversionError(
            "XLS_CONVERSION_FAILED",
            f"LibreOffice 转换 xls 失败：{error_message or '未知错误'}",
        )

    converted_files = sorted(output_dir.glob("*.xlsx"))
    if not converted_files:
        raise SpreadsheetConversionError(
            "XLS_CONVERSION_OUTPUT_MISSING",
            "LibreOffice 未生成 xlsx 转换结果。",
        )
    return converted_files[0]


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
