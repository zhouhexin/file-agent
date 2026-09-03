"""分类体系关键词匹配测试。"""

import pytest

from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.matcher import (
    DocumentFeatures,
    apply_unclassified_fallback,
    match_document_features,
    match_document_text,
    recall_category_candidates,
)


def test_matcher_returns_specific_school_category_path():
    """正文命中子分类名称时，应返回包含学校一级域的完整分类路径。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("本文件涉及教师职称申报材料。", taxonomy)

    assert matches[0]["name"] == "学校/人事师资/职称"
    assert matches[0]["category_path"] == ["学校", "人事师资", "职称"]
    assert matches[0]["taxonomy_key"] == "unified_school_file_classification"
    assert matches[0]["taxonomy_version"] == "2026-09-v8"
    assert "职称" in matches[0]["evidence"]


def test_matcher_prefers_longer_category_name():
    """同时可能命中短词和长词时，应优先返回更具体的长分类名称。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("请归档学院财务管理相关制度。", taxonomy)

    assert matches[0]["category_path"] == ["学院", "财务管理"]
    assert "财务管理" in matches[0]["evidence"]
    assert ["学校", "财务"] not in [item["category_path"] for item in matches]


def test_matcher_returns_multiple_categories_sorted_and_deduped():
    """单个文件命中多个分类时，应返回多个去重后的分类建议并按置信度排序。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("学校教师职称材料，同时包含干部工作和会议纪要。", taxonomy)

    paths = [item["category_path"] for item in matches]
    confidences = [item["confidence"] for item in matches]
    assert ["学校", "人事师资", "职称"] in paths
    assert ["学校", "党委相关", "干部工作"] in paths
    assert ["学校", "行政综合管理类", "会议纪要"] in paths
    assert len(paths) == len({tuple(path) for path in paths})
    assert confidences == sorted(confidences, reverse=True)


def test_matcher_returns_other_when_no_taxonomy_keywords_match():
    """无法命中配置分类时，应返回带 taxonomy 信息的其他分类。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("这是一段无法判断归类的普通文本。", taxonomy)

    assert matches == [
        {
            "name": "其他",
            "category_path": ["其他"],
            "confidence": 0.2,
            "status": "SUGGESTED",
            "evidence": [],
            "taxonomy_key": "unified_school_file_classification",
            "taxonomy_version": "2026-09-v8",
        }
    ]


def test_recall_candidates_uses_aliases_and_positive_signals_for_implicit_topic():
    """即使正文未出现标准分类名，也应通过别名和正向信号召回正确候选。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        filename="2025年教师岗位聘期考核工作安排.docx",
        title="教师岗位聘期考核工作安排",
        full_text="请各学院组织专任教师完成岗位续聘材料提交和聘期考核结果确认。",
    )

    candidates = recall_category_candidates(features, taxonomy, limit=5)

    assert candidates[0].category_id == "school.hr.appointment-assessment"
    assert candidates[0].category_path == ["学校", "人事师资", "考核聘任"]
    assert {"教师", "岗位", "聘期", "考核", "续聘"} & set(candidates[0].matched_signals)
    assert candidates[0].rule_score > 0
    assert "标题" in candidates[0].candidate_reason or "正文" in candidates[0].candidate_reason


def test_teacher_signal_alone_does_not_recall_appointment_assessment():
    """“教师”只能作为人事师资弱信号，不能单独命中考核聘任。"""

    candidates = recall_category_candidates(
        DocumentFeatures(
            filename="教师信息表.xlsx",
            full_text="姓名 工号 所属部门 教师类别",
        ),
        load_default_taxonomy(),
        limit=8,
    )

    assert all(
        candidate.category_id not in {
            "school.hr.appointment-assessment",
            "college.hr.appointment-assessment",
        }
        for candidate in candidates
    )


def test_spreadsheet_function_tutorial_uses_default_organization_fallback():
    """函数教程中的示例人员表不得按样例正文落入业务分类。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        filename="vlookup+match 函数使用.xlsx",
        full_text="姓名 工号 职称时间（聘任时间） 岗位分级 教师六级",
    )
    matches = match_document_features(features, taxonomy)
    matches = apply_unclassified_fallback(
        document_features=features,
        taxonomy=taxonomy,
        matches=matches,
        default_organization_root="学院",
    )

    assert matches[0]["category_id"] == "college.other"
    assert matches[0]["category_path"] == ["学院", "其他"]


