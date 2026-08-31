"""腾讯云表格 OCR Provider 的离线契约测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.modules.structured_extraction.tencent_cloud_table_provider import (
    TencentCloudTableOcrError,
    TencentCloudTableOcrProvider,
    _elements_from_response,
)


class FakeTableClient:
    """记录请求并返回固定表格响应。"""

    def __init__(self, response: object | None = None, errors: list[Exception] | None = None):
        self.response = response
        self.errors = list(errors or [])
        self.requests: list[dict] = []

    def RecognizeTableAccurateOCR(self, request: dict) -> object:
        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)
        return self.response


class FakeTableError(RuntimeError):
    """模拟腾讯云 SDK 错误。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.message = code


def _image(path: Path) -> None:
    Image.new("RGB", (800, 600), color="white").save(path, format="PNG")


def _provider(client: FakeTableClient, **overrides: object) -> TencentCloudTableOcrProvider:
    return TencentCloudTableOcrProvider(
        secret_id="test-secret-id",
        secret_key="test-secret-key",
        external_content_authorized=True,
        client=client,
        request_factory=lambda payload: payload,
        sleep_fn=lambda _: None,
        **overrides,
    )


def test_table_provider_maps_cells_rows_columns_and_coordinates(tmp_path: Path):
    image_path = tmp_path / "table.png"
    _image(image_path)
    client = FakeTableClient(
        response=SimpleNamespace(
            RequestId="table-request-1",
            TableDetections=[
                SimpleNamespace(
                    TableId="table-a",
                    Cells=[
                        SimpleNamespace(
                            Text="姓名",
                            Confidence=98,
                            RowTl=0,
                            RowBr=0,
                            ColTl=0,
                            ColBr=0,
                            Polygon=[
                                SimpleNamespace(X=10, Y=20),
                                SimpleNamespace(X=110, Y=20),
                                SimpleNamespace(X=110, Y=60),
                                SimpleNamespace(X=10, Y=60),
                            ],
                        ),
                        SimpleNamespace(
                            Text="张三",
                            Confidence=92,
                            RowTl=1,
                            RowBr=1,
                            ColTl=0,
                            ColBr=0,
                            ItemPolygon=SimpleNamespace(X=10, Y=70, Width=100, Height=40),
                        ),
                    ],
                )
            ],
        )
    )

    result = _provider(client).parse(file_path=image_path)

    assert result.provider == "tencent_cloud_table"
    assert result.pages[0].width == 800
    assert result.pages[0].provider_request_id == "table-request-1"
    assert len(result.pages[0].elements) == 2
    assert result.pages[0].elements[0].element_type == "table_cell"
    assert result.pages[0].elements[0].row_start == 0
    assert result.pages[0].elements[1].row_start == 1
    assert result.pages[0].elements[1].column_start == 0
    assert result.pages[0].elements[0].confidence == 0.98
    assert result.pages[0].elements[0].bbox is not None
    assert result.pages[0].elements[0].bbox.right == 110
    assert client.requests and client.requests[0]["ImageBase64"]


def test_table_provider_requires_external_authorization(tmp_path: Path):
    image_path = tmp_path / "table.png"
    _image(image_path)
    client = FakeTableClient()
    provider = TencentCloudTableOcrProvider(
        secret_id="secret-id",
        secret_key="secret-key",
        external_content_authorized=False,
        client=client,
    )

    try:
        provider.parse(file_path=image_path)
    except TencentCloudTableOcrError as exc:
        assert exc.code == "OCR_EXTERNAL_CONTENT_NOT_AUTHORIZED"
    else:
        raise AssertionError("未授权时必须关闭式失败")
    assert client.requests == []


def test_table_provider_requires_credentials_before_calling_client(tmp_path: Path):
    image_path = tmp_path / "table.png"
    _image(image_path)
    client = FakeTableClient()
    provider = TencentCloudTableOcrProvider(
        secret_id="",
        secret_key="",
        external_content_authorized=True,
        client=client,
    )

    try:
        provider.parse(file_path=image_path)
    except TencentCloudTableOcrError as exc:
        assert exc.code == "OCR_PROVIDER_CONFIG_INVALID"
    else:
        raise AssertionError("凭证缺失时必须关闭式失败")
    assert client.requests == []


def test_table_provider_retries_rate_limit_and_reports_empty_table(tmp_path: Path):
    image_path = tmp_path / "table.png"
    _image(image_path)
    client = FakeTableClient(
        response=SimpleNamespace(TableDetections=[]),
        errors=[FakeTableError("RequestLimitExceeded")],
    )

    result = _provider(client, max_retries=1).parse(file_path=image_path)

    assert result.warnings == ["TENCENT_TABLE_NO_CELLS"]
    assert len(client.requests) == 2


def test_table_provider_renders_scanned_pdf_pages_and_preserves_page_numbers(tmp_path: Path):
    """扫描 PDF 必须逐页渲染并逐页调用腾讯云，不能只识别第一页。"""

    first = Image.new("RGB", (320, 240), color="white")
    second = Image.new("RGB", (320, 240), color="#eeeeee")
    pdf_path = tmp_path / "scan.pdf"
    first.save(pdf_path, format="PDF", save_all=True, append_images=[second])
    client = FakeTableClient(
        response=SimpleNamespace(
            TableDetections=[
                {
                    "Cells": [
                        {"Text": "页内表格", "RowTl": 0, "ColTl": 0}
                    ]
                }
            ]
        )
    )

    result = _provider(client).parse(file_path=pdf_path)

    assert [page.page_number for page in result.pages] == [1, 2]
    assert len(client.requests) == 2
    assert all(request["ImageBase64"] for request in client.requests)
    assert [
        element.element_index
        for page in result.pages
        for element in page.elements
    ] == [0, 1]
    assert result.pages[0].elements[0].table_id != result.pages[1].elements[0].table_id


def test_table_response_accepts_dictionary_cell_shape():
    elements = _elements_from_response(
        {
            "TableDetections": [
                {
                    "Cells": [
                        {
                            "Text": "金额",
                            "RowTl": 2,
                            "RowBr": 2,
                            "ColTl": 3,
                            "ColBr": 4,
                            "Polygon": [[1, 2], [11, 2], [11, 12], [1, 12]],
                        }
                    ]
                }
            ]
        }
    )

    assert elements[0].text == "金额"
    assert elements[0].row_start == 2
    assert elements[0].column_end == 4
    assert elements[0].bbox is not None
    assert elements[0].bbox.bottom == 12


def test_table_provider_maps_oversized_image_to_stable_size_error(tmp_path: Path):
    """图片超过腾讯云限制时返回大小错误而不是泛化为格式错误。"""

    image_path = tmp_path / "large.png"
    _image(image_path)
    provider = _provider(FakeTableClient(), max_image_bytes=1_024)

    try:
        provider.parse(file_path=image_path)
    except TencentCloudTableOcrError as exc:
        assert exc.code == "OCR_IMAGE_TOO_LARGE"
    else:
        raise AssertionError("超限图片必须被拒绝")
