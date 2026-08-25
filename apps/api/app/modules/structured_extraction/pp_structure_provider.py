"""PP-StructureV3 Python SDK Provider 和稳定版面结构适配器。"""

from __future__ import annotations

import math
import os
from copy import deepcopy
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from app.core.config import Settings, get_settings
from app.modules.structured_extraction.schemas import (
    LayoutBoundingBox,
    LayoutElement,
    LayoutPage,
    LayoutParseResult,
)


class PpStructureUnavailableError(RuntimeError):
    """本地 PP-StructureV3 SDK 或模型不可用。"""


class LayoutParsingProviderProtocol(Protocol):
    """图片版面解析 Provider 的最小受控接口。"""

    name: str
    version: str

    def parse(self, *, file_path: Path, page_number: int | None = None) -> LayoutParseResult:
        """解析一个已授权文件并返回普通可序列化结构。"""


@lru_cache(maxsize=8)
def _load_pipeline(
    *,
    device: str,
    pipeline_config: str,
    model_source: str,
    use_doc_preprocessor: bool,
    use_table_recognition: bool,
    use_formula_recognition: bool,
    use_chart_recognition: bool,
    use_seal_recognition: bool,
    use_region_detection: bool,
    text_detection_model: str,
    text_recognition_model: str,
) -> Any:
    """按受控配置为每个 worker 进程缓存重量级 Pipeline。"""

    # 显式部署配置必须覆盖继承到进程中的旧值，否则缓存 key 与实际模型源会不一致。
    os.environ["PADDLE_PDX_MODEL_SOURCE"] = model_source
    if device.partition(":")[0].lower() == "cpu":
        # PaddleX 会在导入 flags 模块时冻结该开关；必须在首次导入 paddlex 前设置。
        # 顶层 pp_option 仍保留作为第二道约束，覆盖没有继承全局默认值的嵌套模型。
        os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "False"
    try:
        from paddlex import create_pipeline
        from paddlex.inference.models import PaddlePredictorOption
        from paddlex.inference.pipelines import load_pipeline_config
    except ImportError as exc:
        raise PpStructureUnavailableError(
            "缺少 PaddleX PP-StructureV3 Python SDK；请安装 structured-extraction 可选依赖。"
        ) from exc
    try:
        config = _configured_pipeline(
            load_pipeline_config(pipeline_config),
            use_doc_preprocessor=use_doc_preprocessor,
            use_table_recognition=use_table_recognition,
            use_formula_recognition=use_formula_recognition,
            use_chart_recognition=use_chart_recognition,
            use_seal_recognition=use_seal_recognition,
            use_region_detection=use_region_detection,
            text_detection_model=text_detection_model,
            text_recognition_model=text_recognition_model,
        )
        # Paddle 3.3.x 的 oneDNN 新执行器尚不能处理部分 PP-DocLayout 数组属性；
        # CPU 明确使用普通 Paddle 后端，避免真实推理阶段才抛 NotImplementedError。
        predictor_option = (
            PaddlePredictorOption(run_mode="paddle")
            if device.partition(":")[0].lower() == "cpu"
            else None
        )
        return create_pipeline(
            config=config,
            device=device,
            pp_option=predictor_option,
        )
    except Exception as exc:
        raise PpStructureUnavailableError("PP-StructureV3 Pipeline 初始化失败。") from exc


def _configured_pipeline(
    config: dict[str, Any],
    *,
    use_doc_preprocessor: bool = True,
    use_table_recognition: bool,
    use_formula_recognition: bool,
    use_chart_recognition: bool,
    use_seal_recognition: bool,
    use_region_detection: bool,
    text_detection_model: str = "PP-OCRv6_medium_det",
    text_recognition_model: str = "PP-OCRv6_medium_rec",
) -> dict[str, Any]:
    """复制官方配置，收敛能力并覆盖普通 OCR 与表格 OCR 的模型。"""

    configured = deepcopy(config)
    configured.update(
        {
            "use_doc_preprocessor": use_doc_preprocessor,
            "use_table_recognition": use_table_recognition,
            "use_formula_recognition": use_formula_recognition,
            "use_chart_recognition": use_chart_recognition,
            "use_seal_recognition": use_seal_recognition,
            "use_region_detection": use_region_detection,
        }
    )
    _override_general_ocr_models(
        configured,
        text_detection_model=text_detection_model,
        text_recognition_model=text_recognition_model,
    )
    return configured