def test_matcher_uses_document_number_department_as_parent_category_signal():
    """文号中的“人事”应召回学校人事师资父分类。"""

    taxonomy = load_default_taxonomy()
    matches = match_document_text(
        "西安理工人事〔2022〕14号\n关于崔杰等21位同志任职资格的通知",
        taxonomy,
    )

    hr_category = next(item for item in matches if item["category_path"] == ["学校", "人事师资"])
    assert "人事" in hr_category["evidence"]


def test_unified_taxonomy_classifies_school_appointment_notice_without_college_duplicate():
    """统一 taxonomy 应识别校级职称材料，并抑制同名学院候选。"""

    taxonomy = load_default_taxonomy()
    matches = match_document_text(
        "工程师资格-西理人事[2022]14号.PDF\n"
        "西安理工人事〔2022〕14号\n"
        "关于崔杰等21位同志任职资格的通知\n"
        "专业技术职务任职资格",
        taxonomy,
    )

    category_ids = {item["category_id"] for item in matches}
    assert "school.hr.title-review" in category_ids
    assert "school.hr" in category_ids
    assert "college.hr.title-review" not in category_ids


def test_recall_candidates_penalizes_negative_signals():
    """负向信号应降低冲突分类分数，避免奖学金文本误归入教师考核聘任。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        filename="学生奖学金志愿服务证明.docx",
        title="学生奖学金志愿服务证明",
        full_text="本材料用于学生奖学金评审和志愿服务时长证明，不涉及教师岗位聘任。",
    )

    candidates = recall_category_candidates(features, taxonomy, limit=8)
    appointment = next(item for item in candidates if item.category_id == "school.hr.appointment-assessment")
    student = next(item for item in candidates if item.category_id == "college.student-affairs")

    assert {"奖学金", "志愿服务"} & set(appointment.negative_signals)
    assert student.rule_score > appointment.rule_score


def test_recall_candidates_respects_limit_and_stable_sorting():
    """候选召回应按分数排序并遵守调用方给出的数量上限。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        title="学校学院教师科研财务会议纪要年度总结",
        full_text="材料同时涉及学校、学院、教师、科研、财务、会议纪要、年度总结等多个主题。",
    )

    candidates = recall_category_candidates(features, taxonomy, limit=3)

    assert len(candidates) == 3
    assert [item.rule_score for item in candidates] == sorted(
        [item.rule_score for item in candidates],
        reverse=True,
    )


def test_recall_candidates_prefers_college_talent_work_for_college_plan():
    """学院人才引育计划应优先召回学院级人才工作，而不是学校级同名分类。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        filename="计算机学院2024年高层次人才引育计划.docx",
        title="计算机科学与工程学院2024年高层次人才引育计划",
        full_text=(
            "学院始终将人才引育工作作为学院重要工作，学院计划柔性引进国家级人才，"
            "并建立人才队伍台账。"
        ),
    )

    candidates = recall_category_candidates(features, taxonomy, limit=8)
    college = next(item for item in candidates if item.category_id == "college.hr.talent-work")
    school = next(
        (item for item in candidates if item.category_id == "school.hr.talent-work"),
        None,
    )

    assert candidates[0].category_id == "college.hr.talent-work"
    assert school is None
    assert {"高层次人才引育计划", "人才引育工作", "人才队伍台账"} & set(
        college.matched_signals
    )


def test_recall_candidates_prefers_college_talent_work_for_necessity_report():
    """学院提出人才引进需求的必要性报告应归入学院级人才工作。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        filename="计算机学院2023年高层次人才引进必要性分析报告.docx",
        title="计算机科学与工程学院高层次人才引进必要性分析报告",
        full_text="根据学院发展需要，学院拟引进领军人才，以支持学院学科建设。",
    )

    candidates = recall_category_candidates(features, taxonomy, limit=8)
    college = next(item for item in candidates if item.category_id == "college.hr.talent-work")
    school = next(
        (item for item in candidates if item.category_id == "school.hr.talent-work"),
        None,
    )

    assert candidates[0].category_id == "college.hr.talent-work"
    assert school is None
    assert "人才引进必要性" in college.matched_signals
    assert "学院拟引进" in college.matched_signals


