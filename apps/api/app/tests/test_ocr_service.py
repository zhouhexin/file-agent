"""OCR Provider 编排服务测试。"""

from pathlib import Path

from app.modules.ocr.service import OcrService


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
