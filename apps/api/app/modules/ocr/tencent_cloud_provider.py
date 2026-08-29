"""腾讯云通用文字识别 Provider。

本模块只负责把经过格式校验的单页图片发送给腾讯云 `GeneralAccurateOCR`，并转换为
项目统一 OCR 结果。外发授权、大小限制、限流、重试和日志脱敏都在这里收敛；数据库写入
仍由文件解析 Tool 完成，LLM 不会接触腾讯云客户端、密钥或 Base64 内容。
"""

from __future__ import annotations

import base64
from io import BytesIO
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError

from app.modules.files.content_types import detect_image_content_type


_SUPPORTED_TENCENT_MIME_TYPES = {"image/png", "image/jpeg", "image/bmp"}
_RETRYABLE_ERROR_PREFIXES = (
    "InternalError",
    "ServiceUnavailable",
    "RequestLimitExceeded",
    "FailedOperation.EngineRecognizeTimeout",
)
_GLOBAL_RATE_LOCK = threading.Lock()
_GLOBAL_LAST_CALL_AT = 0.0


class OcrImageTooLargeError(ValueError):
    """表示图片经过受控压缩后仍超过腾讯云请求大小限制。"""


class TencentCloudOcrProvider:
    """腾讯云 `GeneralAccurateOCR` 单页识别实现。"""

    name = "tencent_cloud_general_accurate"
    provider_version = "GeneralAccurateOCR@2018-11-19"

    def __init__(
        self,
        *,
        secret_id: str,
        secret_key: str,
        region: str = "ap-guangzhou",
        endpoint: str = "ocr.tencentcloudapi.com",
        action: str = "GeneralAccurateOCR",
        timeout_seconds: int = 30,
        max_retries: int = 2,
        max_qps: int = 2,
        max_image_bytes: int = 10 * 1024 * 1024,
        external_content_authorized: bool = False,
        client: Any | None = None,
        request_factory: Callable[[dict[str, Any]], Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """保存受控配置；未显式授权时即使有密钥也禁止发送文件内容。"""

        self.secret_id = secret_id.strip()
        self.secret_key = secret_key
        self.region = region.strip() or "ap-guangzhou"
        self.endpoint = endpoint.strip() or "ocr.tencentcloudapi.com"
        self.action = action.strip() or "GeneralAccurateOCR"
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self.max_qps = max(1, int(max_qps))
        self.max_image_bytes = max(1_024, int(max_image_bytes))
        self.external_content_authorized = bool(external_content_authorized)
        self._client = client
        self._request_factory = request_factory
        self._sleep = sleep_fn

    def extract_image(self, *, image_path: Path, page_number: int = 1) -> dict[str, Any]:
        """识别单页图片并返回与本地 OCR 一致的结构化结果。"""

        if not self.external_content_authorized:
            return _failed("OCR_EXTERNAL_CONTENT_NOT_AUTHORIZED", "未授权将文件图片发送到腾讯云 OCR。")
        if not self.secret_id or not self.secret_key:
            return _failed("OCR_PROVIDER_CONFIG_INVALID", "腾讯云 OCR SecretId 或 SecretKey 未配置。")
        try:
            image_base64, _mime_type = _encode_image(
                image_path=image_path,
                max_image_bytes=self.max_image_bytes,
            )
        except OcrImageTooLargeError:
            return _failed("OCR_IMAGE_TOO_LARGE", "图片超过腾讯云 OCR 的请求大小限制。")
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            return _failed("OCR_IMAGE_INVALID", f"图片无法发送到腾讯云 OCR：{exc}")

        last_error: dict[str, Any] | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self._wait_for_rate_limit()
                response = self._call_api(image_base64)
                return _parse_response(response, provider_name=self.name, provider_version=self.provider_version)
            except Exception as exc:  # SDK 异常类型在未安装可选依赖时不能静态导入
                error = _sdk_error(exc)
                last_error = error
                if not error["retryable"] or attempt >= self.max_retries:
                    break
                self._sleep(min(8.0, 0.5 * (2**attempt)))
        assert last_error is not None
        internal_code = _internal_error_code(str(last_error["code"]))
        return {
            "ok": False,
            "source": self.name,
            "provider_name": "tencent_cloud",
            "provider_version": self.provider_version,
            "error": {
                "code": internal_code,
                "message": _internal_error_message(internal_code),
                "retryable": bool(last_error["retryable"]),
                "provider_code": str(last_error["code"]),
            },
            "warnings": [],
        }

    def _call_api(self, image_base64: str) -> Any:
        """调用官方 SDK；SDK 仅在首次实际使用时加载，避免未启用腾讯 Provider 时强依赖。"""

        client = self._client or self._build_client()
        payload = {"ImageBase64": image_base64}
        if self._request_factory is not None:
            request = self._request_factory(payload)
        else:
            try:
                from tencentcloud.ocr.v20181119 import models
            except ImportError as exc:
                raise RuntimeError("未安装 tencentcloud-sdk-python-ocr。") from exc
            request = models.GeneralAccurateOCRRequest()
            request.from_json_string(json.dumps(payload))
        method = getattr(client, self.action, None)
        if not callable(method):
            raise RuntimeError(f"腾讯云 OCR SDK 不支持接口 {self.action}。")
        return method(request)

    def _build_client(self) -> Any:
        """构造配置了超时和固定 endpoint 的腾讯云 OCR Client。"""

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
        """在当前进程内限制调用频率；多进程部署仍需由队列或共享限流器统一约束。"""

        global _GLOBAL_LAST_CALL_AT
        interval = 1.0 / float(self.max_qps)
        # build_default_ocr_service 会按文件构造 Provider，因此限流状态必须由进程内
        # 所有 Provider 实例共享，不能每个文件都从零开始计算 QPS。
        with _GLOBAL_RATE_LOCK:
            now = time.monotonic()
            delay = interval - (now - _GLOBAL_LAST_CALL_AT)
            if delay > 0:
                self._sleep(delay)
            _GLOBAL_LAST_CALL_AT = time.monotonic()


def _encode_image(*, image_path: Path, max_image_bytes: int) -> tuple[str, str]:
    """校验真实图片并在必要时生成临时内存压缩副本，不修改原件。"""

    mime_type = detect_image_content_type(image_path)
    if mime_type is None:
        raise ValueError("无法识别真实图片格式。")
    raw_bytes = image_path.read_bytes()
    if mime_type in _SUPPORTED_TENCENT_MIME_TYPES and _base64_size(raw_bytes) <= max_image_bytes:
        return base64.b64encode(raw_bytes).decode("ascii"), mime_type

    try:
        with Image.open(BytesIO(raw_bytes)) as image:
            image.load()
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            # 先保持原分辨率，以免小图因预处理损失文字；超限时逐步缩放和压缩。
            current = image.copy()
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("图片解码失败。") from exc

    for quality in (90, 80, 70, 60, 50):
        output = BytesIO()
        current.save(output, format="JPEG", quality=quality, optimize=True)
        payload = output.getvalue()
        if _base64_size(payload) <= max_image_bytes:
            return base64.b64encode(payload).decode("ascii"), "image/jpeg"
        width, height = current.size
        if min(width, height) <= 600:
            continue
        current = current.resize(
            (max(600, int(width * 0.8)), max(600, int(height * 0.8))),
            Image.Resampling.LANCZOS,
        )
    raise OcrImageTooLargeError("图片 Base64 编码后超过腾讯云 OCR 大小限制。")


def _base64_size(payload: bytes) -> int:
    """计算 Base64 编码后的字节数，避免只检查原始文件大小。"""

    return ((len(payload) + 2) // 3) * 4


def _parse_response(response: Any, *, provider_name: str, provider_version: str) -> dict[str, Any]:
    """把 SDK 响应转换为统一文字块，未定位到文字时返回可审计的空结果。"""

    detections = list(getattr(response, "TextDetections", None) or [])
    blocks: list[dict[str, Any]] = []
    for order, item in enumerate(detections, start=1):
        text = str(getattr(item, "DetectedText", "") or "").strip()
        if not text:
            continue
        confidence = _normalize_confidence(getattr(item, "Confidence", None))
        blocks.append(
            {
                "text": text,
                "order": order,
                "polygon": _polygon_from_item(item),
                "confidence": confidence,
                "role": "text",
            }
        )
    text = "\n".join(block["text"] for block in blocks)
    confidences = [block["confidence"] for block in blocks if block.get("confidence") is not None]
    confidence = sum(confidences) / len(confidences) if confidences else None
    return {
        "ok": True,
        "text": text,
        "source": provider_name,
        "provider_name": "tencent_cloud",
        "provider_version": provider_version,
        "provider_request_id": str(getattr(response, "RequestId", "") or ""),
        "quality_score": _quality_score(text=text, confidence=confidence),
        "confidence": confidence,
        "blocks": blocks,
        "warnings": ["OCR_NO_TEXT"] if not blocks else [],
    }


def _polygon_from_item(item: Any) -> list[list[int]] | None:
    """兼容腾讯 SDK 的 ItemPolygon 矩形字段和 Polygon 点数组。"""

    polygon = getattr(item, "Polygon", None)
    if polygon:
        points = []
        for point in polygon if isinstance(polygon, (list, tuple)) else []:
            x = getattr(point, "X", None)
            y = getattr(point, "Y", None)
            if x is not None and y is not None:
                points.append([int(x), int(y)])
        if points:
            return points
    rectangle = getattr(item, "ItemPolygon", None)
    if rectangle is not None:
        x = int(getattr(rectangle, "X", 0) or 0)
        y = int(getattr(rectangle, "Y", 0) or 0)
        width = int(getattr(rectangle, "Width", 0) or 0)
        height = int(getattr(rectangle, "Height", 0) or 0)
        return [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]
    return None


def _normalize_confidence(value: Any) -> float | None:
    """将腾讯云百分制置信度转换为项目统一的 0 到 1。"""

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(number / 100.0 if number > 1 else number, 1.0)), 6)


def _quality_score(*, text: str, confidence: float | None) -> float:
    """按正文长度和文字行置信度计算与本地 Provider 一致的轻量质量分。"""

    if not text.strip():
        return 0.0
    confidence_score = 0.6 if confidence is None else confidence
    return round((confidence_score * 0.7) + (min(len(text.strip()) / 80, 1.0) * 0.3), 4)


def _sdk_error(exc: Exception) -> dict[str, Any]:
    """提取腾讯 SDK 错误码但不返回密钥、请求体或图片正文。"""

    code = str(getattr(exc, "code", "") or exc.__class__.__name__)
    message = str(getattr(exc, "message", "") or "腾讯云 OCR 请求失败")
    retryable = any(code.startswith(prefix) for prefix in _RETRYABLE_ERROR_PREFIXES)
    return {"code": code, "message": message[:500], "retryable": retryable}


def _internal_error_code(provider_code: str) -> str:
    """把腾讯云易变化的错误码收敛为应用层稳定错误，不影响运维读取 provider_code。"""

    if provider_code.startswith(("AuthFailure", "UnauthorizedOperation")):
        return "OCR_PROVIDER_AUTH_FAILED"
    if provider_code.startswith("FailedOperation.UnOpenError"):
        return "OCR_PROVIDER_NOT_ENABLED"
    if provider_code.startswith(("ResourceUnavailable.InArrears", "ResourceUnavailable.ResourcePackageRunOut")):
        return "OCR_PROVIDER_BILLING_UNAVAILABLE"
    if provider_code.startswith(("LimitExceeded.TooLargeFileError", "RequestSizeLimitExceeded")):
        return "OCR_IMAGE_TOO_LARGE"
    if provider_code.startswith("FailedOperation.ImageNoText"):
        return "OCR_NO_TEXT"
    if provider_code.startswith(("InvalidParameter", "FailedOperation.ImageDecodeFailed", "FailedOperation.EmptyImageError")):
        return "OCR_INPUT_INVALID"
    if any(provider_code.startswith(prefix) for prefix in _RETRYABLE_ERROR_PREFIXES):
        return "OCR_PROVIDER_TEMPORARY_FAILURE"
    if provider_code in {"RuntimeError", "ImportError", "ModuleNotFoundError"}:
        return "OCR_PROVIDER_NOT_AVAILABLE"
    return "OCR_PROVIDER_FAILED"


def _internal_error_message(code: str) -> str:
    """生成不含 SDK 请求细节、密钥或文件内容的稳定用户消息。"""

    messages = {
        "OCR_PROVIDER_AUTH_FAILED": "腾讯云 OCR 鉴权失败，请管理员检查密钥和 CAM 权限。",
        "OCR_PROVIDER_NOT_ENABLED": "腾讯云 OCR 服务尚未开通。",
        "OCR_PROVIDER_BILLING_UNAVAILABLE": "腾讯云 OCR 账户或资源包当前不可用。",
        "OCR_IMAGE_TOO_LARGE": "图片超过腾讯云 OCR 的请求大小限制。",
        "OCR_NO_TEXT": "腾讯云 OCR 未在图片中识别到文字。",
        "OCR_INPUT_INVALID": "图片格式或内容不符合腾讯云 OCR 要求。",
        "OCR_PROVIDER_TEMPORARY_FAILURE": "腾讯云 OCR 暂时不可用，请稍后重试。",
        "OCR_PROVIDER_NOT_AVAILABLE": "腾讯云 OCR SDK 未安装或运行环境不可用。",
        "OCR_PROVIDER_FAILED": "腾讯云 OCR 识别失败。",
    }
    return messages.get(code, "腾讯云 OCR 识别失败。")


def _failed(code: str, message: str) -> dict[str, Any]:
    """构造 OCR 失败结果，供文件解析器生成统一失败页面。"""

    return {
        "ok": False,
        "source": TencentCloudOcrProvider.name,
        "provider_name": "tencent_cloud",
        "provider_version": TencentCloudOcrProvider.provider_version,
        "error": {"code": code, "message": message, "retryable": False},
        "warnings": [],
    }
