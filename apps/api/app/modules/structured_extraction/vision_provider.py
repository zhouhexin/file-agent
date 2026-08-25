"""PaddleOCR-VL 本地二次识别 Provider，供 Autonomous Loop 的受控重试使用。"""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable, Protocol

from PIL import Image

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class VisionTextBlock:
    """视觉文档模型在输入图片坐标系中返回的可定位文本块。"""

    text: str
    bbox: dict[str, float]
    label: str = "text"


@dataclass(frozen=True)
class VisionRecognitionResult:
    """与具体 PaddleOCR-VL SDK 对象解耦的识别结果。"""

    blocks: list[VisionTextBlock] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class VisionRetryProviderProtocol(Protocol):
    """局部或单页视觉二次识别的最小接口。"""

    name: str
    model_name: str
    enabled: bool
    is_external: bool
    supports_unlocated_retry: bool

    def recognize(self, *, image_url: str) -> VisionRecognitionResult:
        """识别后端生成的 PNG data URL。"""


class DisabledVisionRetryProvider:
    """关闭式 Provider；不会隐式加载模型或访问网络。"""

    name = "disabled"
    model_name = "disabled"
    enabled = False
    is_external = False
    supports_unlocated_retry = False

    def recognize(self, *, image_url: str) -> VisionRecognitionResult:
        raise RuntimeError("本地视觉二次识别尚未启用。")


@lru_cache(maxsize=4)
def _load_paddleocr_vl_pipeline(
    *,
    pipeline_version: str,
    model_name: str,
    backend: str,
    device: str,
    model_source: str,
) -> Any:
    """每个 worker 进程只加载一次重量级 PaddleOCR-VL Pipeline。"""

    os.environ["PADDLE_PDX_MODEL_SOURCE"] = model_source
    if device.partition(":")[0].lower() == "cpu":
        os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "False"
    try:
        from paddleocr import PaddleOCRVL
    except ImportError as exc:
        raise RuntimeError(
            "缺少 PaddleOCR-VL Python SDK；请安装 structured-extraction 可选依赖。"
        ) from exc
    try:
        return PaddleOCRVL(
            pipeline_version=pipeline_version,
            vl_rec_model_name=model_name,
            vl_rec_backend=backend,
            device=device,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_layout_detection=True,
            use_chart_recognition=False,
            use_seal_recognition=False,
            use_ocr_for_image_block=False,
            use_queues=False,
        )
    except Exception as exc:
        raise RuntimeError("PaddleOCR-VL Pipeline 初始化失败。") from exc


class PaddleOcrVlVisionRetryProvider:
    """通过 PaddleOCR Python SDK 在本地识别裁剪区域或单页图片。"""

    name = "paddleocr_vl"
    enabled = True
    is_external = False
    supports_unlocated_retry = True

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        pipeline_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.pipeline_factory = pipeline_factory
        self.model_name = (
            f"{self.settings.paddleocr_vl_model_name}"
            f"@{self.settings.paddleocr_vl_pipeline_version}"
        )

    def recognize(self, *, image_url: str) -> VisionRecognitionResult:
        image = _decode_png_data_url(image_url)
        pipeline = (
            self.pipeline_factory(
                pipeline_version=self.settings.paddleocr_vl_pipeline_version,
                model_name=self.settings.paddleocr_vl_model_name,
                backend=self.settings.paddleocr_vl_backend,
                device=self.settings.paddleocr_vl_device,
            )
            if self.pipeline_factory is not None
            else _load_paddleocr_vl_pipeline(
                pipeline_version=self.settings.paddleocr_vl_pipeline_version,
                model_name=self.settings.paddleocr_vl_model_name,
                backend=self.settings.paddleocr_vl_backend,
                device=self.settings.paddleocr_vl_device,
                model_source=self.settings.pp_structure_model_source,
            )
        )
        try:
            import numpy as np

            outputs = pipeline.predict(
                input=np.asarray(image),
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_layout_detection=True,
                use_chart_recognition=False,
                use_seal_recognition=False,
                use_ocr_for_image_block=False,
                use_queues=False,
                temperature=0.0,
                max_new_tokens=self.settings.paddleocr_vl_max_new_tokens,
            )
            result = next(iter(outputs), None)
        except Exception as exc:
            raise RuntimeError("PaddleOCR-VL 二次识别失败。") from exc
        if result is None:
            return VisionRecognitionResult(warnings=["PADDLEOCR_VL_EMPTY_RESULT"])
        raw_blocks = _result_value(result, "parsing_res_list") or []
        blocks: list[VisionTextBlock] = []
        for raw in raw_blocks:
            text = str(_block_value(raw, "content") or "").strip()
            bbox = _normalize_bbox(_block_value(raw, "bbox"))
            if not text or bbox is None:
                continue
            blocks.append(
                VisionTextBlock(
                    text=text,
                    bbox=bbox,
                    label=str(_block_value(raw, "label") or "text")[:80],
                )
            )
        return VisionRecognitionResult(
            blocks=blocks,
            warnings=[] if blocks else ["PADDLEOCR_VL_NO_TEXT_BLOCKS"],
        )


def build_vision_retry_provider(
    *, settings: Settings | None = None
) -> VisionRetryProviderProtocol:
    """按显式配置创建本地视觉 Provider，未知值安全关闭。"""

    current = settings or get_settings()
    if current.structured_extraction_vision_provider == "paddleocr_vl":
        return PaddleOcrVlVisionRetryProvider(settings=current)
    return DisabledVisionRetryProvider()


def _decode_png_data_url(value: str) -> Image.Image:
    prefix = "data:image/png;base64,"
    if not value.startswith(prefix):
        raise ValueError("视觉二次识别只接受后端生成的 PNG data URL。")
    try:
        payload = base64.b64decode(value[len(prefix) :], validate=True)
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            return image.convert("RGB")
    except Exception as exc:
        raise ValueError("视觉二次识别图片无法安全解码。") from exc


def _result_value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    try:
        return result[key]
    except (KeyError, TypeError):
        return getattr(result, key, None)


def _block_value(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def _normalize_bbox(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return None
    values = list(value)
    if len(values) < 4:
        return None
    try:
        left, top, right, bottom = (float(item) for item in values[:4])
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return {"left": left, "top": top, "right": right, "bottom": bottom}
