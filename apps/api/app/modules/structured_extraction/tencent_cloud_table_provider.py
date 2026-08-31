"""腾讯云表格识别 V3 Provider。

该 Provider 只负责把经过后端校验的图片或扫描 PDF 页面发送到腾讯云
``RecognizeTableAccurateOCR``，并将返回的单元格投影为结构化抽取模块已有的
``LayoutParseResult``。字段归一化、证据校验和数据库写入仍由
``StructuredExtractionService`` 完成。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from PIL import Image, UnidentifiedImageError

from app.core.config import Settings
from app.modules.ocr.tencent_cloud_provider import (
    OcrImageTooLargeError,
    _encode_image,
    _sdk_error,
)
from app.modules.structured_extraction.schemas import (
    LayoutBoundingBox,
    LayoutElement,
    LayoutPage,
    LayoutParseResult,
)


_TABLE_RETRYABLE_ERROR_PREFIXES = (
    "InternalError",
    "ServiceUnavailable",
    "RequestLimitExceeded",
    "FailedOperation.EngineRecognizeTimeout",
)
_TABLE_RATE_LOCK = threading.Lock()
_TABLE_LAST_CALL_AT = 0.0


class TencentCloudTableOcrError(RuntimeError):
    """腾讯云表格 OCR 请求失败，携带不含敏感数据的稳定错误信息。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TencentCloudTableOcrProvider:
    """腾讯云 ``RecognizeTableAccurateOCR`` 表格识别适配器。"""

    name = "tencent_cloud_table"
    version = "RecognizeTableAccurateOCR@2018-11-19"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        secret_id: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
        endpoint: str | None = None,
        action: str = "RecognizeTableAccurateOCR",
        timeout_seconds: int | None = None,
        max_retries: int | None = None,
        max_qps: int | None = None,
        max_image_bytes: int | None = None,
        external_content_authorized: bool | None = None,
        client: Any | None = None,
        request_factory: Callable[[dict[str, Any]], Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """保存配置；默认复用腾讯云基础 OCR 的凭证和外发授权。"""

        current = settings
        self.secret_id = (secret_id if secret_id is not None else getattr(current, "tencent_cloud_ocr_secret_id", "")).strip()
        self.secret_key = secret_key if secret_key is not None else getattr(current, "tencent_cloud_ocr_secret_key", "")
        self.region = (region if region is not None else getattr(current, "tencent_cloud_ocr_region", "ap-guangzhou")).strip() or "ap-guangzhou"
        self.endpoint = (endpoint if endpoint is not None else getattr(current, "tencent_cloud_ocr_endpoint", "ocr.tencentcloudapi.com")).strip() or "ocr.tencentcloudapi.com"
        self.action = action.strip() or "RecognizeTableAccurateOCR"
        self.timeout_seconds = max(1, int(timeout_seconds if timeout_seconds is not None else getattr(current, "tencent_cloud_ocr_timeout_seconds", 30)))
        self.max_retries = max(0, int(max_retries if max_retries is not None else getattr(current, "tencent_cloud_ocr_max_retries", 2)))
        # 表格接口默认单独限流为 2 QPS，避免图片基础 OCR 的调用量掩盖表格预算。
        self.max_qps = max(
            1,
            int(
                max_qps
                if max_qps is not None
                else getattr(current, "tencent_cloud_table_ocr_max_qps", 2)
            ),
        )
        self.max_image_bytes = max(1_024, int(max_image_bytes if max_image_bytes is not None else getattr(current, "tencent_cloud_ocr_max_image_bytes", 10 * 1024 * 1024)))
        self.external_content_authorized = bool(
            external_content_authorized
            if external_content_authorized is not None
            else getattr(current, "ocr_external_content_authorized", False)
        )
        self._client = client
        self._request_factory = request_factory
        self._sleep = sleep_fn

    def parse(self, *, file_path: Path, page_number: int | None = None) -> LayoutParseResult:
        """按页调用腾讯云表格 OCR，并返回可持久化的表格单元格元素。"""

        self.validate_configuration()
        if not file_path.is_file():
            raise TencentCloudTableOcrError("OCR_INPUT_INVALID", "表格 OCR 输入文件不存在。")
        try:
            with tempfile.TemporaryDirectory(prefix="file-agent-tencent-table-") as temp_dir:
                pages = list(_materialize_pages(file_path=file_path, output_dir=Path(temp_dir)))
                if not pages:
                    raise TencentCloudTableOcrError("OCR_INPUT_INVALID", "表格 OCR 输入文件没有可读取页面。")
                parsed_pages: list[LayoutPage] = []
                next_element_index = 0
                for index, page_path in enumerate(pages, start=1):
                    actual_page = page_number if page_number is not None else index
                    parsed_page = self._parse_page(page_path, actual_page)
                    normalized_elements: list[LayoutElement] = []
                    for element in parsed_page.elements:
                        normalized_elements.append(
                            element.model_copy(
                                update={
                                    "element_index": next_element_index,
                                    "reading_order": next_element_index,
                                }
                            )
                        )
                        next_element_index += 1
                    parsed_pages.append(
                        parsed_page.model_copy(update={"elements": normalized_elements})
                    )
        except TencentCloudTableOcrError:
            raise
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            raise TencentCloudTableOcrError("OCR_INPUT_INVALID", "表格 OCR 输入文件无法安全读取。") from exc
        return LayoutParseResult(
            provider=self.name,
            provider_version=self.version,
            pages=parsed_pages,
            warnings=[] if any(page.elements for page in parsed_pages) else ["TENCENT_TABLE_NO_CELLS"],
        )

    def _parse_page(self, image_path: Path, page_number: int) -> LayoutPage:
        try:
            with Image.open(image_path) as image:
                width, height = image.size
        except (OSError, UnidentifiedImageError) as exc:
            raise TencentCloudTableOcrError("OCR_INPUT_INVALID", "表格 OCR 页面图片无法安全读取。") from exc
        try:
            image_base64, _ = _encode_image(image_path=image_path, max_image_bytes=self.max_image_bytes)
        except OcrImageTooLargeError as exc:
            raise TencentCloudTableOcrError("OCR_IMAGE_TOO_LARGE", "图片超过腾讯云 OCR 的请求大小限制。") from exc
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            raise TencentCloudTableOcrError("OCR_INPUT_INVALID", "表格 OCR 页面图片无法安全编码。") from exc
        response = self._request_with_retry(image_base64)
        elements = _elements_from_response(
            response,
            table_id_prefix=f"page-{page_number}",
        )
        return LayoutPage(
            page_number=page_number,
            width=width,
            height=height,
            provider_request_id=str(
                _value(response, "RequestId", "request_id") or ""
            )[:128]
            or None,
            elements=elements,
        )

    def _request_with_retry(self, image_base64: str) -> Any:
        last_error: dict[str, Any] | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self._wait_for_rate_limit()
                return self._call_api(image_base64)
            except Exception as exc:
                error = _sdk_error(exc)
                error["retryable"] = bool(error["retryable"] or any(str(error["code"]).startswith(prefix) for prefix in _TABLE_RETRYABLE_ERROR_PREFIXES))
                last_error = error
                if not error["retryable"] or attempt >= self.max_retries:
                    break
                self._sleep(min(8.0, 0.5 * (2**attempt)))
        assert last_error is not None
        raise TencentCloudTableOcrError(
            _table_internal_code(str(last_error["code"])),
            _table_internal_message(_table_internal_code(str(last_error["code"]))),
            retryable=bool(last_error["retryable"]),
        )

    def validate_configuration(self) -> None:
        """在创建异步任务前也可复用的关闭式配置校验。"""

        if not self.external_content_authorized:
            raise TencentCloudTableOcrError("OCR_EXTERNAL_CONTENT_NOT_AUTHORIZED", "未授权将文件图片发送到腾讯云表格 OCR。")
        if not self.secret_id or not self.secret_key:
            raise TencentCloudTableOcrError("OCR_PROVIDER_CONFIG_INVALID", "腾讯云 OCR SecretId 或 SecretKey 未配置。")

    def _call_api(self, image_base64: str) -> Any:
        client = self._client or self._build_client()
        payload = {"ImageBase64": image_base64}
        if self._request_factory is not None:
            request = self._request_factory(payload)
        else:
            try:
                from tencentcloud.ocr.v20181119 import models
            except ImportError as exc:
                raise RuntimeError("未安装 tencentcloud-sdk-python-ocr。") from exc
            request = models.RecognizeTableAccurateOCRRequest()
            request.from_json_string(json.dumps(payload))
        method = getattr(client, self.action, None)
        if not callable(method):
            raise RuntimeError(f"腾讯云 OCR SDK 不支持接口 {self.action}。")
        return method(request)

    def _build_client(self) -> Any:
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.ocr.v20181119 import ocr_client
        except ImportError as exc:
            raise RuntimeError("未安装 tencentcloud-sdk-python-common/ocr。") from exc
        credentials = credential.Credential(self.secret_id, self.secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = self.endpoint
        http_profile.reqTimeout = self.timeout_seconds
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        self._client = ocr_client.OcrClient(credentials, self.region, client_profile)
        return self._client

    def _wait_for_rate_limit(self) -> None:
        global _TABLE_LAST_CALL_AT
        interval = 1.0 / float(self.max_qps)
        with _TABLE_RATE_LOCK:
            now = time.monotonic()
            delay = interval - (now - _TABLE_LAST_CALL_AT)
            if delay > 0:
                self._sleep(delay)
            _TABLE_LAST_CALL_AT = time.monotonic()


def _materialize_pages(*, file_path: Path, output_dir: Path) -> Iterable[Path]:
    """将图片/PDF转换为腾讯云支持的单页 PNG 输入，不修改原件。"""

    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz
        except ImportError as exc:
            raise ValueError("当前环境缺少 PDF 渲染依赖。") from exc
        with fitz.open(file_path) as document:
            for index, page in enumerate(document, start=1):
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                target = output_dir / f"page-{index:04d}.png"
                pixmap.save(target)
                yield target
        return
    with Image.open(file_path) as image:
        frame_count = int(getattr(image, "n_frames", 1) or 1)
        for index in range(frame_count):
            image.seek(index)
            target = output_dir / f"page-{index + 1:04d}.png"
            image.convert("RGB").save(target, format="PNG")
            yield target


def _elements_from_response(
    response: Any,
    *,
    table_id_prefix: str | None = None,
) -> list[LayoutElement]:
    tables = _value(response, "TableDetections", "table_detections", "Tables", "tables")
    if tables is None:
        tables = [response]
    if isinstance(tables, dict):
        tables = [tables]
    elements: list[LayoutElement] = []
    element_index = 0
    for table_index, table in enumerate(tables or [], start=1):
        cells = _value(table, "Cells", "cells", "TableCells", "table_cells")
        if isinstance(cells, dict):
            cells = [cells]
        provider_table_id = str(
            _value(table, "TableId", "table_id", "Id", "id")
            or f"table-{table_index}"
        )
        table_id = (
            f"{table_id_prefix}:{provider_table_id}"
            if table_id_prefix
            else provider_table_id
        )[:120]
        for cell in cells or []:
            # 腾讯云 V3 TableCellInfo 的正式字段是 Text；其余名称仅用于兼容
            # SDK 包装差异或历史离线数据，不能让兼容字段覆盖正式字段。
            text = str(
                _value(
                    cell,
                    "Text",
                    "text",
                    "CellContent",
                    "cell_content",
                    "Content",
                    "DetectedText",
                )
                or ""
            ).strip()
            if not text:
                continue
            row_start = _index_value(cell, "RowTl", "row_tl", "RowStart", "row_start", "Row")
            row_end = _index_value(cell, "RowBr", "row_br", "RowEnd", "row_end", "Row")
            column_start = _index_value(cell, "ColTl", "col_tl", "ColumnStart", "column_start", "Col", "Column")
            column_end = _index_value(cell, "ColBr", "col_br", "ColumnEnd", "column_end", "Col", "Column")
            if row_start is None:
                row_start = 0
            if row_end is None:
                row_end = row_start
            if column_start is None:
                column_start = 0
            if column_end is None:
                column_end = column_start
            elements.append(
                LayoutElement(
                    element_index=element_index,
                    element_type="table_cell",
                    text=text,
                    confidence=_confidence(_value(cell, "Confidence", "confidence", "Score", "score")),
                    bbox=_bbox(_value(cell, "Polygon", "polygon", "ItemPolygon", "item_polygon", "Coord", "coord", "Bbox", "bbox")),
                    reading_order=element_index,
                    table_id=table_id,
                    row_start=min(row_start, row_end),
                    row_end=max(row_start, row_end),
                    column_start=min(column_start, column_end),
                    column_end=max(column_start, column_end),
                )
            )
            element_index += 1
    return elements


def _value(value: Any, *keys: str) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        lowered = {str(key).lower(): item for key, item in value.items()}
        for key in keys:
            if key.lower() in lowered:
                return lowered[key.lower()]
        return None
    for key in keys:
        candidate = getattr(value, key, None)
        if candidate is not None:
            return candidate
    return None


def _index_value(value: Any, *keys: str) -> int | None:
    raw = _value(value, *keys)
    if raw is None or raw == "":
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError, OverflowError):
        return None


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1:
        number /= 100
    return round(max(0.0, min(number, 1.0)), 6)


def _bbox(value: Any) -> LayoutBoundingBox | None:
    if isinstance(value, dict):
        direct = [value.get(key) for key in ("left", "top", "right", "bottom")]
        if all(item is not None for item in direct):
            return LayoutBoundingBox(left=float(direct[0]), top=float(direct[1]), right=float(direct[2]), bottom=float(direct[3]))
        x = value.get("X", value.get("x"))
        y = value.get("Y", value.get("y"))
        width = value.get("Width", value.get("width"))
        height = value.get("Height", value.get("height"))
        if all(item is not None for item in (x, y, width, height)):
            return LayoutBoundingBox(left=float(x), top=float(y), right=float(x) + float(width), bottom=float(y) + float(height))
    if value is not None and not isinstance(value, (str, bytes, dict)):
        points = list(value) if isinstance(value, Iterable) else []
        if len(points) == 4 and all(isinstance(item, (int, float)) for item in points):
            return LayoutBoundingBox(left=float(points[0]), top=float(points[1]), right=float(points[2]), bottom=float(points[3]))
        normalized: list[tuple[float, float]] = []
        for point in points:
            x = _value(point, "X", "x")
            y = _value(point, "Y", "y")
            if x is not None and y is not None:
                normalized.append((float(x), float(y)))
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                normalized.append((float(point[0]), float(point[1])))
        if normalized:
            xs, ys = zip(*normalized)
            return LayoutBoundingBox(left=min(xs), top=min(ys), right=max(xs), bottom=max(ys))
    return None


def _table_internal_code(provider_code: str) -> str:
    if provider_code.startswith(("AuthFailure", "UnauthorizedOperation")):
        return "OCR_PROVIDER_AUTH_FAILED"
    if provider_code.startswith("FailedOperation.UnOpenError"):
        return "OCR_PROVIDER_NOT_ENABLED"
    if provider_code.startswith(("LimitExceeded.TooLargeFileError", "RequestSizeLimitExceeded")):
        return "OCR_IMAGE_TOO_LARGE"
    if provider_code.startswith("FailedOperation.ImageNoText"):
        return "OCR_NO_TABLE"
    if provider_code.startswith("InvalidParameter") or provider_code.startswith("FailedOperation.ImageDecodeFailed"):
        return "OCR_INPUT_INVALID"
    if any(provider_code.startswith(prefix) for prefix in _TABLE_RETRYABLE_ERROR_PREFIXES):
        return "OCR_PROVIDER_TEMPORARY_FAILURE"
    if provider_code in {"RuntimeError", "ImportError", "ModuleNotFoundError"}:
        return "OCR_PROVIDER_NOT_AVAILABLE"
    return "OCR_PROVIDER_FAILED"


def _table_internal_message(code: str) -> str:
    return {
        "OCR_PROVIDER_AUTH_FAILED": "腾讯云 OCR 鉴权失败，请管理员检查密钥和 CAM 权限。",
        "OCR_PROVIDER_NOT_ENABLED": "腾讯云 OCR 服务尚未开通。",
        "OCR_IMAGE_TOO_LARGE": "图片超过腾讯云 OCR 的请求大小限制。",
        "OCR_NO_TABLE": "腾讯云 OCR 未识别到可用表格。",
        "OCR_INPUT_INVALID": "图片格式或内容不符合腾讯云 OCR 要求。",
        "OCR_PROVIDER_TEMPORARY_FAILURE": "腾讯云 OCR 暂时不可用，请稍后重试。",
        "OCR_PROVIDER_NOT_AVAILABLE": "腾讯云 OCR SDK 未安装或运行环境不可用。",
    }.get(code, "腾讯云表格 OCR 识别失败。")