def _override_general_ocr_models(
    config: dict[str, Any],
    *,
    text_detection_model: str,
    text_recognition_model: str,
) -> None:
    """只覆盖 PP-StructureV3 的通用与表格 OCR，不破坏印章等专项模型。"""

    subpipelines = config.get("SubPipelines")
    if not isinstance(subpipelines, dict):
        return
    general_ocr_configs: list[dict[str, Any]] = []
    direct = subpipelines.get("GeneralOCR")
    if isinstance(direct, dict):
        general_ocr_configs.append(direct)
    table = subpipelines.get("TableRecognition")
    if isinstance(table, dict):
        table_subpipelines = table.get("SubPipelines")
        if isinstance(table_subpipelines, dict):
            table_ocr = table_subpipelines.get("GeneralOCR")
            if isinstance(table_ocr, dict):
                general_ocr_configs.append(table_ocr)
    for general_ocr in general_ocr_configs:
        modules = general_ocr.get("SubModules")
        if not isinstance(modules, dict):
            continue
        detection = modules.get("TextDetection")
        recognition = modules.get("TextRecognition")
        if isinstance(detection, dict):
            detection["model_name"] = text_detection_model
            detection["model_dir"] = None
        if isinstance(recognition, dict):
            recognition["model_name"] = text_recognition_model
            recognition["model_dir"] = None


class PpStructureV3Provider:
    """延迟调用本地 PP-StructureV3 并隔离 SDK 版本差异。"""

    name = "pp_structure_v3"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        pipeline_factory: Callable[..., Any] | None = None,
    ) -> None:
        """允许测试注入 fake Pipeline，生产默认使用进程级缓存。"""

        self.settings = settings or get_settings()
        self.pipeline_factory = pipeline_factory
        try:
            self.version = package_version("paddlex")
        except PackageNotFoundError:
            self.version = "unavailable"

    def parse(self, *, file_path: Path, page_number: int | None = None) -> LayoutParseResult:
        """调用 SDK，并立即把所有 SDK 对象投影为稳定结构。"""

        if not file_path.is_file():
            raise ValueError("PP-StructureV3 input file does not exist")
        pipeline = (
            self.pipeline_factory(
                device=self.settings.pp_structure_device,
                pipeline_config=self.settings.pp_structure_pipeline_config,
                model_source=self.settings.pp_structure_model_source,
            )
            if self.pipeline_factory is not None
            else _load_pipeline(
                device=self.settings.pp_structure_device,
                pipeline_config=self.settings.pp_structure_pipeline_config,
                model_source=self.settings.pp_structure_model_source,
                use_doc_preprocessor=self.settings.pp_structure_use_doc_preprocessor,
                use_table_recognition=self.settings.pp_structure_use_table_recognition,
                use_formula_recognition=self.settings.pp_structure_use_formula_recognition,
                use_chart_recognition=self.settings.pp_structure_use_chart_recognition,
                use_seal_recognition=self.settings.pp_structure_use_seal_recognition,
                use_region_detection=self.settings.pp_structure_use_region_detection,
                text_detection_model=self.settings.pp_structure_text_detection_model,
                text_recognition_model=self.settings.pp_structure_text_recognition_model,
            )
        )
        try:
            outputs = pipeline.predict(
                input=str(file_path),
                use_doc_orientation_classify=getattr(
                    self.settings, "pp_structure_use_doc_preprocessor", True
                ),
                use_doc_unwarping=getattr(
                    self.settings, "pp_structure_use_doc_preprocessor", True
                ),
                use_textline_orientation=True,
            )
        except Exception as exc:
            raise RuntimeError("PP-StructureV3 版面解析失败。") from exc
        return self._normalize_outputs(outputs, page_number=page_number)

    def _normalize_outputs(
        self,
        outputs: Iterable[Any] | Any,
        *,
        page_number: int | None,
    ) -> LayoutParseResult:
        """兼容 dict、PaddleX Result 和测试 fake 的常见输出形态。"""

        raw_pages = _as_sequence(outputs)
        pages: list[LayoutPage] = []
        next_element_index = 0
        for page_position, raw_output in enumerate(raw_pages, start=1):
            data = _result_mapping(raw_output)
            actual_page_number = _resolved_page_number(
                data=data,
                requested_page_number=page_number,
                page_position=page_position,
            )
            width, height = _page_size(data)
            elements: list[LayoutElement] = []
            for raw_element in _layout_elements(data):
                element = _normalize_element(
                    raw_element,
                    element_index=next_element_index,
                )
                next_element_index += 1
                elements.append(element)
            if width is None:
                right_edges = [element.bbox.right for element in elements if element.bbox]
                width = max(1, math.ceil(max(right_edges))) if right_edges else None
            if height is None:
                bottom_edges = [element.bbox.bottom for element in elements if element.bbox]
                height = max(1, math.ceil(max(bottom_edges))) if bottom_edges else None
            pages.append(
                LayoutPage(
                    page_number=actual_page_number,
                    width=width,
                    height=height,
                    rotation=float(data.get("rotation") or 0),
                    elements=elements,
                )
            )
        return LayoutParseResult(
            provider=self.name,
            provider_version=self.version,
            pages=pages,
            warnings=[] if pages else ["PP_STRUCTURE_EMPTY_RESULT"],
        )


