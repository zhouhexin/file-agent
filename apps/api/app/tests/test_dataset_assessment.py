"""纯确定性条件判定：整栏无需编号，但声明、范围和资格必须各自成立。"""

import pytest
from app.modules.structured_datasets.assessment import (
    ApprovalScope, FieldAssessment, SourceQuote, assess_field, validate_approval, validate_approval_claim, display_value,
)


def context():
    """合成材料，不调用模型或 OCR。"""
    def ref(quote):
        """引用唯一来源区段。"""
        return {"source_unit_id": "p1", "quote": quote}
    units = [{"source_unit_id": "p1", "text": "本栏情况属实\n教务处圆章\n教学120学时\n项目2项\n获奖0项\n范围外99"}]
    approval = ApprovalScope.model_validate({"binding_method": "WHOLE_SECTION",
        "declaration": ref("本栏情况属实"), "seal": ref("教务处圆章"),
        "scope": [ref("教学120学时\n项目2项\n获奖0项")], "explanation": "同一明确栏目"})
    return ref, units, approval


def test_numbered_items_render_without_changing_source_text():
    original = "第一项\n第二项"
    presentation = {"numbered_item_texts": ["第一项", "第二项"]}
    assert display_value(original, presentation) == "1. 第一项\n2. 第二项"
    assert display_value(display_value(original, presentation), presentation) == "1. 第一项\n2. 第二项"


def test_general_paper_eligibility_checks_advisor_first_and_excluded_status():
    """导师一作需有导师关系；小修与在投必须挡在写回之前。"""
    from app.modules.structured_datasets.assessment import PaperEligibility, validate_paper_eligibility

    source = "姓名：张三\n导师：李四\n论文甲\n导师一作：李四，张三\n已正式发表"
    units = [{"source_unit_id": "unit-1", "text": source}]
    def ref(quote):
        return {"source_unit_id": "unit-1", "quote": quote}

    check = PaperEligibility.model_validate({
        "fragment_index": 0, "applicant_name": "张三",
        "applicant_evidence": ref("姓名：张三"),
        "first_author_kind": "PHD_ADVISOR", "first_author_name": "李四",
        "author_evidence": ref("导师一作：李四，张三"),
        "phd_advisor_evidence": ref("导师：李四"),
        "publication_evidence": ref("已正式发表"),
    })
    assert validate_paper_eligibility(check, SourceQuote.model_validate(ref("论文甲")), units)["first_author_name"] == "李四"
    pending = check.model_copy(update={"publication_evidence": SourceQuote.model_validate(ref("小修"))})
    units[0]["text"] += "\n小修"
    with pytest.raises(ValueError, match="PAPER_NOT_PUBLISHED"):
        validate_paper_eligibility(pending, SourceQuote.model_validate(ref("论文甲")), units)
    units[0]["text"] += "\n在投"
    unpublished = check.model_copy(update={"publication_evidence": SourceQuote.model_validate(ref("在投"))})
    with pytest.raises(ValueError, match="PAPER_NOT_PUBLISHED"):
        validate_paper_eligibility(unpublished, SourceQuote.model_validate(ref("论文甲")), units)
    second_author = check.model_copy(update={"first_author_name": "张三",
                                      "author_evidence": SourceQuote.model_validate(ref("导师一作：李四，张三")),
                                      "phd_advisor_evidence": SourceQuote.model_validate(ref("导师：李四"))})
    with pytest.raises(ValueError, match="PAPER_AUTHOR_EVIDENCE_INVALID"):
        validate_paper_eligibility(second_author, SourceQuote.model_validate(ref("论文甲")), units)


def test_general_paper_eligibility_accepts_early_access_journal_only_and_student_first():
    """仅缺发表细节不排除，学生一作不需要再核对导师身份。"""
    from app.modules.structured_datasets.assessment import PaperEligibility, validate_paper_eligibility

    source = ("姓名：邓静怡\n导师：蔺琛皓教授\n"
              "Jingyi Deng, Chenhao Lin. Artifact-Invariant Detection[J]. TIFS, Early Access.\n"
              "Jingyi Deng, Chenhao Lin. A Survey of Defenses[J]. ACM Computing Surveys.\n"
              "蔺琛皓, 邓静怡（学⽣⼀作）. 虚假数字人脸内容生成与检测技术[J]. 计算机学报, 2023")
    units = [{"source_unit_id": "unit-1", "text": source}]
    def ref(quote):
        return {"source_unit_id": "unit-1", "quote": quote}

    for title, author, applicant in [
        ("Artifact-Invariant Detection", "Jingyi Deng, Chenhao Lin. Artifact-Invariant Detection[J]. TIFS, Early Access.", "Jingyi Deng"),
        ("A Survey of Defenses", "Jingyi Deng, Chenhao Lin. A Survey of Defenses[J]. ACM Computing Surveys.", "Jingyi Deng"),
        ("虚假数字人脸内容生成与检测技术", "邓静怡（学⽣⼀作）", "邓静怡"),
    ]:
        check = PaperEligibility.model_validate({
            "fragment_index": 0, "applicant_name": applicant,
            "applicant_evidence": ref("姓名：邓静怡" if applicant == "邓静怡" else author),
            "first_author_kind": "APPLICANT", "first_author_name": applicant,
            "author_evidence": ref(author),
        })
        assert validate_paper_eligibility(check, SourceQuote.model_validate(ref(title)), units)["first_author_name"] == applicant


