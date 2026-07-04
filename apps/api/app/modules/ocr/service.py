"""OCR Provider 编排服务。

本模块只负责把图片页交给本地 OCR 或 LLM OCR Provider，并返回统一结果。
业务数据库写入仍由 extract-document-text Tool 负责。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Protocol

from app.core.config import get_settings
from app.modules.llm.client import OpenAICompatibleLLMClient


class OcrProviderProtocol(Protocol):
    """单页 OCR Provider 的最小接口。"""

    name: str

    def extract_image(self, *, image_path: Path, page_number: int = 1) -> dict[str, Any]:
        """识别单张图片并返回统一 OCR 字段。"""


class PaddleOcrProvider:
    """PaddleOCR CPU Provider，默认用于本地 OCR。"""

    name = "paddleocr_cpu"

    def __init__(self, *, model_source: str = "BOS") -> None:
        """延迟加载 PaddleOCR，避免服务启动时强依赖模型。"""

        self._ocr = None
        self.model_source = model_source

    def extract_image(self, *, image_path: Path, page_number: int = 1) -> dict[str, Any]:
        """使用 PaddleOCR 识别图片文字。"""

        ocr = self._load_ocr()
        result = ocr.ocr(str(image_path), cls=True)
        blocks = _paddle_result_to_blocks(result)
        text = "\n".join(block["text"] for block in blocks if block.get("text"))
        confidence_values = [float(block["confidence"]) for block in blocks if block.get("confidence") is not None]
        confidence = sum(confidence_values) / len(confidence_values) if confidence_values else None
        return {
            "ok": True,
            "text": text,
            "source": self.name,
            "provider_name": self.name,
            "quality_score": _quality_score(text=text, confidence=confidence),
            "confidence": confidence,
            "blocks": blocks,
            "warnings": [],
        }

    def _load_ocr(self):
        """首次使用时加载 PaddleOCR CPU pipeline。"""

        if self._ocr is None:
            try:
                # PaddleOCR 3.x / PaddleX 支持通过该变量选择模型下载源；默认使用百度 BOS。
                os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", self.model_source)
                from paddleocr import PaddleOCR
            except ImportError as exc:
                raise RuntimeError("缺少 paddleocr，无法执行本地 OCR。") from exc
            self._ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        return self._ocr


class LlmOcrProvider:
    """OpenAI-compatible 多模态 LLM OCR Provider。"""

    name = "llm_ocr_remote"

    def __init__(self, *, client: OpenAICompatibleLLMClient) -> None:
        """保存 LLM 客户端。"""

        self.client = client

    def extract_image(self, *, image_path: Path, page_number: int = 1) -> dict[str, Any]:
        """把单页图片发送给 LLM OCR，并要求返回 JSON 文本。"""

        mime_type = _mime_type_for_image(image_path)
        image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        parsed = self.client.complete_multimodal_json(
            system_prompt="你是 OCR 引擎。只识别图片中的文字，不解释、不扩写，返回 JSON 对象。",
            text=(
                "请按阅读顺序识别图片中的全部文字。返回字段："
                "text 字符串，confidence 0到1数字，warnings 字符串数组。"
            ),
            image_url=f"data:{mime_type};base64,{image_base64}",
        )
        text = str(parsed.get("text") or "").strip()
        confidence = float(parsed.get("confidence") or 0)
        return {
            "ok": True,
            "text": text,
            "source": self.name,
            "provider_name": self.name,
            "quality_score": _quality_score(text=text, confidence=confidence),
            "confidence": confidence,
            "blocks": [],
            "warnings": list(parsed.get("warnings") or []),
        }


class OcrService:
    """先走本地 PaddleOCR，低质量时可升级到 LLM OCR。"""

    def __init__(
        self,
        *,
        primary_provider: OcrProviderProtocol | None = None,
        fallback_provider: OcrProviderProtocol | None = None,
        fallback_quality_threshold: float = 0.68,
    ) -> None:
        """保存 Provider 与 LLM 兜底阈值。"""

        self.primary_provider = primary_provider or PaddleOcrProvider()
        self.fallback_provider = fallback_provider
        self.fallback_quality_threshold = fallback_quality_threshold

    def extract_image(self, *, image_path: Path, page_number: int = 1) -> dict[str, Any]:
        """执行单页 OCR，必要时按质量阈值调用 LLM 兜底。"""

        primary_result = self.primary_provider.extract_image(image_path=image_path, page_number=page_number)
        if (
            self.fallback_provider is None
            or float(primary_result.get("quality_score") or 0) >= self.fallback_quality_threshold
        ):
            return primary_result
        fallback_result = self.fallback_provider.extract_image(image_path=image_path, page_number=page_number)
        return {**fallback_result, "is_fallback": True, "fallback_from": primary_result.get("source")}


def build_default_ocr_service() -> OcrService:
    """按环境配置构造默认 OCR 服务。"""

    settings = get_settings()
    fallback_provider = None
    if settings.ocr_llm_enabled and settings.llm_enabled:
        fallback_provider = LlmOcrProvider(
            client=OpenAICompatibleLLMClient(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_chat_model,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        )
    return OcrService(
        primary_provider=PaddleOcrProvider(model_source=settings.ocr_paddle_model_source),
        fallback_provider=fallback_provider,
        fallback_quality_threshold=settings.ocr_llm_fallback_quality_threshold,
    )


def _paddle_result_to_blocks(result: Any) -> list[dict[str, Any]]:
    """把 PaddleOCR 输出转为统一文本块。"""

    blocks: list[dict[str, Any]] = []
    order = 0
    for page_result in result or []:
        for item in page_result or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            polygon = item[0]
            text_score = item[1]
            text = text_score[0] if isinstance(text_score, (list, tuple)) and text_score else ""
            confidence = text_score[1] if isinstance(text_score, (list, tuple)) and len(text_score) > 1 else None
            order += 1
            blocks.append(
                {
                    "text": str(text),
                    "order": order,
                    "polygon": polygon,
                    "confidence": confidence,
                    "role": "text",
                }
            )
    return blocks


def _quality_score(*, text: str, confidence: float | None) -> float:
    """用文本长度和 Provider 置信度计算轻量质量分。"""

    if not text.strip():
        return 0.0
    confidence_score = 0.6 if confidence is None else max(0.0, min(float(confidence), 1.0))
    length_score = min(len(text.strip()) / 80, 1.0)
    return round((confidence_score * 0.7) + (length_score * 0.3), 4)


def _mime_type_for_image(image_path: Path) -> str:
    """根据图片后缀推断 data URL MIME 类型。"""

    suffix = image_path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"
