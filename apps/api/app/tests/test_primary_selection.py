"""唯一 PRIMARY 与 OTHER 完成语义测试。"""

from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.primary_selection import select_primary_category


def _candidate(category_id: str, path: list[str], **overrides) -> dict:
    """构造具有真实定位证据的确定性候选。"""

    value = {
        "name": "/".join(path),
        "category_id": category_id,
        "category_path": path,
        "taxonomy_key": "unified_school_file_classification",
        "taxonomy_version": "2026-09-v10",
        "status": "SUGGESTED",
        "source": "rule",
        "purpose_basis": "VERSIONED_RULE",
        "organization_scope": path[0] if path[0] in {"学校", "学院"} else None,
        "candidate_scores": {"business": 0.8, "scope": 0.4},
        "evidence_items": [
            {
                "type": "text_quote",
                "page_number": 1,
                "sheet_name": None,
                "quote": "正文中的业务证据",
                "signals": ["业务证据"],
                "source": "rule",
            }
        ],
    }
    value.update(overrides)
    return value


def test_true_resume_can_win_but_research_statement_alone_cannot():
    """T04：完整简历规则可成为主类，只有 Research Statement 题名时必须 OTHER。"""

    taxonomy = load_default_taxonomy()
    resume = _candidate(
        "college.hr.faculty-recruitment",
        ["学院", "人事师资", "师资招聘"],
        purpose_basis="LOCAL_RESUME_WINDOW",
    )
    statement = _candidate(
        "college.hr.faculty-recruitment",
        ["学院", "人事师资", "师资招聘"],
        purpose_basis="CONTENT_RULE",
    )

    accepted = select_primary_category(
        taxonomy=taxonomy,
        candidates=[resume],
        input_fingerprint="resume",
    )
    rejected = select_primary_category(
        taxonomy=taxonomy,
        candidates=[statement],
        input_fingerprint="statement",
    )

    assert accepted.primary_candidate["category_id"] == "college.hr.faculty-recruitment"
    assert accepted.classification_outcome == "CLASSIFIED"
    assert rejected.primary_candidate["category_id"] == "system.other"
    assert rejected.classification_outcome == "OTHER"


def test_unavailable_low_quality_and_ambiguous_results_complete_as_other():
    """T10：无正文、低质量和跨业务歧义均完成为 OTHER，不产生 NEEDS_REVIEW。"""

    taxonomy = load_default_taxonomy()
    finance = _candidate(
        "school.finance",
        ["学校", "财务"],
        candidate_scores={"business": 0.8, "scope": 0.4},
    )
    audit = _candidate(
        "school.audit",
        ["学校", "审计"],
        candidate_scores={"business": 0.72, "scope": 0.4},
    )
    cases = [
        select_primary_category(
            taxonomy=taxonomy,
            candidates=[],
            input_fingerprint="missing",
            extraction_status="FAILED",
        ),
        select_primary_category(
        taxonomy=taxonomy,
        candidates=[
            {
                **finance,
                "purpose_basis": "CONTENT_RULE",
                "candidate_scores": {
                    "business": 0.4,
                    "scope": 0.4,
                    "evidence_support": 0.1,
                },
            }
        ],
            input_fingerprint="low",
        ),
        select_primary_category(
            taxonomy=taxonomy,
            candidates=[finance, audit],
            input_fingerprint="ambiguous",
        ),
    ]

    assert all(item.primary_candidate["category_id"] == "system.other" for item in cases)
    assert all(item.classification_outcome == "OTHER" for item in cases)
    assert all(item.primary_candidate["status"] != "NEEDS_REVIEW" for item in cases)


def test_explicit_target_wins_and_background_preserves_human_primary():
    """T11：本轮明确目标优先；没有新目标时后台建议不得覆盖人工主类。"""

    taxonomy = load_default_taxonomy()
    finance = _candidate("school.finance", ["学校", "财务"])
    audit = _candidate("school.audit", ["学校", "审计"])
    explicit = select_primary_category(
        taxonomy=taxonomy,
        candidates=[finance],
        explicit_target={**audit, "revision": 3},
        existing_human_primary={**finance, "revision": 2},
        input_fingerprint="explicit",
    )
    background = select_primary_category(
        taxonomy=taxonomy,
        candidates=[audit],
        existing_human_primary={**finance, "revision": 2},
        input_fingerprint="human",
    )

    assert explicit.primary_candidate["category_id"] == "school.audit"
    assert explicit.selection_basis == "EXPLICIT_TARGET"
    assert background.primary_candidate["category_id"] == "school.finance"
    assert background.selection_basis == "EXISTING_HUMAN_PRIMARY"
    assert background.secondary_candidates[0]["category_id"] == "school.audit"