def _result_mapping(value: Any) -> dict[str, Any]:
    """把 PaddleX Result 的 JSON/属性投影成字典，不保留原始对象。"""

    if isinstance(value, dict):
        if isinstance(value.get("res"), dict):
            return value["res"]
        return value
    for attribute in ("json", "res", "result"):
        candidate = getattr(value, attribute, None)
        if callable(candidate):
            candidate = candidate()
        if isinstance(candidate, dict):
            if isinstance(candidate.get("res"), dict):
                return candidate["res"]
            return candidate
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        candidate = to_dict()
        if isinstance(candidate, dict):
            return candidate
    raise RuntimeError("PP-StructureV3 返回了无法识别的结果结构。")


def _as_sequence(value: Any) -> list[Any]:
    """把生成器、单个结果或结果列表统一为列表。"""

    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _page_size(data: dict[str, Any]) -> tuple[int | None, int | None]:
    """读取常见图片尺寸字段。"""

    size = data.get("input_img_shape") or data.get("page_size") or data.get("image_size")
    if isinstance(size, (list, tuple)) and len(size) >= 2:
        return int(size[1]), int(size[0])
    width = data.get("width")
    height = data.get("height")
    return (int(width) if width else None, int(height) if height else None)


def _resolved_page_number(
    *,
    data: dict[str, Any],
    requested_page_number: int | None,
    page_position: int,
) -> int:
    """兼容从 0 开始的 page_index 和从 1 开始的 page_number。"""

    if requested_page_number is not None:
        return requested_page_number
    explicit = data.get("page_number")
    if explicit is not None and explicit != "":
        return max(1, int(explicit))
    page_index = data.get("page_index")
    if page_index is not None and page_index != "":
        return max(1, int(page_index) + 1)
    return page_position


