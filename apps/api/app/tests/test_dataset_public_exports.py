"""公开导出链接的认证、签名边界、预览限制和日志脱敏回归。"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openpyxl import Workbook

from app.core import security
from app.main import _PublicAccessLogFilter, _request_path_for_log
from app.tests.test_funding_usage_prompt_edit import _published_book
from app.tests.test_structured_datasets import demo


def test_public_export_links_download_without_login_and_reject_other_tokens(demo, monkeypatch):
    """能力链接只能读取绑定的已发布文件，不能充当登录凭证或接受篡改签名。"""
    client, sessions, users = demo
    published = _published_book(client, sessions, users[0], monkeypatch)
    path = (f"/api/structured-datasets/{published['dataset_id']}"
            f"/exports/{published['export_id']}/browser-links")
    assert client.get(path).status_code == 401
    links_response = client.get(path, headers=users[0])
    assert links_response.status_code == 200, links_response.text
    links = links_response.json()
    download = client.get(links["download_url"])
    assert download.status_code == 200, download.text
    assert download.content == client.get(published["download_url"], headers=users[0]).content
    assert download.headers["cache-control"] == "no-store"
    preview = client.get(links["preview_url"])
    assert preview.status_code == 200
    assert "下载原始 Excel" in preview.text
    assert "default-src 'none'" in preview.headers["content-security-policy"]
    token = links["download_url"].split("/")[-2]
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + token}).status_code == 401
    invalid = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert client.get(f"/api/structured-datasets/public-exports/{invalid}/download").status_code == 404
    wrong_scope = security.create_public_dataset_export_token(str(uuid4()), published["export_id"])
    assert client.get(f"/api/structured-datasets/public-exports/{wrong_scope}/download").status_code == 404


def test_public_export_expiry_and_token_purpose(monkeypatch):
    """过期令牌和其他用途的公开链接不能访问导出文件。"""
    dataset, export = str(uuid4()), str(uuid4())
    token = security.create_public_dataset_export_token(dataset, export, ttl_seconds=1)
    assert security.decode_public_dataset_export_token(token)["dataset_id"] == dataset

    class FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=3)

    monkeypatch.setattr(security, "datetime", FutureDatetime)
    with pytest.raises(security.TokenDecodeError):
        security.decode_public_dataset_export_token(token)
    other = security.create_public_file_access_token(str(uuid4()))
    with pytest.raises(security.TokenDecodeError):
        security.decode_public_dataset_export_token(other)


def test_public_export_request_tokens_are_redacted_in_both_log_channels():
    """应用日志和 Uvicorn 日志都不能保存可直接访问文件的能力令牌。"""
    for prefix in ["/api/structured-datasets/public-exports/", "/api/public/file-access/"]:
        target = prefix + "sensitive-token/preview"
        assert "sensitive-token" not in _request_path_for_log(target)
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                                   '%s - "%s %s HTTP/%s" %d',
                                   ("127.0.0.1", "GET", target, "1.1", 200), None)
        assert _PublicAccessLogFilter().filter(record)
        assert "sensitive-token" not in record.getMessage()
        assert "[redacted]/preview" in record.getMessage()
    assert _request_path_for_log("/api/health") == "/api/health"


@pytest.mark.parametrize("rows,columns,truncated", [(301, 2, True), (2, 41, True), (2, 2, False)])
def test_public_preview_escapes_cells_and_marks_row_or_column_truncation(monkeypatch, tmp_path, rows, columns, truncated):
    """行列上限截断必须告知用户，单元格及文件名不能注入预览 HTML。"""
    from app.modules.structured_datasets import router

    path = tmp_path / "preview.xlsx"
    book = Workbook()
    book.active["A1"] = "<script>alert(1)</script>"
    book.active.cell(rows, columns, "end")
    book.save(path)
    book.close()
    service = Mock()
    service.dataset.return_value = SimpleNamespace(target_filename="<script>文件.xlsx")
    service.download.return_value = SimpleNamespace(path=path)
    monkeypatch.setattr(router, "StructuredDatasetService", lambda db, user: service)
    monkeypatch.setattr(router, "_public_export_scope", lambda token: {"dataset_id": "dataset", "export_id": "export"})
    response = router.preview_public_export("test-token", db=Mock())
    html = response.body.decode("utf-8")
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert ("已截取部分单元格" in html) == truncated