def test_recall_candidates_keeps_school_talent_notice_at_school_level():
    """面向全校发布的人才工作通知不应因学院级规则扩展而改判为学院文件。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        filename="关于做好学校高层次人才引进工作的通知.docx",
        title="关于做好学校高层次人才引进工作的通知",
        full_text="人事处面向全校各单位发布高层次人才引进工作通知。",
    )

    candidates = recall_category_candidates(features, taxonomy, limit=8)
    school = next(item for item in candidates if item.category_id == "school.hr.talent-work")
    college = next(
        (item for item in candidates if item.category_id == "college.hr.talent-work"),
        None,
    )

    assert candidates[0].category_id == "school.hr.talent-work"
    assert college is None or school.rule_score > college.rule_score


def test_recall_candidates_recognizes_workdata_college_talent_filenames():
    """来自 workdata 的稳定学院人才文种在仅有文件名时也应优先召回学院级分类。"""

    taxonomy = load_default_taxonomy()
    filenames = [
        "计算机学院2024年高层次人才引育计划.docx",
        "计算机学院2023年高层次人才引进必要性报告.docx",
        "计算机科学与工程学院关于柔性引进高层次人才的报告.docx",
        "计算机学院人才工作自评表.docx",
        "西安理工大学海外人才需求名单-计算机学院.xlsx",
        "西安理工大学2024年拟引进高层次人才候选人统计表-计算机学院.xls",
    ]

    for filename in filenames:
        candidates = recall_category_candidates(
            DocumentFeatures(filename=filename, title=filename),
            taxonomy,
            limit=8,
        )

        assert candidates[0].category_id == "college.hr.talent-work", filename


def test_recall_candidates_prefers_college_salary_for_workdata_self_check_form():
    """学院工资津贴奖金补助自查表必须召回学院劳资，不得再由“学院”泛词落到发展规划。"""

    taxonomy = load_default_taxonomy()
    filename = "计算机学院规范工资津贴补贴外各项奖金津贴补助发放情况自查表20180622.doc"
    candidates = recall_category_candidates(
        DocumentFeatures(
            filename=filename,
            title=filename,
            full_text=(
                "填报单位：计算机科学与工程学院。表中包括奖金项目、发放对象、"
                "发放标准、经费来源和发放情况。"
            ),
        ),
        taxonomy,
        limit=8,
    )

    assert candidates[0].category_id == "college.hr.salary-social-security"
    assert {
        "工资津贴补贴",
        "奖金津贴补助",
        "发放情况自查",
    } & set(candidates[0].matched_signals)
    assert all(
        item.category_id != "college.admin.development-planning"
        for item in candidates
    )


def test_recall_candidates_resolves_school_scope_before_salary_business_topic():
    """面向全校的工资自查通知必须先进入学校分支，不得被学院长短语截走。"""

    taxonomy = load_default_taxonomy()
    filename = "关于开展全校工资津贴补贴发放情况自查的通知20180628.pdf"
    candidates = recall_category_candidates(
        DocumentFeatures(
            filename=filename,
            title="关于开展全校工资津贴补贴发放情况自查的通知",
            full_text=(
                "通知 校属各单位：根据陕西省教育厅办公室转发省审计厅、省财政厅、"
                "省人社厅关于全省行政事业单位工资津贴补贴发放情况自查工作的通知，"
                "请在全校开展监督检查。"
            ),
        ),
        taxonomy,
        limit=8,
    )

    assert candidates[0].category_id == "school.audit"
    assert candidates[0].organization_scope == "学校"
    assert candidates[0].organization_score > 0
    assert {"全校", "校属各单位"} & set(candidates[0].matched_signals)
    assert any(
        item.category_id == "school.hr.salary-social-security"
        for item in candidates
    )
    assert all(not str(item.category_id).startswith("college.") for item in candidates)


def test_recall_candidates_recognizes_added_college_hr_categories():
    """workdata 中教师发展、师资招聘和博士后文种应进入新增或补全的学院人事分类。"""

    taxonomy = load_default_taxonomy()
    cases = {
        "计算机学院近三年教师进修情况统计表.xlsx": "college.hr.faculty-development",
        "计算机学院师资招聘面试试讲安排.docx": "college.hr.faculty-recruitment",
        "计算机学院博士后流动站年度工作报告.docx": "college.hr.postdoc",
    }

    for filename, expected_category_id in cases.items():
        candidates = recall_category_candidates(
            DocumentFeatures(filename=filename, title=filename),
            taxonomy,
            limit=8,
        )

        assert candidates[0].category_id == expected_category_id, filename


def test_recruitment_resume_context_overrides_incidental_experience_topics():
    """应聘目录中的简历应按文档用途归类，不能被经历字段拆散。"""

    candidates = recall_category_candidates(
        DocumentFeatures(
            filename="李小和简历.doc",
            full_text=(
                "个人简历\n姓名：李小和\n教育经历：博士后。"
                "参加科研项目，论文研究目标如下。"
            ),
            source_context="外来应聘/2015/李小和简历.doc",
        ),
        load_default_taxonomy(),
        limit=8,
    )

    assert [item.category_id for item in candidates] == [
        "college.hr.faculty-recruitment"
    ]
    incidental_categories = {
        "school.research",
        "school.hr.postdoc",
        "school.admin.development-planning",
    }
    assert incidental_categories.isdisjoint(
        {item.category_id for item in candidates}
    )
    assert "受管源目录命中" in candidates[0].candidate_reason


def test_recruitment_resume_semantics_work_without_managed_source_context():
    """文件自身同时表达简历和应聘语义时，也应识别为师资招聘材料。"""

    candidates = recall_category_candidates(
        DocumentFeatures(
            filename="应聘教师CV.pdf",
            full_text="个人信息：姓名张三。教育背景和工作经历如下。",
        ),
        load_default_taxonomy(),
        limit=8,
    )

    assert candidates[0].category_id == "college.hr.faculty-recruitment"


def test_plain_academic_resume_does_not_force_faculty_recruitment():
    """没有应聘目录或应聘语义的学术简历不能被强制归入师资招聘。"""

    candidates = recall_category_candidates(
        DocumentFeatures(
            filename="专家学术简历.doc",
            full_text="个人简历，主要介绍科研项目、论文和博士后研究经历。",
        ),
        load_default_taxonomy(),
        limit=8,
    )

    assert all(
        item.category_id != "college.hr.faculty-recruitment"
        for item in candidates
    )


def test_managed_english_cv_keeps_locatable_resume_evidence():
    """应聘目录中的英文 CV 应使用英文结构字段形成可定位证据。"""

    matches = match_document_features(
        DocumentFeatures(
            filename="resume_wenbo.pdf",
            full_text="Name: Wen Bo\nEducation\nExperience\nPublications",
            source_context="外来应聘/2013应聘人员/resume_wenbo.pdf",
        ),
        load_default_taxonomy(),
    )

    assert matches[0]["category_id"] == "college.hr.faculty-recruitment"
    assert {"Name", "Education", "Experience"} & set(matches[0]["evidence"])


@pytest.mark.parametrize(
    ("filename", "full_text"),
    [
        (
            "_卫凡-东京工业大学.doc",
            "个人基本信息\n姓  名\n出生年月\n联系方式\n教育经历\n研究工作简介与论文发表",
        ),
        (
            "_尹毅峰1_.doc",
            "个  人  简  历\n个人信息\n教育经历\n工作经验\n学术成果",
        ),
        (
            "刘汉强-西安电子科技大学-博士研究生.doc",
            "姓名 刘汉强\n出生年月\n手机\n教育经历\n发表文章",
        ),
        (
            "温苗利.doc",
            "基本信息\n出生年月\n手机\n求职意向\n教育背景\n学术论文",
        ),
        (
            "王青龙.doc",
            "性别 男\n生日\n手机\n教育背景\n工作经历\n发表论文",
        ),
        (
            "西北工业大学-航空宇航制造工程-洪歧[1].doc",
            "简  历\n个人信息\n出生年月\n移动电话\n主要经历\n论文",
        ),
        (
            "洪明辉-台湾.pdf",
            "基本資料\n姓名\n出生年月日\n任教學校\n現任職級\n代表著作",
        ),
    ],
)
def test_managed_recruitment_resume_structure_overrides_generic_filename(
    filename,
    full_text,
):
    """应聘根中的结构化简历即使文件名只有姓名，也应进入师资招聘。"""

    candidates = recall_category_candidates(
        DocumentFeatures(
            filename=filename,
            full_text=full_text,
            source_context=f"外来应聘/{filename}",
        ),
        load_default_taxonomy(),
        limit=8,
    )

    assert candidates[0].category_id == "college.hr.faculty-recruitment"


def test_managed_research_statement_is_faculty_recruitment_material():
    """应聘根中的研究陈述属于候选人提交材料，不应进入其他。"""

    candidates = recall_category_candidates(
        DocumentFeatures(
            filename="Statement of Research Interest.pdf",
            full_text="Statement of Research Interest\nQian Zhang\nPresent Research",
            source_context="外来应聘/Statement of Research Interest.pdf",
        ),
        load_default_taxonomy(),
        limit=8,
    )

    assert candidates[0].category_id == "college.hr.faculty-recruitment"
    assert "Statement of Research Interest" in candidates[0].matched_signals


@pytest.mark.parametrize(
    "filename",
    [
        "CVPR_2020_Wang_Deep_Spatial_Gradient.pdf",
        "CVPRW2020.pdf",
        "2014_CVPR_paper.pdf",
    ],
)
def test_cvpr_paper_is_not_treated_as_resume(filename):
    """CVPR 论文文件名不能因为以 CV 开头而被误判为简历。"""

    candidates = recall_category_candidates(
        DocumentFeatures(
            filename=filename,
            full_text="Computer Vision and Pattern Recognition paper abstract.",
            source_context=f"外来应聘/论文全文/{filename}",
        ),
        load_default_taxonomy(),
        limit=8,
    )

    assert all(
        item.category_id != "college.hr.faculty-recruitment"
        for item in candidates
    )


def test_match_document_text_uses_recall_candidates_for_rule_only_output():
    """兼容入口应基于候选召回生成 rule-only 分类建议。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("教师岗位聘期考核和续聘材料。", taxonomy)

    assert matches[0]["category_path"] == ["学校", "人事师资", "考核聘任"]
    assert matches[0]["source"] == "rule"
    assert "聘期" in matches[0]["evidence"] or "续聘" in matches[0]["evidence"]


