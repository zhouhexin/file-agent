"""未分类工作副本维护脚本的 v10 分类边界测试。"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.config import Settings
from app.modules.classification.auto_placement_policy import AutoPlacementPolicy
from app.scripts.reclassify_unclassified_working_copies import (
    _can_apply_persisted_primary,
    _missing_extraction_other_result,
    _summary,
)


def _settings() -> Settings:
    """构造不访问外部服务的最小策略配置。"""

    return Settings(database_url="sqlite+pysqlite:///:memory:")


def test_parse_failure_other_is_applied_instead_of_classification_review() -> None:
    """解析失败的 Other 是已接受降级，不能被旧脚本重新标记为待复核。"""

    policy_result = AutoPlacementPolicy(_settings()).evaluate(
        categories=[
            {
                "category_id": "system.other",
                "category_path": ["其他"],
                "taxonomy_key": "school-file-classification",
                "taxonomy_version": "2026-09-v10",
                "status": "SUGGESTED",
                "source": "system_fallback",
                "evidence_items": [],
            }
        ],
        extraction_status="FAILED",
        risk_passed=True,
    )

    assert policy_result.accepted is True
    assert "PARSE_FAILED" in policy_result.reason_codes
    assert _can_apply_persisted_primary(
        policy_result=policy_result,
        suggestion=SimpleNamespace(category_id="system.other"),
    )


def test_missing_extraction_creates_system_other_primary_candidate() -> None:
    """没有历史正文时仍要形成持久化 Other 建议，不能留下空分类结果。"""

    result = _missing_extraction_other_result(
        working_copy=SimpleNamespace(
            document_id="document-1",
            current_version_id="version-1",
            workspace_id="workspace-1",
            filename="未知材料.pdf",
        )
    )

    assert result["extraction_status"] == "FAILED"
    assert result["classification_outcome"] == "OTHER"
    assert result["categories"][0]["category_id"] == "system.other"
    assert result["categories"][0]["relation_role"] == "PRIMARY"
    assert result["errors"][0]["code"] == "EXTRACTION_NOT_FOUND"


def test_summary_uses_other_and_skipped_without_new_review_count() -> None:
    """脚本回执只能报告已归入 Other 和真实运行跳过，不能再制造分类复核计数。"""

    summary = _summary(
        mode="apply",
        previews=[
            {"status": "CLASSIFIED", "classification_outcome": "OTHER"},
            {"status": "CLASSIFIED", "classification_outcome": "CLASSIFIED"},
            {"status": "SKIPPED", "classification_outcome": "OTHER"},
        ],
    )

    assert summary["classified_count"] == 2
    assert summary["other_count"] == 1
    assert summary["skipped_count"] == 1
    assert "needs_review_count" not in summary