def test_general_paper_eligibility_requires_explicit_advisor_first_and_rejects_masters_advisor():
    """只有明确导师一作才检查导师，硕士导师不能冒充博士导师。"""
    from app.modules.structured_datasets.assessment import PaperEligibility, validate_paper_eligibility

    source = "姓名：张三\n导师：李四\n硕士导师：王五\n论文甲\n李四，张三\n导师一作：李四"
    units = [{"source_unit_id": "unit-1", "text": source}]
    def ref(quote):
        return {"source_unit_id": "unit-1", "quote": quote}
    base = {"fragment_index": 0, "applicant_name": "张三", "applicant_evidence": ref("姓名：张三"),
            "first_author_kind": "PHD_ADVISOR", "first_author_name": "李四",
            "author_evidence": ref("导师一作：李四"), "phd_advisor_evidence": ref("导师：李四")}
    check = PaperEligibility.model_validate(base)
    assert validate_paper_eligibility(check, SourceQuote.model_validate(ref("论文甲")), units)
    no_label = check.model_copy(update={"author_evidence": SourceQuote.model_validate(ref("李四，张三"))})
    with pytest.raises(ValueError, match="PAPER_ADVISOR_EVIDENCE_INVALID"):
        validate_paper_eligibility(no_label, SourceQuote.model_validate(ref("论文甲")), units)
    masters = check.model_copy(update={"first_author_name": "王五",
        "author_evidence": SourceQuote.model_validate(ref("导师一作：王五")),
        "phd_advisor_evidence": SourceQuote.model_validate(ref("硕士导师：王五"))})
    units[0]["text"] += "\n导师一作：王五"
    with pytest.raises(ValueError, match="PAPER_ADVISOR_EVIDENCE_INVALID"):
        validate_paper_eligibility(masters, SourceQuote.model_validate(ref("论文甲")), units)


def test_general_paper_eligibility_distinguishes_names_in_shared_advisor_list():
    """多人导师列表中的硕士导师标记必须覆盖该标记下的所有姓名。"""
    from app.modules.structured_datasets.assessment import PaperEligibility, validate_paper_eligibility

    source = "姓名：张三\n硕士导师：王五、李四；博士导师：赵六\n论文甲\n导师一作：李四\n导师一作：赵六"
    units = [{"source_unit_id": "unit-1", "text": source}]
    def ref(quote):
        return {"source_unit_id": "unit-1", "quote": quote}
    base = {"fragment_index": 0, "applicant_name": "张三", "applicant_evidence": ref("姓名：张三"),
            "first_author_kind": "PHD_ADVISOR"}
    master = PaperEligibility.model_validate({**base, "first_author_name": "李四",
        "author_evidence": ref("导师一作：李四"), "phd_advisor_evidence": ref("硕士导师：王五、李四；博士导师：赵六")})
    with pytest.raises(ValueError, match="PAPER_ADVISOR_EVIDENCE_INVALID"):
        validate_paper_eligibility(master, SourceQuote.model_validate(ref("论文甲")), units)
    doctoral = PaperEligibility.model_validate({**base, "first_author_name": "赵六",
        "author_evidence": ref("导师一作：赵六"), "phd_advisor_evidence": ref("硕士导师：王五、李四；博士导师：赵六")})
    assert validate_paper_eligibility(doctoral, SourceQuote.model_validate(ref("论文甲")), units)


def test_optional_branches_and_mandatory_branch():
    """3选1与至少2项/必选组合不被误判成所有分支必须通过。"""
    ref, units, approval = context()
    title = "教学至少80学时；项目至少2项；获奖至少1项"
    groups = [{"checks": [{"criterion": criterion, "operator": "GTE", "expected": expected,
        "observed": observed, "evidence": [ref(quote)]}]} for criterion, expected, observed, quote in [
        ("教学至少80学时", "80", "120", "教学120学时"),
        ("项目至少2项", "2", "2", "项目2项"),
        ("获奖至少1项", "1", "0", "获奖0项")]]
    for mode, minimum, required, expected in [("ANY", 1, [], True), ("ALL", 1, [], False),
            ("AT_LEAST", 2, [0], True), ("AT_LEAST", 2, [2], False)]:
        result = assess_field(label=title, approval=approval, units=units,
            assessment=FieldAssessment(title=title, groups=groups, mode=mode, minimum=minimum, required_groups=required))
        assert result["checkmark"] is expected
        value = display_value("教学120学时；项目2项", result)
        assert value.endswith(" √") is expected
        assert display_value(value, result) == value


