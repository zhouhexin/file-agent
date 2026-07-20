"""OCR Provider 编排服务测试。"""

from pathlib import Path
import sys
from types import ModuleType

from app.modules.ocr.service import OcrService, PaddleOcrProvider


class FakeProvider:
    """测试用 OCR Provider。"""

    def __init__(self, *, name: str, text: str, quality_score: float) -> None:
        """保存固定返回结果。"""

        self.name = name
        self.text = text
        self.quality_score = quality_score
        self.calls: list[dict] = []

    def extract_image(self, *, image_path: Path, page_number: int = 1) -> dict:
        """记录调用并返回固定 OCR 结果。"""

        self.calls.append({"image_path": image_path, "page_number": page_number})
        return {
            "ok": True,
            "text": self.text,
            "source": self.name,
            "provider_name": self.name,
            "quality_score": self.quality_score,
            "confidence": self.quality_score,
            "blocks": [],
            "warnings": [],
        }


def test_ocr_service_uses_llm_fallback_when_paddle_quality_is_low(tmp_path):
    """本地 PaddleOCR 质量低时，应按阈值调用 LLM OCR 兜底。"""

    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"fake")
    paddle = FakeProvider(name="paddleocr_cpu", text="低质", quality_score=0.2)
    llm = FakeProvider(name="llm_ocr_remote", text="LLM OCR 完整文本", quality_score=0.9)

    result = OcrService(
        primary_provider=paddle,
        fallback_provider=llm,
        fallback_quality_threshold=0.68,
    ).extract_image(image_path=image_path, page_number=3)

    assert result["text"] == "LLM OCR 完整文本"
    assert result["source"] == "llm_ocr_remote"
    assert result["is_fallback"] is True
    assert result["fallback_from"] == "paddleocr_cpu"
    assert paddle.calls[0]["page_number"] == 3
    assert llm.calls[0]["page_number"] == 3


def test_ocr_service_keeps_paddle_result_when_quality_is_enough(tmp_path):
    """本地 PaddleOCR 质量达标时，不应调用 LLM OCR。"""

    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"fake")
    paddle = FakeProvider(name="paddleocr_cpu", text="Paddle OCR 文本", quality_score=0.9)
    llm = FakeProvider(name="llm_ocr_remote", text="LLM OCR 文本", quality_score=0.9)

    result = OcrService(
        primary_provider=paddle,
        fallback_provider=llm,
        fallback_quality_threshold=0.68,
    ).extract_image(image_path=image_path, page_number=1)

    assert result["text"] == "Paddle OCR 文本"
    assert result["source"] == "paddleocr_cpu"
    assert llm.calls == []


def test_paddle_provider_sets_baidu_bos_model_source_before_loading(monkeypatch):
    """加载 PaddleOCR 前必须设置百度 BOS 模型下载源，避免默认源在部署环境不可用。"""

    captured: dict[str, str | None] = {}
    fake_module = ModuleType("paddleocr")

    class FakePaddleOCR:
        """测试用 PaddleOCR 类，记录初始化时的环境变量。"""

        def __init__(self, **_: object) -> None:
            """保存初始化时看到的下载源。"""

            import os

            captured["source"] = os.environ.get("PADDLE_PDX_MODEL_SOURCE")

    fake_module.PaddleOCR = FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)
    monkeypatch.delenv("PADDLE_PDX_MODEL_SOURCE", raising=False)

    PaddleOcrProvider(model_source="BOS")._load_ocr()

    assert captured["source"] == "BOS"


def test_paddle_provider_supports_v3_result_structure(monkeypatch, tmp_path):
    """PaddleOCR 3.x 初始化参数和字典结果必须转换为统一 OCR block。"""

    captured: dict[str, object] = {}
    fake_module = ModuleType("paddleocr")

    class FakePaddleOCR:
        """模拟 PaddleOCR 3.x API。"""

        def __init__(self, **kwargs: object) -> None:
            captured["kwargs"] = kwargs

        def ocr(self, image_path: str):
            captured["image_path"] = image_path
            return [
                {
                    "rec_texts": ["西安理工大学文件", "西安理工人事〔2022】14号"],
                    "rec_scores": [0.98, 0.96],
                    "rec_polys": [
                        [[0, 0], [10, 0], [10, 5], [0, 5]],
                        [[0, 8], [10, 8], [10, 13], [0, 13]],
                    ],
                }
            ]

    fake_module.PaddleOCR = FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)
    monkeypatch.setattr("app.modules.ocr.service.package_version", lambda _: "3.7.0")
    image_path = tmp_path / "scan.png"
    image_path.write_bytes(b"fake")

    result = PaddleOcrProvider(model_source="BOS").extract_image(image_path=image_path)

    assert "show_log" not in captured["kwargs"]
    assert captured["kwargs"]["use_doc_unwarping"] is False
    assert result["text"] == "西安理工大学文件\n西安理工人事〔2022】14号"
    assert result["confidence"] == 0.97
