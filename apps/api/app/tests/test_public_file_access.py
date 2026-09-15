"""搜索结果公开文件能力链接与 HTML 预览单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.security import (
    TokenDecodeError,
    create_public_classification_access_token,
    create_public_file_access_token,
    decode_public_classification_access_token,
    decode_public_file_access_token,
)
from app.modules.file_lifecycle.public_access import PublicWorkingCopyAccessService
from app.tests.helpers import clear_overrides, client_with_database


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


def test_public_classification_access_token_is_read_only_and_has_no_expiration() -> None:
    """分类能力令牌只声明只读范围，不能携带登录身份或过期字段。"""

    token = create_public_classification_access_token()
    payload = decode_public_classification_access_token(token)

    assert payload == {
        "aud": "file-agent-public-classification-access-v1",
        "scope": "organization-classification-read",
    }
    assert "sub" not in payload
    assert "role" not in payload
    assert "exp" not in payload

    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    with pytest.raises(TokenDecodeError):
        decode_public_classification_access_token(tampered)


def test_public_classification_routes_skip_login_only_with_signed_token() -> None:
    """WorkBuddy 能力链接无需登录；缺失或篡改令牌不能读取分类目录。"""

    client, _session_factory = client_with_database()
    try:
        token = create_public_classification_access_token()
        tree_response = client.get(
            "/api/classification/organization/public/tree",
            params={"access_token": token},
        )
        files_response = client.get(
            "/api/classification/organization/public/files",
            params={"access_token": token, "page": 1, "page_size": 20},
        )

        assert tree_response.status_code == 200
        assert tree_response.json()["public_access_token"] == token
        assert files_response.status_code == 200
        assert files_response.json()["public_access_token"] == token

        missing_response = client.get(
            "/api/classification/organization/public/tree"
        )
        protected_response = client.get("/api/classification/organization/tree")
        tampered_response = client.get(
            "/api/classification/organization/public/tree",
            params={"access_token": f"{token}x"},
        )
        assert missing_response.status_code == 422
        assert protected_response.status_code == 401
        assert tampered_response.status_code == 404
    finally:
        clear_overrides()


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
