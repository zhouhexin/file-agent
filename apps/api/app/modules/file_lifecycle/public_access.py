"""搜索结果公开预览与下载的只读能力链接。

链接不依赖登录会话，也不设置过期时间。服务端仍只接受签名令牌，并且每次打开时重新
校验工作副本仍处于活动状态，避免公开任意路径或回收站内容。
"""

from __future__ import annotations

from collections import defaultdict
from html import escape
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy.orm import Session

from app.core.security import (
    TokenDecodeError,
    create_public_file_access_token,
    decode_public_file_access_token,
)
from app.db.models import Document, DocumentVersion, WorkingCopy
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.files.extraction_repository import FileExtractionRepository


PUBLIC_FILE_ACCESS_PREFIX = "/api/public/file-access"
_BROWSER_INLINE_EXTENSIONS = {
    ".bmp",
    ".csv",
    ".jpeg",
    ".jpg",
    ".md",
    ".pdf",
    ".png",
    ".text",
    ".tif",
    ".tiff",
    ".txt",
    ".webp",
}
_SPREADSHEET_EXTENSIONS = {".xls", ".xlsx", ".xlsm"}


def public_file_links(working_copy_id: str) -> dict[str, str]:
    """生成可由浏览器直接打开的相对预览和下载地址。"""

    token = create_public_file_access_token(working_copy_id)
    base = f"{PUBLIC_FILE_ACCESS_PREFIX}/{token}"
    return {
        "preview_url": f"{base}/preview",
        "download_url": f"{base}/download",
    }


