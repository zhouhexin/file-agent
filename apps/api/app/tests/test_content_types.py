"""统一 MIME 推断与结构化抽取内容校验的回归测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.modules.agent.tool_schemas import StructuredFieldSpec, StructuredImageExtractionInput
from app.modules.file_lifecycle.risk import inspect_basic_file_risks
from app.modules.files.content_types import (
    detect_image_content_type,
    detect_structured_source_content_type,
    infer_content_type,
    normalize_content_type,
)
from app.modules.ocr.service import LlmOcrProvider
from app.modules.structured_extraction.service import StructuredExtractionService


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("scan.jpg", "image/jpeg"),
        ("scan.JPEG", "image/jpeg"),
        ("scan.jfif", "image/jpeg"),
        ("scan.png", "image/png"),
        ("scan.webp", "image/webp"),
        ("scan.bmp", "image/bmp"),
        ("scan.tif", "image/tiff"),
        ("scan.tiff", "image/tiff"),
        ("scan.pdf", "application/pdf"),
        ("unknown.bin", "application/octet-stream"),
    ],
)
def test_content_type_inference_is_stable_across_supported_file_extensions(
    filename: str,
    expected: str,
):
    """上传、受管快照和工作副本必须对相同扩展名生成同一个规范 MIME。"""

    assert infer_content_type(filename=filename) == expected
    assert (
        infer_content_type(
            filename=filename,
            declared_content_type="application/octet-stream",
        )
        == expected
    )


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("IMAGE/JPG", "image/jpeg"),
        ("image/pjpeg", "image/jpeg"),
        ("image/x-png; charset=binary", "image/png"),
        ("image/x-ms-bmp", "image/bmp"),
        ("image/tif", "image/tiff"),
    ],
)
def test_common_browser_image_mime_aliases_are_normalized(declared: str, expected: str):
    """浏览器和旧数据库中的兼容 MIME 别名不能导致合法图片被拒绝。"""

    assert normalize_content_type(declared) == expected


def test_inference_preserves_mismatched_dangerous_mime_for_risk_rejection():
    """扩展名推断不能覆盖客户端明确上报的危险或不匹配类型。"""

    assert (
        infer_content_type(
            filename="fake.jpg",
            declared_content_type="application/x-msdownload",
        )
        == "application/x-msdownload"
    )


def test_basic_risk_check_covers_image_extensions_and_aliases(tmp_path: Path):
    """统一规则必须让图片也进入 MIME/扩展名一致性审计。"""

    path = tmp_path / "scan.jpg"
    path.write_bytes(b"not-used-by-basic-mime-check")

    compatible = inspect_basic_file_risks(
        file_path=path,
        filename=path.name,
        content_type="image/jpg",
    )
    mismatch = inspect_basic_file_risks(
        file_path=path,
        filename=path.name,
        content_type="text/plain",
    )

    assert compatible.mime_consistent is True
    assert compatible.warnings == []
    assert mismatch.mime_consistent is False
    assert mismatch.warnings[0]["code"] == "MIME_EXTENSION_MISMATCH"


@pytest.mark.parametrize(
    ("suffix", "image_format", "expected"),
    [
        (".jpg", "JPEG", "image/jpeg"),
        (".png", "PNG", "image/png"),
        (".webp", "WEBP", "image/webp"),
        (".bmp", "BMP", "image/bmp"),
        (".tiff", "TIFF", "image/tiff"),
    ],
)
def test_content_detection_accepts_every_supported_real_image_format(
    tmp_path: Path,
    suffix: str,
    image_format: str,
    expected: str,
):
    """历史 MIME 无论是否准确，所有受支持图片都必须由真实容器格式识别。"""

    path = tmp_path / f"scan{suffix}"
    Image.new("RGB", (32, 24), "white").save(path, format=image_format)

    assert detect_structured_source_content_type(path) == expected
    assert detect_image_content_type(path) == expected


def test_content_detection_accepts_real_pdf_by_signature_and_container(tmp_path: Path):
    """PDF 扫描件候选必须具有真实 PDF 头，后续页数校验再完整打开容器。"""

    import fitz

    path = tmp_path / "scan.pdf"
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()

    assert detect_structured_source_content_type(path) == "application/pdf"


def test_content_detection_rejects_non_image_with_jpg_extension(tmp_path: Path):
    """内容校验不能因为扩展名或数据库 MIME 看起来像图片就放行伪装文件。"""

    path = tmp_path / "fake.jpg"
    path.write_text("this is not an image", encoding="utf-8")

    assert detect_structured_source_content_type(path) is None
    assert detect_image_content_type(path) is None


def test_llm_ocr_data_url_uses_verified_bmp_mime(tmp_path: Path):
    """LLM OCR 发送图片时必须使用真实容器 MIME，不能再把 BMP/TIFF 错标成 PNG。"""

    path = tmp_path / "scan.bmp"
    Image.new("RGB", (32, 24), "white").save(path, format="BMP")

    class FakeClient:
        image_url = ""

        def complete_multimodal_json(self, **kwargs):
            self.image_url = kwargs["image_url"]
            return {"text": "识别内容", "confidence": 0.9, "warnings": []}

    client = FakeClient()
    result = LlmOcrProvider(client=client).extract_image(image_path=path)

    assert result["ok"] is True
    assert client.image_url.startswith("data:image/bmp;base64,")


def test_llm_ocr_rejects_spoofed_image_before_external_call(tmp_path: Path):
    """外部 OCR 调用前必须关闭式拒绝伪装图片，避免发送错误或未授权内容。"""

    path = tmp_path / "fake.jpg"
    path.write_text("not an image", encoding="utf-8")

    class FailingClient:
        def complete_multimodal_json(self, **_kwargs):
            raise AssertionError("伪装图片不得发送到外部 Provider")

    with pytest.raises(ValueError, match="无法安全读取"):
        LlmOcrProvider(client=FailingClient()).extract_image(image_path=path)


def _enqueue_service_for_source(path: Path, *, declared_content_type: str) -> StructuredExtractionService:
    """构造不访问数据库和真实模型的入队服务，只验证结构化抽取前置边界。"""

    service = object.__new__(StructuredExtractionService)
    service.settings = SimpleNamespace(
        structured_extraction_enabled=True,
        pp_structure_enabled=True,
        structured_extraction_max_fields=8,
        pp_structure_max_pdf_pages=10,
        pp_structure_max_image_pixels=1_000_000,
    )
    service.file_repository = SimpleNamespace(
        resolve_original_file=lambda _document_id: {
            "ok": True,
            "document": SimpleNamespace(
                id="doc-1",
                original_filename=path.name,
                content_type=declared_content_type,
            ),
            "file_object": SimpleNamespace(sha256="a" * 64),
            "file_path": path,
        }
    )
    # 返回空版本可以让测试在内容校验后稳定停止，不会创建运行或异步任务。
    service.repository = SimpleNamespace(latest_document_version=lambda *_args, **_kwargs: None)
    return service


def test_structured_enqueue_repairs_historical_octet_stream_by_content(tmp_path: Path):
    """结构化抽取入口不能再只依赖历史 document.content_type。"""

    path = tmp_path / "scan.jpg"
    Image.new("RGB", (32, 24), "white").save(path, format="JPEG")
    service = _enqueue_service_for_source(
        path,
        declared_content_type="application/octet-stream",
    )
    tool_input = StructuredImageExtractionInput(
        document_id="doc-1",
        schema_mode="EXPLICIT_FIELDS",
        fields=[StructuredFieldSpec(key="name", label="姓名", field_type="person_name")],
    )

    result = service.enqueue(tool_input)

    assert result["error"]["code"] == "DOCUMENT_VERSION_REQUIRED"


def test_structured_enqueue_rejects_spoofed_jpeg_even_with_image_mime(tmp_path: Path):
    """真实内容不是图片时必须在创建运行和异步任务前关闭式失败。"""

    path = tmp_path / "fake.jpg"
    path.write_text("not an image", encoding="utf-8")
    service = _enqueue_service_for_source(path, declared_content_type="image/jpeg")
    tool_input = StructuredImageExtractionInput(
        document_id="doc-1",
        schema_mode="EXPLICIT_FIELDS",
        fields=[StructuredFieldSpec(key="name", label="姓名", field_type="person_name")],
    )

    result = service.enqueue(tool_input)

    assert result["error"]["code"] == "UNSUPPORTED_STRUCTURED_IMAGE_TYPE"
