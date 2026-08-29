"""腾讯云 OCR Provider 的离线契约测试。

测试只注入 deterministic fake Client，不安装或调用真实腾讯云 SDK，也不会产生费用。
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.modules.ocr.tencent_cloud_provider import TencentCloudOcrProvider


class FakeTencentClient:
    """记录请求并返回固定腾讯云响应。"""

    def __init__(self, response: object | None = None, errors: list[Exception] | None = None) -> None:
        """保存响应和按顺序抛出的异常。"""

        self.response = response
        self.errors = list(errors or [])
        self.requests: list[dict] = []

    def GeneralAccurateOCR(self, request: dict) -> object:
        """模拟官方 Client 方法。"""

        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)
        return self.response


class FakeTencentError(RuntimeError):
    """模拟腾讯云 SDK 的 code/message 属性。"""

    def __init__(self, code: str, message: str) -> None:
        """保存不含敏感信息的服务错误。"""

        super().__init__(message)
        self.code = code
        self.message = message


def _image(path: Path) -> None:
    """生成由 Pillow 可验证的真实 PNG。"""

    Image.new("RGB", (640, 800), color="white").save(path, format="PNG")


def _provider(*, client: FakeTencentClient, authorized: bool = True, **overrides: object) -> TencentCloudOcrProvider:
    """构造不依赖真实 SDK 的 Provider。"""

    return TencentCloudOcrProvider(
        secret_id="test-secret-id",
        secret_key="test-secret-key",
        external_content_authorized=authorized,
        client=client,
        request_factory=lambda payload: payload,
        sleep_fn=lambda _: None,
        **overrides,
    )


def test_tencent_provider_maps_text_coordinates_and_confidence(tmp_path):
    """腾讯百分制置信度、坐标和 RequestId 必须转换为统一 OCR 结果。"""

    image_path = tmp_path / "scan.png"
    _image(image_path)
    response = SimpleNamespace(
        RequestId="request-123",
        TextDetections=[
            SimpleNamespace(
                DetectedText="西安理工大学文件",
                Confidence=98,
                Polygon=None,
                ItemPolygon=SimpleNamespace(X=10, Y=20, Width=200, Height=30),
            ),
            SimpleNamespace(
                DetectedText="学生工作通知",
                Confidence=96,
                Polygon=None,
                ItemPolygon=SimpleNamespace(X=10, Y=60, Width=180, Height=30),
            ),
        ],
    )
    client = FakeTencentClient(response=response)

    result = _provider(client=client).extract_image(image_path=image_path, page_number=2)

    assert result["ok"] is True
    assert result["text"] == "西安理工大学文件\n学生工作通知"
    assert result["confidence"] == 0.97
    assert result["provider_request_id"] == "request-123"
    assert result["blocks"][0]["polygon"] == [[10, 20], [210, 20], [210, 50], [10, 50]]
    assert base64.b64decode(client.requests[0]["ImageBase64"]).startswith(b"\x89PNG")


def test_tencent_provider_refuses_external_call_without_authorization(tmp_path):
    """未显式授权时即使密钥完整也不能把图片发给腾讯云。"""

    image_path = tmp_path / "scan.png"
    _image(image_path)
    client = FakeTencentClient(response=SimpleNamespace(TextDetections=[], RequestId="unused"))

    result = _provider(client=client, authorized=False).extract_image(image_path=image_path)

    assert result["ok"] is False
    assert result["error"]["code"] == "OCR_EXTERNAL_CONTENT_NOT_AUTHORIZED"
    assert client.requests == []


def test_tencent_provider_retries_only_retryable_error(tmp_path):
    """限流错误可以有界重试，鉴权错误不能盲目重试。"""

    image_path = tmp_path / "scan.png"
    _image(image_path)
    response = SimpleNamespace(RequestId="request-after-retry", TextDetections=[])
    retry_client = FakeTencentClient(
        response=response,
        errors=[FakeTencentError("RequestLimitExceeded", "请求过快")],
    )
    retry_result = _provider(client=retry_client, max_retries=1).extract_image(image_path=image_path)

    auth_client = FakeTencentClient(
        errors=[FakeTencentError("AuthFailure.SecretIdNotFound", "密钥不存在")],
    )
    auth_result = _provider(client=auth_client, max_retries=2).extract_image(image_path=image_path)

    assert retry_result["ok"] is True
    assert len(retry_client.requests) == 2
    assert auth_result["ok"] is False
    assert auth_result["error"]["code"] == "OCR_PROVIDER_AUTH_FAILED"
    assert auth_result["error"]["provider_code"] == "AuthFailure.SecretIdNotFound"
    assert len(auth_client.requests) == 1


def test_tencent_provider_rejects_spoofed_image_before_call(tmp_path):
    """伪装图片必须在外发前关闭式拒绝。"""

    image_path = tmp_path / "fake.png"
    image_path.write_text("not an image", encoding="utf-8")
    client = FakeTencentClient()

    result = _provider(client=client).extract_image(image_path=image_path)

    assert result["ok"] is False
    assert result["error"]["code"] == "OCR_IMAGE_INVALID"
    assert client.requests == []


def test_tencent_provider_reports_image_too_large_before_call(tmp_path):
    """受控压缩后仍超限的图片必须返回稳定大小错误，不能误报图片损坏。"""

    image_path = tmp_path / "noisy.png"
    Image.effect_noise((640, 800), 100).convert("RGB").save(image_path, format="PNG")
    client = FakeTencentClient()

    result = _provider(client=client, max_image_bytes=1_024).extract_image(
        image_path=image_path
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "OCR_IMAGE_TOO_LARGE"
    assert client.requests == []
