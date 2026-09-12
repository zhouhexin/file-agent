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
        "taxonomy_version": "2026-09-v13",
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


def test_evidence_backed_content_rule_can_become_primary():
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
    accepted_content = select_primary_category(
        taxonomy=taxonomy,
        candidates=[statement],
        input_fingerprint="statement",
    )

    assert accepted.primary_candidate["category_id"] == "college.hr.faculty-recruitment"
    assert accepted.classification_outcome == "CLASSIFIED"
    assert accepted_content.primary_candidate["category_id"] == "college.hr.faculty-recruitment"
    assert accepted_content.classification_outcome == "CLASSIFIED"
    assert accepted_content.selection_basis == "EVIDENCE_BACKED_BUSINESS_RULE"


def test_unknown_scope_is_not_treated_as_opposite_scope_conflict():
    """无组织关键词不等于检测到相反组织证据，强正文业务仍可成为主类。"""

    result = select_primary_category(
        taxonomy=load_default_taxonomy(),
        candidates=[
            _candidate(
                "college.teaching.program-accreditation",
                ["学院", "教学工作", "专业认证与教学评估"],
                organization_scope=None,
            )
        ],
        input_fingerprint="unknown-scope-with-business-evidence",
    )

    assert result.primary_candidate["category_id"] == (
        "college.teaching.program-accreditation"
    )
    assert result.classification_outcome == "CLASSIFIED"


def test_specific_title_theme_can_use_reduced_evidence_threshold():
    """标题主旨和多项正文支持允许降低门槛，避免明确人才材料进入其他。"""

    result = select_primary_category(
        taxonomy=load_default_taxonomy(),
        candidates=[
            _candidate(
                "school.hr.talent-work",
                ["学校", "人事师资", "人才工作"],
                candidate_scores={
                    "business": 0.4,
                    "scope": 0.4,
                    "title_theme": 0.22,
                    "leading_body": 0.1,
                    "evidence_support": 0.55,
                },
            )
        ],
        input_fingerprint="specific-title-theme",
    )

    assert result.primary_candidate["category_id"] == "school.hr.talent-work"
    assert result.classification_outcome == "CLASSIFIED"


def test_multiple_located_signals_can_use_evidence_support_threshold():
    """没有强标题时，多项可定位正文信号也可支持具体业务分类。"""

    result = select_primary_category(
        taxonomy=load_default_taxonomy(),
        candidates=[
            _candidate(
                "college.hr.talent-work",
                ["学院", "人事师资", "人才工作"],
                candidate_scores={
                    "business": 0.35,
                    "scope": 0.4,
                    "title_theme": 0.0,
                    "leading_body": 0.1,
                    "evidence_support": 0.55,
                },
            )
        ],
        input_fingerprint="multiple-located-signals",
    )

    assert result.primary_candidate["category_id"] == "college.hr.talent-work"
    assert result.classification_outcome == "CLASSIFIED"


def test_title_theme_advantage_avoids_background_keyword_ambiguity():
    """主标题明显占优时，正文背景中的接近候选不应触发 OTHER。"""

    talent = _candidate(
        "college.hr.talent-work",
        ["学院", "人事师资", "人才工作"],
        candidate_scores={
            "business": 0.6,
            "scope": 0.85,
            "title_theme": 0.3,
            "leading_body": 0.2,
        },
    )
    digital = _candidate(
        "college.digital-services",
        ["学院", "信息化"],
        candidate_scores={
            "business": 0.56,
            "scope": 0.85,
            "title_theme": 0.0,
            "leading_body": 0.07,
        },
    )
    result = select_primary_category(
        taxonomy=load_default_taxonomy(),
        candidates=[talent, digital],
        input_fingerprint="title-over-background",
    )

    assert result.primary_candidate["category_id"] == "college.hr.talent-work"
    assert result.classification_outcome == "CLASSIFIED"


