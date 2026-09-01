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


def test_policy_allows_filename_only_soft_signal_during_top1_test() -> None:
    """Top-1 测试阶段文件名信号不足只保留审计特征，不再阻止落位。"""

    result = AutoPlacementPolicy(_settings()).evaluate(
        categories=[_category(matched_content_signals=[])],
        extraction_status="COMPLETED",
        risk_passed=True,
    )

    assert result.accepted is True
    assert "FILENAME_ONLY_SIGNAL" not in result.reason_codes
    assert result.feature_snapshot["content_signal_count"] == 0
    assert result.feature_snapshot["soft_gate_mode"] == "top1_test_disabled"


def test_policy_allows_negative_signal_and_small_margin_during_top1_test() -> None:
    """Top-1 测试阶段负向信号和候选间隔只记录，不再作为软拒绝条件。"""

    result = AutoPlacementPolicy(_settings()).evaluate(
        categories=[
            _category(negative_signals=["非会议材料"]),
            _category(category_id="school.admin.rules", confidence=0.88),
        ],
        extraction_status="COMPLETED",
        risk_passed=True,
    )

    assert result.accepted is True
    assert "NEGATIVE_SIGNAL_CONFLICT" not in result.reason_codes
    assert "TOP_MARGIN_TOO_SMALL" not in result.reason_codes
    assert result.feature_snapshot["negative_signal_count"] == 1
    assert round(result.top_margin or 0, 2) == 0.07


def test_policy_allows_low_score_and_summary_conflict_during_top1_test() -> None:
    """低于旧阈值且摘要/全文冲突时仍选择当前最高置信有效候选。"""

    result = AutoPlacementPolicy(_settings()).evaluate(
        categories=[
            _category(confidence=0.62, summary_fulltext_agreement=False),
            _category(category_id="school.admin.rules", confidence=0.55),
        ],
        extraction_status="COMPLETED",
        risk_passed=True,
    )

    assert result.accepted is True
    assert "TOP_SCORE_BELOW_THRESHOLD" not in result.reason_codes
    assert "TOP_MARGIN_TOO_SMALL" not in result.reason_codes
    assert "SUMMARY_FULLTEXT_CONFLICT" not in result.reason_codes
    assert result.calibrated_confidence == 0.62


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


def test_policy_accepts_scoped_other_without_text_quote() -> None:
    """已确定学校、学院或部门范围的“其他”允许按兜底规则直接形成主分类。"""

    result = AutoPlacementPolicy(_settings()).evaluate(
        categories=[
            _category(
                name="学校/其他",
                category_id="school.other",
                category_path=["学校", "其他"],
                confidence=0.52,
                status="NEEDS_REVIEW",
                source="rule_fallback",
                matched_content_signals=[],
                evidence_items=[],
            )
        ],
        extraction_status="COMPLETED",
        risk_passed=True,
    )

    assert result.accepted is True
    assert result.reason_codes == ()