@pytest.mark.parametrize("negative", ["本栏情况不属实", "本栏未通过", "本栏待确认"])
def test_negative_approval_is_not_success(negative):
    """不能因包含关键词而把否定声明当批准。"""
    _, units, approval = context()
    units[0]["text"] = units[0]["text"].replace("本栏情况属实", negative)
    approval.declaration.quote = negative
    with pytest.raises(ValueError, match="APPROVAL_DECLARATION_REQUIRED"):
        validate_approval(approval, units)


def test_condition_outside_scope_rejected():
    """同页范围外数值不能用作标题通过依据。"""
    ref, units, approval = context()
    title = "至少80"
    assessment = FieldAssessment(title=title, groups=[{"checks": [{"criterion": title, "operator": "GTE",
        "expected": "80", "observed": "99", "evidence": [ref("范围外99")]}]}])
    with pytest.raises(ValueError, match="APPROVAL_SCOPE_MISMATCH"):
        assess_field(label=title, approval=approval, units=units, assessment=assessment)


@pytest.mark.parametrize("expected,observed,operator", [("8", "120", "GTE"), ("80", "20", "GTE"), ("80", "120", "LTE")])
def test_numeric_substrings_and_reversed_operator_rejected(expected, observed, operator):
    """拒绝截取标题/观测数值或反转比较方向以制造通过。"""
    ref, units, approval = context()
    title = "教学至少80学时"
    assessment = FieldAssessment(title=title, groups=[{"checks": [{"criterion": title, "operator": operator,
        "expected": expected, "observed": observed, "evidence": [ref("教学120学时")]}]}])
    with pytest.raises(ValueError, match="REQUIREMENT_VALUE_INVALID"):
        assess_field(label=title, approval=approval, units=units, assessment=assessment)


def test_hr_annual_excellence_requires_located_seal_block():
    """L 列年度优秀可没有“属实/章”字样，但必须来自同页真实坐标印章区块。"""
    quote = {"source_unit_id": "p25", "quote": "年度考核优秀"}
    approval = ApprovalScope.model_validate({"binding_method": "WHOLE_SECTION",
        "approval_kind": "HR_ANNUAL_EXCELLENCE", "declaration": quote, "seal": quote,
        "scope": [{"source_unit_id": "p25", "quote": "2022年度考核优秀"}],
        "explanation": "年度考核栏内方章"})
    units = [{"source_unit_id": "p25", "kind": "PDF_PAGE", "page_number": 25,
        "text": "2022年度考核优秀", "seal_blocks": []}]
    with pytest.raises(ValueError, match="APPROVAL_SEAL_REQUIRED"):
        validate_approval(approval, units)
    units[0]["seal_blocks"] = [{"text": "2022年度考核优秀", "start": 0, "end": 10,
        "bbox": {"left": 0.1, "top": 0.2, "right": 0.3, "bottom": 0.4}}]
    validate_approval(approval, units)
    units[0]["seal_blocks"][0]["text"] = "普通打印文字"
    with pytest.raises(ValueError, match="APPROVAL_SEAL_REQUIRED"):
        validate_approval(approval, units)


def test_hr_annual_excellence_preflight_rejects_different_declaration():
    """方章里的优秀结论不能由邻近打印声明替代。"""
    approval = ApprovalScope.model_validate({
        "binding_method": "WHOLE_SECTION", "approval_kind": "HR_ANNUAL_EXCELLENCE",
        "declaration": {"source_unit_id": "p25", "quote": "2024年度考核优秀"},
        "seal": {"source_unit_id": "p25", "quote": "2022年度考核优秀"},
        "scope": [{"source_unit_id": "p25", "quote": "2022年度考核优秀"}],
        "explanation": "年度考核栏内方章",
    })
    with pytest.raises(ValueError, match="APPROVAL_DECLARATION_REQUIRED"):
        validate_approval_claim(approval)


def test_source_only_display_marks_only_specific_fragment():
    """无章事实只在自身片段尾部加状态，不污染原文或整格末尾资格勾。"""
    presentation = {"source_only_fragment_indexes": [1], "source_status": "MIXED"}
    raw = "教学工作量120学时\n教学效果优秀"
    expected = "教学工作量120学时\n教学效果优秀〔PDF 原文，未见对应印章〕"
    assert display_value(raw, presentation) == expected
    assert display_value(expected, presentation) == expected