def test_title_theme_can_outrank_slightly_higher_background_score():
    """背景候选业务分略高时，明确标题仍应成为主类而不是误落综合类别。"""

    talent = _candidate(
        "college.hr.talent-work",
        ["学院", "人事师资", "人才工作"],
        candidate_scores={
            "business": 0.38,
            "scope": 0.85,
            "title_theme": 0.26,
            "leading_body": 0.22,
            "evidence_support": 0.75,
        },
    )
    planning = _candidate(
        "college.admin.development-planning",
        ["学院", "行政综合管理", "发展规划"],
        candidate_scores={
            "business": 0.49,
            "scope": 0.85,
            "title_theme": 0.0,
            "leading_body": 0.12,
            "evidence_support": 0.6,
        },
    )

    result = select_primary_category(
        taxonomy=load_default_taxonomy(),
        candidates=[planning, talent],
        input_fingerprint="title-outranks-background",
    )

    assert result.primary_candidate["category_id"] == "college.hr.talent-work"
    assert result.classification_outcome == "CLASSIFIED"


def test_unavailable_and_low_quality_results_complete_as_other():
    """T10：无正文和低质量结果完成为 OTHER，不产生 NEEDS_REVIEW。"""

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
    ]

    assert all(item.primary_candidate["category_id"] == "system.other" for item in cases)
    assert all(item.classification_outcome == "OTHER" for item in cases)
    assert all(item.primary_candidate["status"] != "NEEDS_REVIEW" for item in cases)


def test_higher_cross_branch_candidate_wins_even_when_gap_is_below_point_ten():
    """两个证据充分分支接近时，仍以排序分更高者作为唯一 PRIMARY。"""

    result = select_primary_category(
        taxonomy=load_default_taxonomy(),
        candidates=[
            _candidate(
                "school.finance",
                ["学校", "财务"],
                candidate_scores={"business": 0.80, "scope": 0.4},
            ),
            _candidate(
                "school.audit",
                ["学校", "审计"],
                candidate_scores={"business": 0.72, "scope": 0.4},
            ),
        ],
        input_fingerprint="close-cross-branch",
    )

    assert result.primary_candidate["category_id"] == "school.finance"
    assert result.classification_outcome == "CLASSIFIED"
    assert result.reason_codes == []


def test_exact_cross_branch_tie_remains_other():
    """完全同分且没有标题主旨优势时仍属确实歧义，不任意按目录顺序落位。"""

    result = select_primary_category(
        taxonomy=load_default_taxonomy(),
        candidates=[
            _candidate(
                "school.finance",
                ["学校", "财务"],
                candidate_scores={"business": 0.8, "scope": 0.4},
            ),
            _candidate(
                "school.audit",
                ["学校", "审计"],
                candidate_scores={"business": 0.8, "scope": 0.4},
            ),
        ],
        input_fingerprint="exact-cross-branch-tie",
    )

    assert result.primary_candidate["category_id"] == "system.other"
    assert result.classification_outcome == "OTHER"
    assert "AMBIGUOUS_PRIMARY" in result.reason_codes


def test_school_scope_switches_college_business_candidate_to_school_mirror():
    """正文为学校范围时，学院人才候选必须切换到受控学校镜像节点。"""

    result = select_primary_category(
        taxonomy=load_default_taxonomy(),
        candidates=[
            _candidate(
                "college.hr.talent-work",
                ["学院", "人事师资", "人才工作"],
                organization_scope="学校",
                candidate_scores={
                    "business": 0.58,
                    "scope": 0.0,
                    "title_theme": 0.25,
                    "evidence_support": 0.75,
                },
            )
        ],
        input_fingerprint="school-scope-mirror",
    )

    assert result.primary_candidate["category_id"] == "school.hr.talent-work"
    assert result.primary_candidate["category_path"][0] == "学校"
    assert result.classification_outcome == "CLASSIFIED"


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
