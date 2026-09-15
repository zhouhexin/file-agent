"""搜索结果公开文件能力链接与 HTML 预览单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.security import (
    TokenDecodeError,
    create_public_file_access_token,
    decode_public_file_access_token,
)
from app.modules.file_lifecycle.public_access import PublicWorkingCopyAccessService


def test_public_file_access_token_is_stable_without_expiration_claim() -> None:
    """只读能力令牌应绑定工作副本，但不能继承登录令牌的 exp 语义。"""

    working_copy_id = str(uuid4())
    token = create_public_file_access_token(working_copy_id)
    payload = decode_public_file_access_token(token)

    assert payload == {
        "aud": "file-agent-public-file-access-v1",
        "working_copy_id": working_copy_id,
    }
    assert "exp" not in payload

    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    with pytest.raises(TokenDecodeError):
        decode_public_file_access_token(tampered)


def test_document_preview_escapes_filename_and_extracted_text() -> None:
    """文件名和持久化正文只能作为页面数据，不能注入 HTML。"""

    extraction = {
        "pages": [
            SimpleNamespace(
                page_number=1,
                text_content='<script>alert("正文")</script>',
            )
        ],
        "elements": [],
    }
    html = PublicWorkingCopyAccessService._render_preview_html(
        filename='<img src=x onerror=alert("文件名")>.docx',
        extension=".docx",
        extraction=extraction,
        download_url="/api/public/file-access/token/download",
    )

    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x" in html


def test_spreadsheet_preview_renders_sheet_and_cells_as_table() -> None:
    """Excel 预览应使用结构化 table_cell，而不是只显示不可定位的摘要。"""

    extraction = {
        "pages": [],
        "elements": [
            SimpleNamespace(
                label="table_cell",
                text_content="姓名",
                metadata_json={
                    "sheet_name": "名单",
                    "row": 1,
                    "column": 1,
                    "display_value": "姓名",
                },
            ),
            SimpleNamespace(
                label="table_cell",
                text_content="赵明华",
                metadata_json={
                    "sheet_name": "名单",
                    "row": 2,
                    "column": 1,
                    "display_value": "赵明华",
                },
            ),
        ],
    }
    html = PublicWorkingCopyAccessService._render_preview_html(
        filename="推荐名单.xlsx",
        extension=".xlsx",
        extraction=extraction,
        download_url="/api/public/file-access/token/download",
    )

    assert "Sheet：名单" in html
    assert "<table>" in html
    assert "<th>姓名</th>" in html
    assert "<td>赵明华</td>" in html