def _layout_elements(data: dict[str, Any]) -> list[dict[str, Any]]:
    """从不同 SDK 版本字段中读取版面元素与 OCR 行。"""

    for key in ("elements", "layout", "layout_parsing_result", "parsing_res_list"):
        values = data.get(key)
        if isinstance(values, list):
            normalized = [item for item in values if isinstance(item, dict)]
            if normalized:
                return normalized
        if isinstance(values, dict):
            for nested_key in ("elements", "blocks", "parsing_res_list"):
                nested = values.get(nested_key)
                if isinstance(nested, list):
                    normalized = [item for item in nested if isinstance(item, dict)]
                    if normalized:
                        return normalized
    # PP-StructureV3 可能在版面模型没有命中块时只返回 overall_ocr_res；
    # 这仍是可定位的真实 OCR 证据，不能误判成空页面。
    ocr_data = data.get("overall_ocr_res")
    if isinstance(ocr_data, dict) and isinstance(ocr_data.get("res"), dict):
        ocr_data = ocr_data["res"]
    if not isinstance(ocr_data, dict):
        ocr_data = data
    text_values = ocr_data.get("rec_texts")
    score_values = ocr_data.get("rec_scores")
    polygon_values = ocr_data.get("rec_polys")
    if polygon_values is None:
        polygon_values = ocr_data.get("dt_polys")
    texts = list(text_values) if text_values is not None else []
    scores = list(score_values) if score_values is not None else []
    polygons = list(polygon_values) if polygon_values is not None else []
    return [
        {
            "text": text,
            "confidence": scores[index] if index < len(scores) else None,
            "bbox": polygons[index] if index < len(polygons) else None,
            "label": "text",
        }
        for index, text in enumerate(texts)
    ]


def _normalize_element(raw: dict[str, Any], *, element_index: int) -> LayoutElement:
    """将单个版面元素归一化并收敛表格行列元数据。"""

    text = str(
        raw.get("text")
        or raw.get("content")
        or raw.get("block_content")
        or raw.get("rec_text")
        or ""
    )
    confidence = _optional_confidence(raw.get("confidence", raw.get("score")))
    # PaddleX 官方结果中的 polygon 是 NumPy 数组，不能参与 Python 的 ``or`` 真值运算。
    bbox = _normalize_bbox(
        next(
            (
                raw[key]
                for key in ("bbox", "block_bbox", "coordinate", "poly")
                if key in raw and raw[key] is not None
            ),
            None,
        )
    )
    element_type = str(
        raw.get("label") or raw.get("type") or raw.get("block_label") or "text"
    ).lower()
    return LayoutElement(
        element_index=element_index,
        element_type=element_type[:80] or "text",
        text=text,
        confidence=confidence,
        bbox=bbox,
        reading_order=(
            _optional_nonnegative_int(
                raw.get("reading_order", raw.get("block_order"))
            )
            or element_index
        ),
        parent_ref=str(raw.get("parent_ref") or raw.get("block_id") or "")[:200] or None,
        table_id=str(raw.get("table_id") or "")[:120] or None,
        row_start=_optional_nonnegative_int(raw.get("row_start", raw.get("row"))),
        row_end=_optional_nonnegative_int(raw.get("row_end", raw.get("row"))),
        column_start=_optional_nonnegative_int(raw.get("column_start", raw.get("col"))),
        column_end=_optional_nonnegative_int(raw.get("column_end", raw.get("col"))),
    )


def _normalize_bbox(value: Any) -> LayoutBoundingBox | None:
    """把 xyxy 或四点多边形统一为矩形。"""

    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()
    if isinstance(value, dict):
        candidates = [value.get(key) for key in ("left", "top", "right", "bottom")]
        if all(item is not None for item in candidates):
            left, top, right, bottom = (float(item) for item in candidates)
            return LayoutBoundingBox(
                left=min(left, right),
                top=min(top, bottom),
                right=max(left, right),
                bottom=max(top, bottom),
            )
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        left, top, right, bottom = (float(item) for item in value)
    else:
        points = [item for item in value if isinstance(item, (list, tuple)) and len(item) >= 2]
        if not points:
            return None
        xs = [float(item[0]) for item in points]
        ys = [float(item[1]) for item in points]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    return LayoutBoundingBox(left=left, top=top, right=right, bottom=bottom)


def _optional_nonnegative_int(value: Any) -> int | None:
    """将表格行列索引转换为非负整数。"""

    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _optional_confidence(value: Any) -> float | None:
    """忽略 SDK 中非法或越界的可选置信度，不让单个坏字段中断整页。"""

    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < 0 or parsed > 1:
        return None
    return parsed
