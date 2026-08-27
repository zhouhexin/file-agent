"""置信门控首次自动分类策略测试。"""

from app.core.config import Settings
from app.modules.classification.auto_placement_policy import AutoPlacementPolicy


def _settings(**overrides) -> Settings:
    """构造不依赖外部服务的策略测试配置。"""

    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        auto_classification_fallback_threshold=0.9,
        auto_classification_fallback_margin=0.2,
        **overrides,
    )


def _category(**overrides) -> dict:
    """生成具有稳定 taxonomy 身份和可定位正文证据的高可靠候选。"""

    category = {
        "name": "学校/行政综合管理类/会议纪要",
        "category_id": "school.admin.meeting-minutes",
        "category_path": ["学校", "行政综合管理类", "会议纪要"],
        "taxonomy_key": "school-file-classification",
        "taxonomy_version": "v2",
        "confidence": 0.95,
        "status": "SUGGESTED",
        "source": "rule",
        "matched_content_signals": ["会议纪要", "研究决定", "议题"],
        "negative_signals": [],
        "summary_fulltext_agreement": True,
        "evidence_items": [
            {
                "type": "text_quote",
                "page_number": 1,
                "sheet_name": None,
                "quote": "会议研究决定通过本次议题",
                "signals": ["研究决定"],
                "source": "rule",
            }
        ],
    }
    category.update(overrides)
    return category


def test_policy_accepts_only_unique_high_confidence_content_candidate() -> None:
    """高分、明显间隔、多正文信号且证据可定位时才能自动选择主分类。"""

    result = AutoPlacementPolicy(_settings()).evaluate(
        categories=[_category(), _category(category_id="school.admin.rules", confidence=0.60)],
        extraction_status="COMPLETED",
        risk_passed=True,
    )

    assert result.accepted is True
    assert result.evaluated_decision == "AUTO_ORGANIZED"
    assert result.reason_codes == ()
    assert result.top_margin == 0.35


def test_policy_rejects_filename_only_candidate_even_with_high_score() -> None:
    """文件名命中不能冒充正文证据，高分也必须主动拒识。"""

    result = AutoPlacementPolicy(_settings()).evaluate(
        categories=[_category(matched_content_signals=[])],
        extraction_status="COMPLETED",
        risk_passed=True,
    )

    assert result.accepted is False
    assert "FILENAME_ONLY_SIGNAL" in result.reason_codes


def test_policy_rejects_negative_signal_and_small_margin() -> None:
    """负向冲突或 Top1/Top2 过近均不能选择最接近的目录。"""

    result = AutoPlacementPolicy(_settings()).evaluate(
        categories=[
            _category(negative_signals=["非会议材料"]),
            _category(category_id="school.admin.rules", confidence=0.88),
        ],
        extraction_status="COMPLETED",
        risk_passed=True,
    )

    assert result.accepted is False
    assert "NEGATIVE_SIGNAL_CONFLICT" in result.reason_codes
    assert "TOP_MARGIN_TOO_SMALL" in result.reason_codes


def test_policy_rejects_unlocated_evidence_and_parse_failure() -> None:
    """无法定位页码/Sheet 的引用和解析失败都进入复核。"""

    result = AutoPlacementPolicy(_settings()).evaluate(
        categories=[
            _category(
                evidence_items=[
                    {
                        "type": "text_quote",
                        "page_number": None,
                        "sheet_name": None,
                        "quote": "会议研究决定",
                    }
                ]
            )
        ],
        extraction_status="FAILED",
        risk_passed=True,
    )

    assert result.accepted is False
    assert "PARSE_FAILED" in result.reason_codes
    assert "EVIDENCE_MISSING" in result.reason_codes