class PublicWorkingCopyAccessService:
    """解析永久只读能力令牌，并投影工作副本内容或预览页面。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def download_response(self, token: str) -> FileResponse:
        """返回公开下载响应；文件已回收或内容丢失时拒绝继续读取。"""

        copy, document, _version, path = self._resolve(token)
        return FileResponse(
            path=path,
            filename=copy.filename,
            media_type=document.content_type,
            content_disposition_type="attachment",
            headers={"Cache-Control": "no-store"},
        )

    def preview_response(self, token: str) -> Response:
        """返回浏览器内联文件或由持久化解析结果生成的只读 HTML 预览。"""

        copy, document, version, path = self._resolve(token)
        extension = Path(copy.filename).suffix.lower()
        if extension in _BROWSER_INLINE_EXTENSIONS or document.content_type.startswith("image/"):
            browser_content_type = mimetypes.guess_type(copy.filename)[0] or document.content_type
            return FileResponse(
                path=path,
                filename=copy.filename,
                media_type=browser_content_type,
                content_disposition_type="inline",
                headers={"Cache-Control": "no-store"},
            )

        extraction = FileExtractionRepository(
            self.db,
            document.user_id,
        ).get_latest_successful_extraction(
            document_id=document.id,
            document_version_id=version.id,
        )
        download_url = public_file_links(copy.id)["download_url"]
        html = self._render_preview_html(
            filename=copy.filename,
            extension=extension,
            extraction=extraction,
            download_url=download_url,
        )
        return HTMLResponse(
            content=html,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    def _resolve(
        self,
        token: str,
    ) -> tuple[WorkingCopy, Document, DocumentVersion, Path]:
        """把能力令牌解析为当前活动工作副本，且不接受客户端路径。"""

        try:
            payload = decode_public_file_access_token(token)
        except TokenDecodeError as exc:
            raise HTTPException(status_code=404, detail="公开文件链接无效") from exc
        copy = (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.id == payload["working_copy_id"],
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
            )
            .one_or_none()
        )
        if copy is None:
            raise HTTPException(status_code=404, detail="公开文件链接无效")
        if copy.status != "ACTIVE":
            raise HTTPException(status_code=410, detail="文件已删除或不再可用")
        document = self.db.get(Document, copy.document_id)
        version = self.db.get(DocumentVersion, copy.current_version_id) if copy.current_version_id else None
        if document is None or version is None:
            raise HTTPException(status_code=404, detail="工作副本内容不存在")
        path = FileLifecycleStorageService().working_copy_path(version.storage_path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="工作副本内容不存在")
        return copy, document, version, path

    @classmethod
    def _render_preview_html(
        cls,
        *,
        filename: str,
        extension: str,
        extraction: dict[str, Any] | None,
        download_url: str,
    ) -> str:
        """把可信数据库中的正文或单元格转为经过 HTML 转义的预览页面。"""

        safe_filename = escape(filename)
        body = (
            cls._spreadsheet_html(extraction)
            if extension in _SPREADSHEET_EXTENSIONS
            else cls._document_html(extraction)
        )
        if not body:
            body = (
                '<p class="empty">正文预览尚未生成或该格式不支持浏览器预览。'
                "你仍可以使用上方下载链接打开原文件。</p>"
            )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_filename}</title>
<style>
body {{ margin: 0; color: #172033; background: #f3f6fa; font-family: system-ui, "Microsoft YaHei", sans-serif; }}
main {{ max-width: 1100px; margin: 24px auto; padding: 0 18px 48px; }}
header, section {{ background: #fff; border: 1px solid #dfe5ec; border-radius: 10px;
padding: 18px; margin-bottom: 14px; }}
h1 {{ margin: 0 0 12px; font-size: 20px; overflow-wrap: anywhere; }}
h2 {{ margin: 0 0 10px; font-size: 16px; }}
a {{ color: #145bd7; }} pre {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.65; }}
table {{ border-collapse: collapse; width: 100%; display: block; overflow: auto; }}
th, td {{ border: 1px solid #dfe5ec; padding: 6px 8px; min-width: 80px; text-align: left; vertical-align: top; }}
.empty {{ color: #5d6778; }}
</style>
</head>
<body><main><header><h1>{safe_filename}</h1>
<a href="{escape(download_url, quote=True)}">下载原文件</a></header>{body}</main></body>
</html>"""

    @staticmethod
    def _document_html(extraction: dict[str, Any] | None) -> str:
        """按页或段落渲染 Word 等文档的持久化正文。"""

        if not extraction:
            return ""
        sections: list[str] = []
        remaining = 200_000
        for index, page in enumerate(extraction.get("pages") or [], start=1):
            text = str(page.text_content or "")
            if not text:
                continue
            visible = text[:remaining]
            label = f"第 {page.page_number} 页" if page.page_number else f"正文 {index}"
            sections.append(f"<section><h2>{escape(label)}</h2><pre>{escape(visible)}</pre></section>")
            remaining -= len(visible)
            if remaining <= 0:
                sections.append('<section class="empty">预览内容过长，已截断；可下载原文件查看完整内容。</section>')
                break
        return "".join(sections)

    @staticmethod
    def _spreadsheet_html(extraction: dict[str, Any] | None) -> str:
        """把 Excel 解析元素按 Sheet、行列组织为有限大小的只读表格。"""

        if not extraction:
            return ""
        sheets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for element in extraction.get("elements") or []:
            if element.label != "table_cell":
                continue
            metadata = dict(element.metadata_json or {})
            sheet_name = str(metadata.get("sheet_name") or "Sheet")
            sheets[sheet_name].append(
                {
                    "row": int(metadata.get("row") or 0),
                    "column": int(metadata.get("column") or 0),
                    "value": str(metadata.get("display_value") or element.text_content or ""),
                }
            )
        rendered: list[str] = []
        remaining_cells = 1000
        for sheet_name, cells in list(sheets.items())[:10]:
            visible = [cell for cell in cells if 1 <= cell["row"] <= 100 and 1 <= cell["column"] <= 30]
            visible = visible[:remaining_cells]
            if not visible:
                continue
            max_row = max(cell["row"] for cell in visible)
            max_column = max(cell["column"] for cell in visible)
            values = {(cell["row"], cell["column"]): cell["value"] for cell in visible}
            rows = []
            for row in range(1, max_row + 1):
                tag = "th" if row == 1 else "td"
                rows.append(
                    "<tr>"
                    + "".join(
                        f"<{tag}>{escape(values.get((row, column), ''))}</{tag}>"
                        for column in range(1, max_column + 1)
                    )
                    + "</tr>"
                )
            rendered.append(
                f"<section><h2>Sheet：{escape(sheet_name)}</h2><table>{''.join(rows)}</table></section>"
            )
            remaining_cells -= len(visible)
            if remaining_cells <= 0:
                rendered.append('<section class="empty">表格预览已截断；可下载原文件查看完整内容。</section>')
                break
        if rendered:
            return "".join(rendered)
        return PublicWorkingCopyAccessService._document_html(extraction)
