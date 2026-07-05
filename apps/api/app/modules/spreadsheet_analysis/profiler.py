import openpyxl
from pathlib import Path

from .schemas import ColumnProfile, ColumnType, SheetProfile, WorkbookProfile


def profile_workbook(
    *,
    document_id: str,
    filename: str,
    file_path: Path,
) -> WorkbookProfile:
    workbook = openpyxl.load_workbook(
        file_path,
        read_only=True,
        data_only=True,
    )

    sheets: list[SheetProfile] = []

    for sheet_index, worksheet in enumerate(workbook.worksheets, start=1):
        header_row = detect_header_row(worksheet)
        headers = read_headers(worksheet, header_row)
        columns = build_column_profiles(
            worksheet=worksheet,
            sheet_id=f"sheet_{sheet_index}",
            header_row=header_row,
            headers=headers,
        )

        sheets.append(
            SheetProfile(
                sheet_id=f"sheet_{sheet_index}",
                sheet_name=worksheet.title,
                header_row=header_row,
                row_count=max(0, worksheet.max_row - header_row),
                columns=columns,
            )
        )

    workbook.close()

    return WorkbookProfile(
        document_id=document_id,
        filename=filename,
        sheets=sheets,
    )