@pytest.mark.parametrize(
    ("text", "expected_id", "expected_path"),
    [
        ("校属各单位请知悉本项临时联络事项。", "school.other", ["学校", "其他"]),
        (
            "西安理工校发〔2026〕12号，校属各单位请知悉本项临时联络事项。",
            "school.issued",
            ["学校", "发文"],
        ),
        (
            "财务处关于“两新”项目配套资金的工作通知。",
            "school.finance.other",
            ["学校", "财务", "其他"],
        ),
        (
            "财务处〔2026〕8号，关于“两新”项目配套资金的工作通知。",
            "school.finance.issued",
            ["学校", "财务", "发文"],
        ),
        ("计算机科学与工程学院院内临时联络材料。", "college.other", ["学院", "其他"]),
        (
            "计算机学院〔2026〕3号，关于临时联络事项的说明。",
            "college.issued",
            ["学院", "发文"],
        ),
    ],
)
def test_unclassified_fallback_uses_scope_department_and_document_number(
    text: str,
    expected_id: str,
    expected_path: list[str],
):
    """未命中具体业务分类时，应按组织、部门和文号组合落入发文或其他。"""

    matches = match_document_text(text, load_default_taxonomy())

    assert matches[0]["category_id"] == expected_id
    assert matches[0]["category_path"] == expected_path
    assert matches[0]["source"] == "rule_fallback"


def test_unclassified_fallback_does_not_override_specific_business_category():
    """已有具体业务分类时，即使存在部门和文号，也不得改写为发文兜底分类。"""

    matches = match_document_text(
        "西安理工人事〔2026〕8号，教师专业技术职务任职资格申报材料。",
        load_default_taxonomy(),
    )

    category_ids = {item.get("category_id") for item in matches}
    assert "school.hr.title-review" in category_ids
    assert "school.hr.issued" not in category_ids


def test_unclassified_fallback_uses_primary_root_after_evidence_review():
    """最高排名具体候选缺少正文证据时，应按其学校分支回退到其他。"""

    matches = apply_unclassified_fallback(
        document_features=DocumentFeatures(filename="国际合作统计表.xls"),
        taxonomy=load_default_taxonomy(),
        matches=[
            {
                "category_id": "school.international-cooperation",
                "category_path": ["学校", "国际合作交流"],
                "status": "NEEDS_REVIEW",
                "source": "rule",
            },
            {
                "category_id": "school.hr.appointment-assessment",
                "category_path": ["学校", "人事师资", "考核聘任"],
                "status": "SUGGESTED",
                "source": "rule",
            },
        ],
    )

    assert matches[0]["category_id"] == "school.other"
    assert matches[0]["category_path"] == ["学校", "其他"]
