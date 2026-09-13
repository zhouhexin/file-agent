"""基于分类体系配置的确定性文本匹配器。"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from app.modules.classification.schemas import CategoryNode, CategoryNodeKind, Taxonomy
from app.modules.classification.rule_policy import (
    ClassificationRulePolicy,
    evaluate_rule_policy,
)


_APPOINTMENT_CATEGORY_IDS = {
    "school.hr.appointment-assessment",
    "college.hr.appointment-assessment",
}
_FACULTY_RECRUITMENT_CATEGORY_ID = "college.hr.faculty-recruitment"
_TITLE_REVIEW_CATEGORY_ID = "school.hr.title-review"
_TITLE_REVIEW_CATEGORY_IDS = {
    _TITLE_REVIEW_CATEGORY_ID,
    "college.hr.title-review",
}
_TITLE_REVIEW_FORM_SIGNALS = (
    "专家鉴定意见表",
    "教师职务任职资格评审表",
    "职务任职资格评审表",
    "职务评审简表",
    "评审简表",
)
_RESUME_SIGNALS = (
    "个人简历",
    "求职简历",
    "应聘简历",
    "个人履历",
    "求职履历",
    "求职个人简介",
    "简历",
)
_RESUME_FLEXIBLE_SIGNALS = (
    "个人简历",
    "求职简历",
    "应聘简历",
    "个人履历",
    "履历表",
    "简历",
)
_RESUME_STRUCTURE_SIGNAL_GROUPS = (
    ("个人信息", "基本信息", "基本资料", "基本資料", "姓名", "性别", "出生年月", "出生日期", "生日"),
    ("教育经历", "教育背景", "学习经历", "毕业院校", "学历", "学位"),
    (
        "工作经历",
        "工作经验",
        "主要经历",
        "求职意向",
        "任教学校",
        "任教學校",
        "现任职级",
        "現任職級",
        "现职聘任",
        "現職聘任",
    ),
    ("联系方式", "手机", "电话", "邮箱", "通信地址"),
    ("科研经历", "项目经历", "发表论文", "发表文章", "学术成果", "代表著作", "著作表列"),
)
_RESUME_STRUCTURE_ENGLISH_SIGNAL_GROUPS = (
    ("name", "birth", "personal information"),
    ("education", "academic background"),
    ("experience", "employment", "work experience"),
    ("contact", "phone", "email", "address"),
    ("publications", "research experience", "projects"),
)
_RESEARCH_STATEMENT_SIGNALS = (
    "Statement of Research Interest",
    "Research Interest Statement",
    "Research Statement",
    "研究兴趣陈述",
    "研究陈述",
)
_RECRUITMENT_CONTEXT_SIGNALS = (
    "外来应聘",
    "应聘人员",
    "应聘材料",
    "应聘教师",
    "应聘",
    "求职",
    "师资招聘",
    "教师招聘",
    "招聘",
    "面试人员",
    "面试材料",
    "面试",
    "试讲",
    "拟聘",
)
_SPREADSHEET_EXTENSIONS = (".xls", ".xlsx", ".xlsm", ".csv")
_SPREADSHEET_TUTORIAL_PATTERN = re.compile(
    r"(?:vlookup|hlookup|xlookup|\bmatch\b|函数(?:使用|教程|示例|练习)|公式(?:教程|示例|练习))",
    re.IGNORECASE,
)

# 这些词只判定学校/学院组织范围，不直接生成具体业务分类。词表来自
# E:\workdata 的真实目录和文件命名；具体业务类别仍由 taxonomy 子节点的
# 正文信号及可定位证据决定。
_COLLEGE_SCOPE_IDENTITY_SIGNALS = (
    "计算机科学与工程学院",
    "计算机学院",
    "数据科学与大数据技术",
    "计算机科学与技术",
    "网络空间安全",
    "智能科学与技术",
    "数字媒体技术",
    "物联网工程",
    "计算机技术",
    "软件工程",
    "网络工程",
    "网络安全",
    "信息安全",
    "人工智能",
    "网络安全所",
    "计算机实验室",
    "计算机系",
    "软件系",
    "网络系",
    "计算机",
)
_COLLEGE_SCOPE_FORMAL_SIGNALS = (
    "计算机科学与工程学院",
    "计算机学院",
)
_SCHOOL_SCOPE_INSTITUTION_SIGNALS = (
    "中共西安理工大学委员会",
    "西安理工大学",
)
_SCHOOL_SCOPE_DEPARTMENT_SIGNALS = (
    "实验室管理处",
    "国际交流处",
    "发展规划处",
    "曲江管理处",
    "校友工作处",
    "研究生院",
    "研究生部",
    "信息化处",
    "校财务处",
    "财务处",
    "人事处",
    "保卫处",
    "后勤处",
    "审计处",
    "教务处",
    "科技处",
    "资产处",
    "招标处",
    "档案馆",
    "宣传部",
    "组织部",
    "统战部",
    "校工会",
    "校办",
    "党办",
    "纪委",
)
_SCHOOL_SCOPE_WIDE_SIGNALS = (
    "中共西安理工大学委员会",
    "学校统一部署",
    "校属各单位",
    "面向全校",
    "学校党委",
    "学校行政",
    "学校决定",
    "校长办公会",
    "党委常委会",
    "全校",
)
_SCHOOL_SCOPE_PUBLISH_ACTIONS = (
    "组织开展",
    "统一部署",
    "印发",
    "发布",
    "下发",
    "部署",
    "决定",
    "要求",
    "通知",
    "关于",
)
_SCHOOL_SCOPE_RECIPIENT_ACTIONS = (
    "提交至",
    "报送至",
    "提交",
    "报送",
    "上报",
    "送交",
    "反馈",
    "报给",
    "交至",
)

# 常见材料必须同时命中明确题名与表单字段组；“申请表”“合同”“任务书”
# 等单个文种词不能独立形成业务主类。元组字段依次为题名、字段组、学校 ID、
# 学院 ID、默认 ID 和最少字段组数。
_STRUCTURED_DOCUMENT_RULES = (
    # 人才申报汇总表以表内真实题名和成组字段为依据，个人科研履历只是支撑信息。
    (
        ("申报CJ计划人员情况汇总表", "人才计划人员情况汇总表", "青年教师奖申报汇总表",
         "青年教师基金及教师奖", "人才需求信息表", "引才用才汇总表", "校招共用人才目录"),
        (("姓名", "拟招聘人员", "人才"), ("申报项目", "项目类别", "引才用才", "校招共用"),
         ("入选人才计划", "入校时间", "职称", "学历", "需求")),
        "school.hr.talent-work", "college.hr.talent-work", "school.hr.talent-work", 3,
    ),
    (
        ("人事代理人员合同期满考核评分表", "人事代理人员合同期满考核情况汇总表",
         "合同期满考核评分表"),
        (("评价目标", "考核情况", "考核结果"), ("评价标准", "考核等级", "考核结论"),
         ("得分", "姓名", "岗位")),
        "school.hr.appointment-assessment", "college.hr.appointment-assessment",
        "school.hr.appointment-assessment", 3,
    ),
    (
        ("外聘和兼职教师基本信息采集表", "兼职教师基本信息表", "工号"),
        # 无表内题名的花名册须同时满足五组人事字段，不能仅凭文件名或“姓名”分类。
        (("聘任时间",), ("聘期",), ("任职状态",), ("单位名称",), ("工号", "姓名")),
        "school.hr", "college.hr", "school.hr", 5,
    ),
    (
        (
            "高层次人才申报表",
            "人才计划申报书",
            "三秦学者申请表",
            "特支计划申报书",
            "科技创新领军人才申报书",
            "青年拔尖人才申报书",
            "哲学社会科学和文化艺术人才申报书",
        ),
        (("姓名", "申报人"), ("申报类别", "人才计划", "申报层次"), ("所在单位", "工作单位")),
        "school.hr.talent-work",
        "college.hr.talent-work",
        "college.hr.talent-work",
        2,
    ),
    (
        ("教师职务聘期任务书",),
        (
            ("受聘人", "乙方", "姓名"),
            ("聘期", "受聘期间", "合同期限", "考核周期"),
            ("拟承担的工作任务", "工作任务", "岗位职责", "目标任务", "岗位"),
        ),
        "school.hr.faculty-recruitment",
        "college.hr.faculty-recruitment",
        "school.hr.faculty-recruitment",
        2,
    ),
    (
        ("教师岗位聘用合同", "聘期考核表", "聘期目标任务书", "合同期考核目标和任务书"),
        (("受聘人", "乙方", "姓名"), ("聘期", "合同期限", "考核周期"), ("岗位职责", "目标任务", "岗位")),
        "school.hr.appointment-assessment",
        "college.hr.appointment-assessment",
        "school.hr.appointment-assessment",
        2,
    ),
    (
        ("教师招聘报名表", "应聘人员登记表", "教师引进试讲评价表", "试讲评价表"),
        (("姓名", "应聘人"), ("应聘岗位", "拟聘岗位", "申报岗位"), ("毕业院校", "学历", "学位")),
        None,
        "college.hr.faculty-recruitment",
        "college.hr.faculty-recruitment",
        2,
    ),
    (
        ("教职工离职审批表", "教职工辞职审批表"),
        (
            ("申请人", "姓名", "职工姓名"),
            ("所在单位", "工作单位", "部门"),
            ("离职原因", "辞职原因", "辞职申请书"),
            ("审批意见", "单位意见", "人事处审核意见"),
        ),
        "school.hr",
        "college.hr",
        "school.hr",
        2,
    ),
    (
        ("教师职务任职资格评审表", "专业技术职务申报表", "职称申报评审表", "专家鉴定意见表"),
        (("申报人", "姓名"), ("申报职务", "申报专业技术职务", "任职资格"), ("评审意见", "专家意见")),
        "school.hr.title-review",
        "college.hr.title-review",
        "school.hr.title-review",
        2,
    ),
    (
        ("专业认证自评报告", "工程教育认证自评报告", "专业认证申请书"),
        (("专业名称", "认证专业"), ("培养目标", "毕业要求"), ("课程体系", "持续改进")),
        None,
        "college.teaching.program-accreditation",
        "college.teaching.program-accreditation",
        2,
    ),
    (
        ("专业建设方案", "本科人才培养方案", "专业人才培养方案", "课程建设方案"),
        (("专业名称", "专业代码"), ("培养目标", "培养方案"), ("课程体系", "课程设置")),
        None,
        "college.teaching",
        "college.teaching",
        2,
    ),
    (
        ("科研项目申报书", "科技项目申请书", "科研项目任务书"),
        (("项目名称",), ("项目负责人", "申请人"), ("研究内容", "技术路线"), ("经费预算", "经费概算")),
        "school.research",
        "college.research",
        "college.research",
        3,
    ),
)


@dataclass(frozen=True)
class FlattenedCategory:
    """展平后的分类路径，便于关键词匹配和回执展示。"""

    path: list[str]
    name: str
    order: int
    category_id: str | None = None
    description: str = ""
    aliases: list[str] | None = None
    positive_signals: list[str] | None = None
    negative_signals: list[str] | None = None
    examples: list[str] | None = None
    node_kind: CategoryNodeKind | None = None
    recall_enabled: bool = True
    primary_enabled: bool = False
    selectable: bool = True
    visible: bool = True


@dataclass(frozen=True)
class DocumentFeatures:
    """用于分类候选召回的文档特征，不包含持久化依赖。"""

    filename: str = ""
    title: str = ""
    full_text: str = ""
    headings: list[str] | None = None
    sheet_names: list[str] | None = None
    source_context: str = ""
    verified_purpose_category_id: str | None = None


@dataclass(frozen=True)
class CategoryCandidate:
    """分类候选召回结果，只用于排序和后续判定，不直接等同最终分类。"""

    category_id: str | None
    category_path: list[str]
    name: str
    rule_score: float
    matched_signals: list[str]
    matched_title_signals: list[str]
    matched_content_signals: list[str]
    negative_signals: list[str]
    organization_scope: str | None
    organization_score: float
    candidate_reason: str
    taxonomy_key: str
    taxonomy_version: str
    order: int
    business_score: float = 0.0
    scope_score: float = 0.0
    purpose_basis: str | None = None
    evidence_support: float = 0.0
    title_theme_score: float = 0.0
    leading_body_score: float = 0.0
    negative_conflict: bool = False


def flatten_category_paths(taxonomy: Taxonomy) -> list[FlattenedCategory]:
    """把树状分类配置展平成完整路径列表。"""

    flattened: list[FlattenedCategory] = []

    def walk(node: CategoryNode, parent_path: list[str]) -> None:
        """递归遍历分类树，并记录节点顺序用于稳定排序。"""

        path = [*parent_path, node.name]
        flattened.append(
            FlattenedCategory(
                path=path,
                name=node.name,
                order=len(flattened),
                category_id=node.id,
                description=node.description,
                aliases=list(node.aliases),
                positive_signals=list(node.positive_signals),
                negative_signals=list(node.negative_signals),
                examples=list(node.examples),
                node_kind=(
                    node.node_kind
                    or (CategoryNodeKind.BUSINESS if parent_path else CategoryNodeKind.GROUP)
                ),
                recall_enabled=(
                    bool(node.recall_enabled)
                    if node.recall_enabled is not None
                    else bool(parent_path)
                ),
                primary_enabled=bool(node.primary_enabled),
                selectable=(node.selectable is not False),
                visible=(node.visible is not False),
            )
        )
        for child in node.children:
            walk(child, path)

    for root in taxonomy.categories:
        walk(root, [])
    return flattened


def recall_category_candidates(
    document_features: DocumentFeatures,
    taxonomy: Taxonomy,
    *,
    limit: int = 5,
    rule_policy: ClassificationRulePolicy | None = None,
) -> list[CategoryCandidate]:
    """根据分类名、别名、正负信号召回 Top N 分类候选。"""

    if _is_spreadsheet_tutorial(document_features):
        return []

    title_text = _join_text(
        [
            document_features.filename,
            document_features.title,
            *(document_features.headings or []),
            *(document_features.sheet_names or []),
        ]
    )
    body_text = document_features.full_text or ""
    organization_scope = _detect_organization_scope(
        taxonomy=taxonomy,
        title_text=title_text,
        body_text=body_text,
    )
    title_review_form = _title_review_form_candidate(
        taxonomy=taxonomy,
        title_text=title_text,
        body_text=body_text,
        organization_scope=organization_scope.dominant_root,
    )
    recruitment_resume = _recruitment_resume_candidate(
        document_features=document_features,
        taxonomy=taxonomy,
        title_text=title_text,
        body_text=body_text,
    )
    structured_form = _structured_document_candidate(
        taxonomy=taxonomy,
        filename=document_features.filename,
        title_text=title_text,
        body_text=body_text,
        organization_scope=organization_scope.dominant_root,
    )
    candidates: list[CategoryCandidate] = [
        candidate
        for candidate in (title_review_form, recruitment_resume, structured_form)
        if candidate is not None
    ]
    for category in flatten_category_paths(taxonomy):
        if not category.recall_enabled or category.node_kind in {
            CategoryNodeKind.GROUP,
            CategoryNodeKind.FALLBACK,
        }:
            continue
        root_name = category.path[0] if category.path else ""
        if (
            category.category_id in _TITLE_REVIEW_CATEGORY_IDS
            and organization_scope.dominant_root in {"学校", "学院"}
            and root_name != organization_scope.dominant_root
        ):
            # 职称评审已有明确组织范围时，不再把另一组织镜像放回候选集。
            continue
        (
            score,
            matched_signals,
            matched_title_signals,
            matched_content_signals,
            negative_signals,
            reasons,
            title_theme_score,
            leading_body_score,
            negative_conflict,
        ) = _score_category_candidate(
            category=category,
            title_text=title_text,
            body_text=body_text,
        )
        if (
            category.category_id in _APPOINTMENT_CATEGORY_IDS
            and not _has_appointment_specific_signal(title_text, body_text)
        ):
            continue
        if score <= 0:
            continue
        scope_score = organization_scope.scores.get(root_name, 0.0)
        if root_name == organization_scope.dominant_root:
            matched_title_signals = _unique_signals(
                [
                    *matched_title_signals,
                    *organization_scope.matched_title_signals.get(root_name, []),
                ]
            )
            matched_content_signals = _unique_signals(
                [
                    *matched_content_signals,
                    *organization_scope.matched_content_signals.get(root_name, []),
                ]
            )
            matched_signals = _unique_signals(
                [
                    *matched_signals,
                    *matched_title_signals,
                    *matched_content_signals,
                ]
            )
            scope_signals = _unique_signals(
                [
                    *organization_scope.matched_title_signals.get(root_name, []),
                    *organization_scope.matched_content_signals.get(root_name, []),
                ]
            )
            reasons.insert(
                0,
                f"组织层级判定为{root_name}：{'、'.join(scope_signals[:5])}",
            )
        candidates.append(
            CategoryCandidate(
                category_id=category.category_id,
                category_path=category.path,
                name="/".join(category.path),
                rule_score=round(score, 4),
                matched_signals=matched_signals,
                matched_title_signals=matched_title_signals,
                matched_content_signals=matched_content_signals,
                negative_signals=negative_signals,
                organization_scope=organization_scope.dominant_root,
                organization_score=round(scope_score, 4),
                candidate_reason="；".join(reasons),
                taxonomy_key=taxonomy.key,
                taxonomy_version=taxonomy.version,
                order=category.order,
                business_score=round(score, 4),
                scope_score=round(scope_score, 4),
                purpose_basis="CONTENT_RULE",
                evidence_support=_evidence_support(
                    matched_title_signals=matched_title_signals,
                    matched_content_signals=matched_content_signals,
                    negative_signals=negative_signals,
                ),
                title_theme_score=title_theme_score,
                leading_body_score=leading_body_score,
                negative_conflict=negative_conflict,
            )
        )

    candidates = _merge_versioned_rule_candidates(
        candidates=candidates,
        taxonomy=taxonomy,
        document_features=document_features,
        organization_scope=organization_scope,
        title_text=title_text,
        body_text=body_text,
        rule_policy=rule_policy,
    )

    candidates = _dedupe_candidates_and_remove_shorter_embedded_matches(candidates)
    candidates.sort(
        key=lambda item: (
            # 强结构候选必须先进入有限候选集，再由主类选择器校正组织镜像；
            # 否则长表里的多个同组织背景词会把尚未切镜像的人事候选挤出 Top-N。
            -int(item.purpose_basis == "STRUCTURED_FORM"),
            -int(
                bool(organization_scope.dominant_root)
                and bool(item.category_path)
                and item.category_path[0] == organization_scope.dominant_root
            ),
            -item.business_score,
            -item.scope_score,
            -item.evidence_support,
            item.order,
        )
    )
    return candidates[:max(0, min(limit, 8))]


def detect_structured_document_purpose(*, filename: str, full_text: str) -> str | None:
    """返回可作为材料包锚点的唯一结构化业务用途。

    结果只来自明确题名和正文首页字段组。多个规则同时成立时返回 ``None``，
    防止汇编、模板合集或复合材料把整包附件带入错误业务。
    """

    # 文件名可用于组织提示，但结构业务题名本身必须在原正文首页出现。
    title_zone = (full_text or "")[:600]
    body_zone = (full_text or "")[:5_000]
    scope = _lightweight_structured_scope(filename, body_zone)
    matches: list[str] = []
    for (
        title_signals,
        field_groups,
        school_category_id,
        college_category_id,
        default_category_id,
        minimum_groups,
    ) in _STRUCTURED_DOCUMENT_RULES:
        if not _matched_structured_signals(title_zone, title_signals):
            continue
        group_count = sum(
            bool(_matched_structured_signals(body_zone, group))
            for group in field_groups
        )
        if group_count < minimum_groups:
            continue
        category_id = (
            school_category_id
            if scope == "学校" and school_category_id
            else college_category_id
            if scope == "学院" and college_category_id
            else default_category_id
        )
        if category_id:
            matches.append(category_id)
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _structured_document_candidate(
    *,
    taxonomy: Taxonomy,
    filename: str,
    title_text: str,
    body_text: str,
    organization_scope: str | None,
) -> CategoryCandidate | None:
    """把常见表单的“精确题名＋字段结构”转换为低门槛强候选。"""

    category_id = detect_structured_document_purpose(
        filename=filename,
        full_text=body_text,
    )
    if category_id is None:
        return None
    category = next(
        (
            item
            for item in flatten_category_paths(taxonomy)
            if item.category_id == category_id and item.primary_enabled
        ),
        None,
    )
    if category is None:
        return None
    matched_titles: list[str] = []
    matched_fields: list[str] = []
    for title_signals, field_groups, *_rest in _STRUCTURED_DOCUMENT_RULES:
        titles = _matched_structured_signals(body_text[:600], title_signals)
        if not titles:
            continue
        group_count = sum(
            bool(_matched_structured_signals(body_text[:5_000], group))
            for group in field_groups
        )
        if group_count < _rest[-1]:
            continue
        fields = [
            signal
            for group in field_groups
            for signal in _matched_structured_signals(body_text[:5_000], group)
        ]
        matched_titles.extend(titles)
        matched_fields.extend(fields)
    matched_titles = _unique_signals(matched_titles)
    matched_fields = _unique_signals(matched_fields)
    matched = _unique_signals([*matched_titles, *matched_fields])
    return CategoryCandidate(
        category_id=category.category_id,
        category_path=category.path,
        name="/".join(category.path),
        rule_score=0.72,
        matched_signals=matched,
        matched_title_signals=matched_titles,
        matched_content_signals=matched_fields,
        negative_signals=[],
        organization_scope=organization_scope or category.path[0],
        organization_score=0.85 if organization_scope == category.path[0] else 0.45,
        candidate_reason=(
            "明确表单题名与正文首页字段结构同时成立："
            f"{'、'.join(matched[:6])}"
        ),
        taxonomy_key=taxonomy.key,
        taxonomy_version=taxonomy.version,
        order=category.order,
        business_score=0.72,
        scope_score=0.85 if organization_scope == category.path[0] else 0.45,
        purpose_basis="STRUCTURED_FORM",
        evidence_support=1.0,
        title_theme_score=0.46,
        leading_body_score=0.3,
        negative_conflict=False,
    )


def _lightweight_structured_scope(title_text: str, body_text: str) -> str | None:
    """为结构锚点解析组织根；题名范围优先于表单内填报单位。"""

    # 题名直接声明的组织范围优先于表单正文中的“所在单位”等填报值。
    # 例如校级《西安理工大学教职工离职审批表》内填写“计算机学院”，
    # 仍然是学校人事表单，不应被被审批人的单位反向切成学院表单。
    if _matched_configured_signals(title_text, _COLLEGE_SCOPE_FORMAL_SIGNALS):
        return "学院"
    if _matched_configured_signals(title_text, _SCHOOL_SCOPE_DEPARTMENT_SIGNALS):
        return "学校"
    if _matched_configured_signals(title_text, _SCHOOL_SCOPE_INSTITUTION_SIGNALS):
        return "学校"
    body_zone = body_text[:2_000]
    if _matched_configured_signals(body_zone, _COLLEGE_SCOPE_FORMAL_SIGNALS):
        return "学院"
    if _matched_configured_signals(body_zone, _SCHOOL_SCOPE_DEPARTMENT_SIGNALS):
        return "学校"
    if _matched_configured_signals(body_zone, _SCHOOL_SCOPE_INSTITUTION_SIGNALS):
        return "学校"
    return None


def _merge_versioned_rule_candidates(
    *,
    candidates: list[CategoryCandidate],
    taxonomy: Taxonomy,
    document_features: DocumentFeatures,
    organization_scope: "_OrganizationScopeDecision",
    title_text: str,
    body_text: str,
    rule_policy: ClassificationRulePolicy | None,
) -> list[CategoryCandidate]:
    """把版本化强规则合并到候选集，不删除其他组织或正文候选。"""

    categories_by_id = {
        category.category_id: category
        for category in flatten_category_paths(taxonomy)
        if category.category_id and category.recall_enabled
    }
    by_id = {candidate.category_id: candidate for candidate in candidates}
    for policy_match in evaluate_rule_policy(
        filename=document_features.filename,
        title=document_features.title,
        body_text=body_text,
        organization_root=organization_scope.dominant_root,
        policy=rule_policy,
    ):
        category = categories_by_id.get(policy_match.category_id)
        if category is None:
            continue
        root_name = category.path[0] if category.path else ""
        scope_score = organization_scope.scores.get(root_name, 0.0)
        current = by_id.get(category.category_id)
        if current is not None:
            updated = replace(
                current,
                rule_score=max(current.rule_score, policy_match.business_score),
                business_score=max(current.business_score, policy_match.business_score),
                scope_score=max(current.scope_score, scope_score),
                evidence_support=max(
                    current.evidence_support,
                    policy_match.evidence_support,
                ),
                title_theme_score=max(
                    current.title_theme_score,
                    _signal_group_score(
                        [
                            signal
                            for signal in policy_match.matched_signals
                            if signal in title_text
                        ],
                        location="title",
                    ),
                ),
                matched_signals=_unique_signals(
                    [*current.matched_signals, *policy_match.matched_signals]
                ),
                negative_signals=_unique_signals(
                    [*current.negative_signals, *policy_match.negative_signals]
                ),
                candidate_reason=(
                    f"{current.candidate_reason}；{policy_match.reason}"
                    if current.candidate_reason
                    else policy_match.reason
                ),
                purpose_basis="VERSIONED_RULE",
            )
            candidates[candidates.index(current)] = updated
            by_id[category.category_id] = updated
            continue
        matched_title = [
            signal for signal in policy_match.matched_signals if signal in title_text
        ]
        matched_body = [
            signal for signal in policy_match.matched_signals if signal in body_text
        ]
        created = CategoryCandidate(
            category_id=category.category_id,
            category_path=category.path,
            name="/".join(category.path),
            rule_score=policy_match.business_score,
            matched_signals=list(policy_match.matched_signals),
            matched_title_signals=matched_title,
            matched_content_signals=matched_body,
            negative_signals=list(policy_match.negative_signals),
            organization_scope=organization_scope.dominant_root,
            organization_score=scope_score,
            candidate_reason=policy_match.reason,
            taxonomy_key=taxonomy.key,
            taxonomy_version=taxonomy.version,
            order=category.order,
            business_score=policy_match.business_score,
            scope_score=scope_score,
            purpose_basis="VERSIONED_RULE",
            evidence_support=policy_match.evidence_support,
            title_theme_score=_signal_group_score(matched_title, location="title"),
            leading_body_score=_signal_group_score(
                [
                    signal
                    for signal in matched_body
                    if signal in body_text[:2_000]
                ],
                location="leading",
            ),
            negative_conflict=False,
        )
        candidates.append(created)
        by_id[category.category_id] = created
    return candidates


def _title_review_form_candidate(
    *,
    taxonomy: Taxonomy,
    title_text: str,
    body_text: str,
    organization_scope: str | None,
) -> CategoryCandidate | None:
    """明确职称表单可形成强候选，但不能自行猜测学校或学院范围。"""

    title_signals = _matched_signals(title_text, _TITLE_REVIEW_FORM_SIGNALS)
    leading_body = body_text[:1_500]
    body_signals = _matched_signals(leading_body, _TITLE_REVIEW_FORM_SIGNALS)
    if not title_signals and not body_signals:
        return None
    if organization_scope not in {"学校", "学院"}:
        return None
    category_id = (
        "school.hr.title-review"
        if organization_scope == "学校"
        else "college.hr.title-review"
    )
    category = next(
        (
            item
            for item in flatten_category_paths(taxonomy)
            if item.category_id == category_id
        ),
        None,
    )
    if category is None:
        return None
    matched_signals = _unique_signals([*title_signals, *body_signals])
    return CategoryCandidate(
        category_id=category.category_id,
        category_path=category.path,
        name="/".join(category.path),
        rule_score=4.0,
        matched_signals=matched_signals,
        matched_title_signals=title_signals,
        matched_content_signals=body_signals,
        negative_signals=[],
        organization_scope=organization_scope,
        organization_score=1.0,
        candidate_reason=(
            "文件名、标题或正文首页明确包含职称评审表单题名："
            f"{'、'.join(matched_signals)}"
        ),
        taxonomy_key=taxonomy.key,
        taxonomy_version=taxonomy.version,
        order=category.order,
        business_score=4.0,
        scope_score=1.0,
        purpose_basis="TITLE_FORM",
        evidence_support=1.0,
    )


def _recruitment_resume_candidate(
    *,
    document_features: DocumentFeatures,
    taxonomy: Taxonomy,
    title_text: str,
    body_text: str,
) -> CategoryCandidate | None:
    """只在局部窗口形成单人简历结构，避免跨章节累积履历词。"""

    resume_title_signals = _resume_signals(title_text)
    resume_window = _find_resume_structure_window(body_text)
    resume_body_signals = _resume_signals(resume_window.text) if resume_window else []
    structure_signals = list(resume_window.signals) if resume_window else []
    structure_group_count = resume_window.group_count if resume_window else 0
    verified_recruitment_package = (
        document_features.verified_purpose_category_id
        == _FACULTY_RECRUITMENT_CATEGORY_ID
    )
    research_statement_title_signals = _matched_case_insensitive_signals(
        title_text,
        _RESEARCH_STATEMENT_SIGNALS,
        word_boundary=False,
    )
    research_statement_body_signals = _matched_case_insensitive_signals(
        resume_window.text if resume_window else (body_text if verified_recruitment_package else ""),
        _RESEARCH_STATEMENT_SIGNALS,
        word_boundary=False,
    )
    resume_expression = bool(resume_title_signals or resume_body_signals)
    has_recruitment_material_signal = bool(
        resume_expression
        or research_statement_title_signals
        or research_statement_body_signals
        or structure_signals
    )
    if verified_recruitment_package:
        if not has_recruitment_material_signal:
            return None
    elif structure_group_count < 3 or not resume_expression:
        return None

    title_context_signals = _matched_signals(title_text, _RECRUITMENT_CONTEXT_SIGNALS)
    body_context_signals = _matched_signals(
        resume_window.text if resume_window else "",
        _RECRUITMENT_CONTEXT_SIGNALS,
    )
    category = next(
        (
            item
            for item in flatten_category_paths(taxonomy)
            if item.category_id == _FACULTY_RECRUITMENT_CATEGORY_ID
        ),
        None,
    )
    if category is None:
        return None

    matched_content_signals = _unique_signals(
        [
            *resume_body_signals,
            *research_statement_body_signals,
            *body_context_signals,
            *structure_signals,
        ]
    )
    matched_title_signals = _unique_signals(
        [
            *resume_title_signals,
            *research_statement_title_signals,
            *title_context_signals,
        ]
    )
    matched_signals = _unique_signals(
        [
            *matched_content_signals,
            *matched_title_signals,
        ]
    )
    context_signals = _unique_signals([*title_context_signals, *body_context_signals])
    context_reason = (
        "冻结用途包已验证为师资招聘"
        if verified_recruitment_package
        else f"文件语义命中：{'、'.join(context_signals[:3])}"
    )
    return CategoryCandidate(
        category_id=category.category_id,
        category_path=category.path,
        name="/".join(category.path),
        rule_score=1.0,
        matched_signals=matched_signals,
        matched_title_signals=matched_title_signals,
        matched_content_signals=matched_content_signals,
        negative_signals=[],
        organization_scope="学院",
        organization_score=0.45,
        candidate_reason=f"简历文档用途优先；{context_reason}",
        taxonomy_key=taxonomy.key,
        taxonomy_version=taxonomy.version,
        order=category.order,
        business_score=1.0,
        scope_score=0.45,
        purpose_basis=(
            "VERIFIED_PACKAGE" if verified_recruitment_package else "LOCAL_RESUME_WINDOW"
        ),
        evidence_support=1.0,
    )


@dataclass(frozen=True)
class _ResumeStructureWindow:
    """单个可定位窗口内的简历结构证据。"""

    text: str
    signals: tuple[str, ...]
    group_count: int


def _find_resume_structure_window(text: str) -> _ResumeStructureWindow | None:
    """在最多 12 段、3,000 字的连续窗口内寻找三组独立履历结构。"""

    paragraphs = [part.strip() for part in re.split(r"[\r\n]+", text) if part.strip()]
    if not paragraphs and text.strip():
        paragraphs = [text.strip()]
    best: _ResumeStructureWindow | None = None
    for start in range(len(paragraphs)):
        window_parts: list[str] = []
        for paragraph in paragraphs[start : start + 12]:
            candidate_text = "\n".join([*window_parts, paragraph])
            if len(candidate_text) > 3_000:
                remaining = 3_000 - len("\n".join(window_parts))
                if remaining > 0:
                    window_parts.append(paragraph[:remaining])
                break
            window_parts.append(paragraph)
        window_text = "\n".join(window_parts)
        signals, group_count = _resume_structure_signals(window_text)
        candidate = _ResumeStructureWindow(
            text=window_text,
            signals=tuple(signals),
            group_count=group_count,
        )
        if best is None or candidate.group_count > best.group_count:
            best = candidate
        if group_count >= 3 and _resume_signals(_join_text([paragraphs[start], window_text])):
            return candidate
    return best if best is not None and best.group_count >= 3 else None


def _resume_signals(text: str) -> list[str]:
    """识别中文简历词和独立的 CV/resume 英文表达。"""

    matched = _unique_signals(
        [
            *_matched_signals(text, _RESUME_SIGNALS),
            *[
                value
                for signal in _RESUME_FLEXIBLE_SIGNALS
                if (value := _match_flexible_chinese_signal(text, signal))
            ],
        ]
    )
    lowered = text.lower()
    if re.search(r"(?:^|[^a-z])resume(?:[^a-z]|$)", lowered):
        matched.append("resume")
    if re.search(r"(?:^|[^a-z])cv(?:[^a-z]|$)", lowered):
        matched.append("CV")
    if "curriculum vitae" in lowered:
        matched.append("curriculum vitae")
    return _unique_signals(matched)


def _resume_structure_signals(text: str) -> tuple[list[str], int]:
    """识别无简历标题文件中的个人、教育、工作、联系和成果结构。"""

    matched: list[str] = []
    matched_group_count = 0
    for group in _RESUME_STRUCTURE_SIGNAL_GROUPS:
        group_matches = [
            value
            for signal in group
            if (value := _match_flexible_chinese_signal(text, signal))
        ]
        if group_matches:
            matched_group_count += 1
            matched.extend(group_matches)
    for group in _RESUME_STRUCTURE_ENGLISH_SIGNAL_GROUPS:
        group_matches = _matched_case_insensitive_signals(text, group)
        if group_matches:
            matched_group_count += 1
            matched.extend(group_matches)
    return _unique_signals(matched), matched_group_count


def _match_flexible_chinese_signal(text: str, signal: str) -> str:
    """允许中文表格标题字符间出现空格，但不跨行匹配。"""

    pattern = r"[ \t\u3000]*".join(re.escape(character) for character in signal)
    result = re.search(pattern, text)
    return result.group(0) if result is not None else ""


def _matched_signals(text: str, signals: tuple[str, ...]) -> list[str]:
    """按配置顺序返回文本中实际出现的信号。"""

    return _matched_configured_signals(text, signals)


def _matched_configured_signals(
    text: str, signals: tuple[str, ...] | list[str]
) -> list[str]:
    """匹配 taxonomy 信号并保留原文中的实际片段，供后续证据定位。"""

    return _unique_signals(
        [
            matched
            for signal in signals
            if (matched := _match_configured_signal(text, signal))
        ]
    )


def _matched_structured_signals(
    text: str, signals: tuple[str, ...] | list[str]
) -> list[str]:
    """匹配表格字段，允许旧版 Office 把单元格文字按字符拆成多行。"""

    matched: list[str] = []
    for raw_signal in signals:
        signal = str(raw_signal or "").strip()
        if not signal or not text:
            continue
        direct = _match_configured_signal(text, signal)
        if direct:
            matched.append(direct)
            continue
        if not any("\u4e00" <= character <= "\u9fff" for character in signal):
            continue
        pattern = r"[ \t\u3000\r\n]*".join(
            re.escape(character) for character in signal
        )
        result = re.search(pattern, text)
        if result is not None:
            matched.append(result.group(0))
    return _unique_signals(matched)


def _match_configured_signal(text: str, signal: str) -> str:
    """支持大小写、全角空白和中文字符间空白，但不生成不存在于原文的引文。"""

    signal = str(signal or "").strip()
    if not signal or not text:
        return ""
    if signal in text:
        return signal
    if any("\u4e00" <= character <= "\u9fff" for character in signal):
        if matched := _match_flexible_chinese_signal(text, signal):
            return matched
    matched = re.search(re.escape(signal), text, re.IGNORECASE)
    return matched.group(0) if matched is not None else ""


def _matched_case_insensitive_signals(
    text: str,
    signals: tuple[str, ...],
    *,
    word_boundary: bool = True,
) -> list[str]:
    """返回英文文本中实际出现的原始大小写信号，供证据定位。"""

    matched: list[str] = []
    for signal in signals:
        pattern = rf"\b{re.escape(signal)}\b" if word_boundary else re.escape(signal)
        result = re.search(pattern, text, re.IGNORECASE)
        if result is not None:
            matched.append(result.group(0))
    return matched


def match_document_text(
    text: str,
    taxonomy: Taxonomy,
    *,
    limit: int = 5,
    rule_policy: ClassificationRulePolicy | None = None,
) -> list[dict[str, Any]]:
    """基于候选召回生成 rule-only 分类建议，保留旧调用入口。"""

    candidates = recall_category_candidates(
        DocumentFeatures(full_text=text or ""),
        taxonomy,
        limit=limit,
        rule_policy=rule_policy,
    )
    matches = [_candidate_to_category(candidate) for candidate in candidates]
    return apply_unclassified_fallback(
        document_features=DocumentFeatures(full_text=text or ""),
        taxonomy=taxonomy,
        matches=_dedupe_and_remove_shorter_embedded_matches(matches),
    )


def match_document_features(
    document_features: DocumentFeatures,
    taxonomy: Taxonomy,
    *,
    limit: int = 5,
    rule_policy: ClassificationRulePolicy | None = None,
) -> list[dict[str, Any]]:
    """按文件名和正文分离的特征生成建议，供自动落位区分证据来源。"""

    candidates = recall_category_candidates(
        document_features,
        taxonomy,
        limit=limit,
        rule_policy=rule_policy,
    )
    matches = _dedupe_and_remove_shorter_embedded_matches(
        [_candidate_to_category(candidate) for candidate in candidates]
    )
    return apply_unclassified_fallback(
        document_features=document_features,
        taxonomy=taxonomy,
        matches=matches,
    )


_DOCUMENT_NUMBER_PATTERNS = (
    re.compile(
        r"[\u4e00-\u9fffA-Za-z]{1,20}?\s*[〔\[（(【]"
        r"(?:19|20)\d{2}[〕\]）)】]\s*\d{1,6}\s*号"
    ),
    re.compile(r"(?:19|20)\d{2}\s*年\s*第\s*\d{1,6}\s*号"),
)


def apply_unclassified_fallback(
    *,
    document_features: DocumentFeatures,
    taxonomy: Taxonomy,
    matches: list[dict[str, Any]],
    default_organization_root: str | None = None,
) -> list[dict[str, Any]]:
    """在业务候选后追加学校/学院、部门和文号驱动的受控兜底。

    FALLBACK 节点不会进入普通关键词、语义或图谱竞争；只有主分类选择器确认
    没有合格业务候选时才会使用这里生成的建议。若组织范围也无法唯一确定，
    才继续使用全局 ``system.other``，因此不会凭来源路径编造部门。
    """

    concrete_matches = [
        item
        for item in matches
        if _is_current_business_candidate(item)
    ]
    # 已有明确末级业务候选时不把兜底作为普通次级标签返回；兜底只负责
    # “无法具体分类”的最后落位，不能污染已分类文件的多标签结果。
    if any(
        len(list(item.get("category_path") or [])) >= 3
        and float(dict(item.get("candidate_scores") or {}).get("business") or 0.0) >= 0.45
        for item in concrete_matches
    ):
        return concrete_matches
    # 摘要阶段的根级兜底不能锁死结果；每轮用当前完整证据重新验证部门及文号。
    fallback = _build_scoped_fallback(
        document_features=document_features,
        taxonomy=taxonomy,
        matches=concrete_matches,
        default_organization_root=default_organization_root,
    )
    if fallback is None:
        fallback = _other_category(taxonomy)
    return [*concrete_matches, fallback]


def _build_scoped_fallback(
    *,
    document_features: DocumentFeatures,
    taxonomy: Taxonomy,
    matches: list[dict[str, Any]],
    default_organization_root: str | None,
) -> dict[str, Any] | None:
    """仅用正文/标题可验证的组织、部门和文号生成分支兜底。"""

    policy = taxonomy.fallback_policy
    if (
        policy is None
        or not policy.department_category_ids
        or policy.issued is None
        or policy.other is None
    ):
        return None
    body_text = document_features.full_text or ""
    document_title_text = _join_text(
        [
            document_features.title,
            *(document_features.headings or []),
            *(document_features.sheet_names or []),
        ]
    )
    # 普通文件名仍不能决定范围 fallback；唯一例外是严格文号整体，它可以
    # 作为不可变来源元数据参与发文机关与部门的唯一映射。
    filename_document_number, filename_number_source = _find_filename_document_number(
        document_features.filename
    )
    located_text = _join_text(
        [
            document_features.title,
            *(document_features.headings or []),
            *(document_features.sheet_names or []),
            body_text,
            filename_document_number,
        ]
    )
    document_number, document_number_source = _find_document_number(
        title_text=document_title_text,
        body_text=body_text,
    )
    if not document_number and filename_document_number:
        document_number = filename_document_number
        document_number_source = filename_number_source
    scope = _detect_organization_scope(
        taxonomy=taxonomy,
        title_text=_join_text([document_title_text, filename_document_number]),
        body_text=body_text,
    )
    department_ids = set(policy.department_category_ids)
    department_nodes = {
        item.category_id: item
        for item in flatten_category_paths(taxonomy)
        if item.category_id in department_ids
    }
    # 部门识别独立于业务 Top-N：父节点可能被叶候选挤出，但正文发布/报送机关仍有效。
    department_matches = [
        {
            "category_id": node.category_id,
            "category_path": node.path,
            "evidence": [
                signal
                for signal in _unique_signals(
                    [node.name, *node.aliases, *node.positive_signals]
                )
                if signal in located_text
            ],
        }
        for node in department_nodes.values()
        if (
            not scope.dominant_root
            or node.path[:1] == [scope.dominant_root]
        )
        and _is_department_keyword_match(
            department=node,
            text=located_text,
            document_number=document_number,
        )
    ]
    departments_by_id = {
        str(item.get("category_id") or ""): item for item in department_matches
    }
    if len(departments_by_id) > 1:
        return None
    department = (
        next(iter(departments_by_id.values()))
        if len(departments_by_id) == 1
        else None
    )
    if department is not None:
        base_path = [str(value) for value in department.get("category_path", [])]
        base_id = str(department.get("category_id") or "")
        evidence = _unique_signals(
            [
                *[str(value) for value in department.get("evidence", [])],
                *[str(value) for value in department.get("matched_signals", [])],
            ]
        )
    else:
        fallback_root = (
            scope.dominant_root
            or (
                default_organization_root
                if default_organization_root in {"学校", "学院"}
                and bool(scope.scores.get(default_organization_root, 0.0))
                else None
            )
        )
        root = next(
            (node for node in taxonomy.categories if node.name == fallback_root),
            None,
        )
        if root is None or not root.id:
            return None
        base_path = [root.name]
        base_id = root.id
        evidence = _unique_signals(
            [
                *scope.matched_title_signals.get(root.name, []),
                *scope.matched_content_signals.get(root.name, []),
            ]
        )
    leaf = policy.issued if document_number else policy.other
    category_id = f"{base_id}.{leaf.id_suffix}"
    category = next(
        (
            item
            for item in flatten_category_paths(taxonomy)
            if item.category_id == category_id and item.primary_enabled
        ),
        None,
    )
    if category is None:
        return None
    evidence = _unique_signals([*evidence, document_number])
    evidence_items: list[dict[str, Any]] = []
    if document_number:
        evidence_items.append(
            {
                "type": (
                    "metadata"
                    if document_number_source == "filename_document_number"
                    else "text_quote"
                ),
                "page_number": None,
                "sheet_name": None,
                "quote": document_number,
                "signals": [document_number],
                "source": document_number_source,
            }
        )
    return {
        "name": "/".join(category.path),
        "category_id": category.category_id,
        "category_path": category.path,
        "confidence": 0.64 if department is not None else 0.54,
        "status": "SUGGESTED",
        "source": "rule_fallback",
        "purpose_basis": "SCOPED_FALLBACK",
        "evidence": evidence[:5],
        "evidence_items": evidence_items,
        "matched_signals": evidence[:5],
        "matched_title_signals": [],
        "matched_content_signals": evidence[:5],
        "negative_signals": [],
        "organization_scope": base_path[0],
        "candidate_scores": {
            "business": 0.0,
            "scope": scope.scores.get(base_path[0], 0.0),
            "evidence_support": 0.0,
            "fallback": 1.0,
            "department": 1.0 if department is not None else 0.0,
            "document_number": 1.0 if document_number else 0.0,
        },
        "taxonomy_key": taxonomy.key,
        "taxonomy_version": taxonomy.version,
        "candidate_reason": "未命中合格业务分类时，按组织层级、部门和文号使用受控兜底。",
    }


_DEPARTMENT_SUFFIXES = ("处", "部", "办", "科", "室", "中心", "委员会", "工会", "团委")


def _is_department_keyword_match(
    *,
    department: FlattenedCategory | None,
    text: str,
    document_number: str,
) -> bool:
    """仅用该 taxonomy 节点自有词识别部门，排除候选附加的组织词。"""

    if department is None:
        return False
    signals = _unique_signals(
        [
            department.name,
            *(department.aliases or []),
            *(department.positive_signals or []),
        ]
    )
    for signal in signals:
        if any(signal.endswith(suffix) and signal in text for suffix in _DEPARTMENT_SUFFIXES):
            return True
        if any(f"{signal}{suffix}" in text for suffix in _DEPARTMENT_SUFFIXES):
            return True
        if document_number and signal in document_number:
            return True
    return False


def _find_document_number(
    *,
    title_text: str,
    body_text: str,
) -> tuple[str, str]:
    """从文档内标题或正文识别正式文号，并返回可审计来源。"""

    for source, text in (
        ("document_text", body_text),
        ("document_title", title_text),
    ):
        for pattern in _DOCUMENT_NUMBER_PATTERNS:
            match = pattern.search(text or "")
            if match:
                return match.group(0).strip(), source
    return "", ""


def _find_filename_document_number(filename: str) -> tuple[str, str]:
    """只提取文件名中的严格正式文号，普通机构关键词不进入范围落位。"""

    stem = str(filename or "").rsplit(".", 1)[0]
    for pattern in _DOCUMENT_NUMBER_PATTERNS:
        match = pattern.search(stem)
        if match:
            return match.group(0).strip(), "filename_document_number"
    return "", ""


def _is_current_business_candidate(candidate: dict[str, Any]) -> bool:
    """过滤新版分类流程不得新建的历史、兜底和复核候选。"""

    category_id = str(candidate.get("category_id") or "")
    path = list(candidate.get("category_path") or [])
    return bool(
        category_id
        and category_id != "system.other"
        and not category_id.endswith((".other", ".issued"))
        and path[-1:] != ["其他"]
        and str(candidate.get("source") or "") != "rule_fallback"
        and str(candidate.get("status") or "") != "NEEDS_REVIEW"
    )


def _dedupe_and_remove_shorter_embedded_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去除重复路径，并过滤被更长分类名包含的短词误命中。"""

    longest_evidence = [
        str((item.get("evidence") or [""])[0])
        for item in matches
    ]
    filtered: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, ...]] = set()
    for item in matches:
        path = tuple(str(value) for value in item.get("category_path", []))
        evidence = str((item.get("evidence") or [""])[0])
        if path in seen_paths:
            continue
        if any(evidence != other and evidence in other for other in longest_evidence):
            continue
        seen_paths.add(path)
        filtered.append(item)
    return filtered


def _dedupe_candidates_and_remove_shorter_embedded_matches(
    candidates: list[CategoryCandidate],
) -> list[CategoryCandidate]:
    """去除重复路径，候选阶段保留相近分类交给后续判定。"""

    filtered: list[CategoryCandidate] = []
    seen_paths: set[tuple[str, ...]] = set()
    for candidate in candidates:
        path = tuple(candidate.category_path)
        if path in seen_paths:
            continue
        seen_paths.add(path)
        filtered.append(candidate)
    return filtered


def _score_category_candidate(
    *,
    category: FlattenedCategory,
    title_text: str,
    body_text: str,
) -> tuple[
    float,
    list[str],
    list[str],
    list[str],
    list[str],
    list[str],
    float,
    float,
    bool,
]:
    """按标题、正文开头和其余正文的证据位置计算业务主题分。

    文件名、标题、Word 首段、PDF 首页和 Excel 表头更接近文档主旨，因此权重
    高于长正文中的背景描述。其余正文仍可召回候选和生成原文引用，但单个泛词
    不应把“师资引进报告”误判为信息化，也不应把合同任务中的“专业认证”当主旨。
    """

    title_signals = _unique_signals(
        [category.name, *(category.aliases or []), *(category.positive_signals or [])]
    )
    body_signals = title_signals
    matched_title = _matched_configured_signals(title_text, title_signals)
    matched_body = _matched_configured_signals(body_text, body_signals)
    leading_body = body_text[:2_000]
    matched_leading_body = _matched_configured_signals(leading_body, body_signals)
    combined_text = _join_text([title_text, body_text])
    matched_examples = _matched_configured_signals(
        combined_text, _unique_signals(category.examples or [])
    )
    negative_signals = _matched_configured_signals(
        combined_text, _unique_signals(category.negative_signals or [])
    )
    matched_signals = _unique_signals([*matched_title, *matched_body, *matched_examples])
    if not matched_signals:
        return 0.0, [], [], [], negative_signals, [], 0.0, 0.0, False

    score = 0.0
    if _match_configured_signal(title_text, category.name):
        score += 0.34 + min(0.06, len(category.name) * 0.01)
    elif _match_configured_signal(leading_body, category.name):
        score += 0.2 + min(0.04, len(category.name) * 0.01)
    elif _match_configured_signal(body_text, category.name):
        score += 0.06 + min(0.02, len(category.name) * 0.005)

    title_theme_score = _signal_group_score(
        [signal for signal in matched_title if signal != category.name],
        location="title",
    )
    leading_body_score = _signal_group_score(
        [signal for signal in matched_leading_body if signal != category.name],
        location="leading",
    )
    trailing_body_score = _signal_group_score(
        [
            signal
            for signal in matched_body
            if signal != category.name and signal not in matched_leading_body
        ],
        location="trailing",
    )
    score += title_theme_score + leading_body_score + trailing_body_score
    score += min(0.12, 0.06 * len(matched_examples))

    negative_title = _matched_configured_signals(title_text, category.negative_signals or [])
    negative_leading = _matched_configured_signals(
        leading_body, category.negative_signals or []
    )
    negative_conflict = bool(
        negative_title
        or (
            negative_leading
            and title_theme_score < 0.16
            and leading_body_score < 0.16
        )
    )
    score -= min(0.25, 0.08 * len(negative_signals))

    reasons: list[str] = []
    if matched_title:
        reasons.append(f"标题/文件名命中：{'、'.join(matched_title[:5])}")
    if matched_body:
        reasons.append(f"正文命中：{'、'.join(matched_body[:5])}")
    if matched_examples:
        reasons.append(f"示例命中：{'、'.join(matched_examples[:2])}")
    if negative_signals:
        reasons.append(f"负向信号降分：{'、'.join(negative_signals[:5])}")
    return (
        max(0.01, score),
        matched_signals,
        _unique_signals(matched_title),
        _unique_signals([*matched_body, *matched_examples]),
        negative_signals,
        reasons,
        round(title_theme_score, 4),
        round(leading_body_score, 4),
        negative_conflict,
    )


def _signal_group_score(signals: list[str], *, location: str) -> float:
    """按词组专属性和证据位置给业务信号加分，短信号不得主导长文档。"""

    specific = _prefer_specific_signals(signals)
    if location == "title":
        weights = ((6, 0.26), (4, 0.22), (3, 0.16), (0, 0.08))
        cap = 0.46
    elif location == "leading":
        weights = ((6, 0.12), (4, 0.1), (3, 0.07), (0, 0.04))
        cap = 0.3
    else:
        weights = ((6, 0.05), (4, 0.04), (3, 0.025), (0, 0.012))
        cap = 0.12
    score = 0.0
    for signal in specific:
        normalized_length = len(re.sub(r"\s+", "", signal))
        score += next(weight for minimum, weight in weights if normalized_length >= minimum)
    return round(min(cap, score), 4)


def _evidence_support(
    *,
    matched_title_signals: list[str],
    matched_content_signals: list[str],
    negative_signals: list[str],
) -> float:
    """独立记录证据支持度，避免把组织范围或短词直接当成业务置信度。"""

    positive = min(
        1.0,
        0.35 * len(_prefer_specific_signals(matched_title_signals))
        + 0.2 * len(_prefer_specific_signals(matched_content_signals)),
    )
    penalty = min(0.6, 0.15 * len(_prefer_specific_signals(negative_signals)))
    return round(max(0.0, positive - penalty), 4)


def _is_spreadsheet_tutorial(document_features: DocumentFeatures) -> bool:
    """表格函数教程中的示例数据不作为业务分类证据。"""

    filename = document_features.filename.strip()
    return filename.lower().endswith(_SPREADSHEET_EXTENSIONS) and bool(
        _SPREADSHEET_TUTORIAL_PATTERN.search(_join_text([filename, document_features.title]))
    )


def _has_appointment_specific_signal(title_text: str, body_text: str) -> bool:
    """考核聘任必须命中聘任词或明确的考核组合词。"""

    text = _join_text([title_text, body_text])
    if any(
        signal in text
        for signal in (
            "聘任",
            "续聘",
            "岗位聘用",
            "聘用合同",
            "合同期考核",
            "预聘制考核",
            "预聘制博士师资",
        )
    ):
        return True
    if any(subject in text and "考核" in text for subject in ("聘期", "岗位", "履职")):
        return True
    return "教师" in text and "年度考核" in text


@dataclass(frozen=True)
class _OrganizationScopeDecision:
    """学校/学院一级组织范围判定，只约束候选分支，不直接生成分类。"""

    dominant_root: str | None
    scores: dict[str, float]
    matched_title_signals: dict[str, list[str]]
    matched_content_signals: dict[str, list[str]]


def _detect_organization_scope(
    *,
    taxonomy: Taxonomy,
    title_text: str,
    body_text: str,
) -> _OrganizationScopeDecision:
    """先根据 taxonomy 根节点信号判断学校或学院，再进入业务分类。"""

    suppress_body_major_scope = _suppress_body_only_major_scope(
        title_text=title_text,
        body_text=body_text,
    )
    scores: dict[str, float] = {}
    matched_title_by_root: dict[str, list[str]] = {}
    matched_content_by_root: dict[str, list[str]] = {}
    for root in taxonomy.categories:
        if root.name not in {"学校", "学院"}:
            continue
        positive_signals = _unique_signals(
            [root.name, *root.aliases, *root.positive_signals]
        )
        negative_signals = _unique_signals(root.negative_signals)
        matched_title = _prefer_specific_signals(
            [signal for signal in positive_signals if signal in title_text]
        )
        matched_content = _prefer_specific_signals(
            [signal for signal in positive_signals if signal in body_text]
        )
        if root.name == "学院" and suppress_body_major_scope:
            matched_content = [
                signal
                for signal in matched_content
                if signal not in _COLLEGE_SCOPE_IDENTITY_SIGNALS
            ]
        negative_title = _prefer_specific_signals(
            [signal for signal in negative_signals if signal in title_text]
        )
        negative_content = _prefer_specific_signals(
            [signal for signal in negative_signals if signal in body_text]
        )
        score = min(
            0.65,
            sum(_scope_signal_weight(signal, title=True) for signal in matched_title),
        )
        score += min(
            0.3,
            sum(
                _scope_signal_weight(signal, title=False)
                for signal in matched_content
            ),
        )
        score -= min(
            0.65,
            sum(_scope_signal_weight(signal, title=True) for signal in negative_title),
        )
        score -= min(
            0.3,
            sum(
                _scope_signal_weight(signal, title=False)
                for signal in negative_content
            ),
        )
        scores[root.name] = round(max(0.0, score), 4)
        matched_title_by_root[root.name] = matched_title
        matched_content_by_root[root.name] = matched_content

    precedence_root, precedence_signals = _organization_scope_precedence(
        title_text=title_text,
        body_text=body_text,
    )
    if precedence_root is not None:
        # 明确组织身份或发布关系优先于“西安理工大学”这一通用校名。
        # 不清空另一根得分，仍为审计和候选排序保留原始竞争信息。
        scores[precedence_root] = max(scores.get(precedence_root, 0.0), 0.85)
        title_hits = _matched_configured_signals(title_text, precedence_signals)
        content_hits = _matched_configured_signals(body_text, precedence_signals)
        matched_title_by_root[precedence_root] = _unique_signals(
            [*matched_title_by_root.get(precedence_root, []), *title_hits]
        )
        matched_content_by_root[precedence_root] = _unique_signals(
            [*matched_content_by_root.get(precedence_root, []), *content_hits]
        )

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    dominant_root: str | None = None
    if precedence_root is not None:
        dominant_root = precedence_root
    elif ranked:
        best_root, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        # 正文中一个明确的组织短语（如“校属各单位”“计算机学院”）
        # 已足以支持兜底层级；0.18 会要求正文至少同时命中两个信号。
        if best_score >= 0.12 and best_score - second_score >= 0.1:
            dominant_root = best_root
    return _OrganizationScopeDecision(
        dominant_root=dominant_root,
        scores=scores,
        matched_title_signals=matched_title_by_root,
        matched_content_signals=matched_content_by_root,
    )


def _organization_scope_precedence(
    *,
    title_text: str,
    body_text: str,
) -> tuple[str | None, list[str]]:
    """按组织身份和发布/报送角色解决校名、学院名同时出现的冲突。"""

    combined_text = _join_text([title_text, body_text])
    title_college_signals = _prefer_specific_signals(
        _matched_configured_signals(title_text, _COLLEGE_SCOPE_IDENTITY_SIGNALS)
    )
    title_formal_college_signals = _prefer_specific_signals(
        _matched_configured_signals(title_text, _COLLEGE_SCOPE_FORMAL_SIGNALS)
    )
    title_institution_signals = _prefer_specific_signals(
        _matched_configured_signals(title_text, _SCHOOL_SCOPE_INSTITUTION_SIGNALS)
    )
    body_college_signals = _prefer_specific_signals(
        _matched_configured_signals(body_text, _COLLEGE_SCOPE_IDENTITY_SIGNALS)
    )
    if _suppress_body_only_major_scope(title_text=title_text, body_text=body_text):
        body_college_signals = []
    college_signals = _unique_signals(
        [*title_college_signals, *body_college_signals]
    )
    department_signals = _prefer_specific_signals(
        _matched_configured_signals(combined_text, _SCHOOL_SCOPE_DEPARTMENT_SIGNALS)
    )
    institution_signals = _prefer_specific_signals(
        _matched_configured_signals(combined_text, _SCHOOL_SCOPE_INSTITUTION_SIGNALS)
    )
    formal_college_signals = _prefer_specific_signals(
        _matched_configured_signals(combined_text, _COLLEGE_SCOPE_FORMAL_SIGNALS)
    )
    school_wide_signals = _prefer_specific_signals(
        _matched_configured_signals(combined_text, _SCHOOL_SCOPE_WIDE_SIGNALS)
    )

    if college_signals:
        if title_formal_college_signals:
            # 文件名或标题中的学院全称代表文档形成主体，优先于正文背景里的教务处等校级单位。
            return "学院", college_signals
        if title_institution_signals and not title_college_signals:
            # 学校制式表单标题优先于正文中的“所在单位：某学院”等填报字段；
            # 标题同时出现学院全称时已由上一分支按学院处理。
            return "学校", title_institution_signals
        department_is_recipient = _has_scope_recipient_relation(
            combined_text, department_signals
        )
        department_is_publisher = _has_scope_publisher_relation(
            combined_text, department_signals
        )
        if department_is_publisher and not department_is_recipient:
            return "学校", department_signals
        if (
            not title_college_signals
            and not formal_college_signals
            and school_wide_signals
        ):
            # 正文业务字段中的“计算机科学与技术”不能覆盖明确的全校发布
            # 范围；标题/文件名中的专业名以及学院全称仍按学院处理。
            return "学校", school_wide_signals
        # “西安理工大学计算机科学与工程学院”中的校名是学院全称的
        # 上位机构，不与学院范围竞争；学院向校级部门报送时也保持学院。
        return "学院", college_signals

    if department_signals:
        return "学校", department_signals
    if institution_signals:
        return "学校", institution_signals
    return None, []


def _suppress_body_only_major_scope(*, title_text: str, body_text: str) -> bool:
    """职称表中的学科字段是评审对象属性，不是文件形成单位。"""

    if _matched_configured_signals(title_text, _COLLEGE_SCOPE_IDENTITY_SIGNALS):
        return False
    if _matched_configured_signals(body_text, _COLLEGE_SCOPE_FORMAL_SIGNALS):
        return False
    return bool(
        _matched_configured_signals(
            _join_text([title_text, body_text]),
            _TITLE_REVIEW_FORM_SIGNALS,
        )
    )


def _has_scope_publisher_relation(text: str, departments: list[str]) -> bool:
    """识别“教务处下发/通知/部署”等校级部门发布关系。"""

    if not departments:
        return False
    department_pattern = "|".join(
        re.escape(signal) for signal in sorted(departments, key=len, reverse=True)
    )
    action_pattern = "|".join(
        re.escape(action) for action in _SCHOOL_SCOPE_PUBLISH_ACTIONS
    )
    return bool(
        re.search(
            rf"(?:{department_pattern})[^。；;\n]{{0,12}}(?:{action_pattern})",
            text,
        )
    )


def _has_scope_recipient_relation(text: str, departments: list[str]) -> bool:
    """识别学院“向/提交/报送校级部门”，避免把接收方当成文件主体。"""

    if not departments:
        return False
    department_pattern = "|".join(
        re.escape(signal) for signal in sorted(departments, key=len, reverse=True)
    )
    action_pattern = "|".join(
        re.escape(action) for action in _SCHOOL_SCOPE_RECIPIENT_ACTIONS
    )
    return bool(
        re.search(
            rf"(?:{action_pattern})[^。；;\n]{{0,10}}(?:{department_pattern})",
            text,
        )
        or re.search(
            rf"(?:向|给)(?:{department_pattern})[^。；;\n]{{0,8}}(?:{action_pattern})",
            text,
        )
    )


def _scope_signal_weight(signal: str, *, title: bool) -> float:
    """长组织短语比“学校/学院”等泛词更可靠，标题信号高于正文信号。"""

    base = 0.18 if title else 0.08
    return base + min(0.08, len(signal) * 0.01)


def _prefer_specific_signals(signals: list[str]) -> list[str]:
    """同一短语命中时只保留最长表达，避免“全校/面向全校”重复放大。"""

    unique = _unique_signals(signals)
    return [
        signal
        for signal in unique
        if not any(signal != other and signal in other for other in unique)
    ]


def _candidate_to_category(candidate: CategoryCandidate) -> dict[str, Any]:
    """把候选召回结果转换为现有 rule-only 分类建议结构。"""

    return {
        "name": candidate.name,
        "category_id": candidate.category_id,
        "category_path": candidate.category_path,
        "confidence": min(0.95, round(0.45 + candidate.rule_score * 0.5, 2)),
        "status": "SUGGESTED",
        "source": "rule",
        "evidence": candidate.matched_signals[:5],
        "rule_score": candidate.rule_score,
        "matched_signals": candidate.matched_signals,
        "matched_title_signals": candidate.matched_title_signals,
        "matched_content_signals": candidate.matched_content_signals,
        "negative_signals": candidate.negative_signals,
        "organization_scope": candidate.organization_scope,
        "candidate_scores": {
            "rule": candidate.rule_score,
            "business": candidate.business_score,
            "scope": candidate.scope_score,
            "evidence_support": candidate.evidence_support,
            "title_theme": candidate.title_theme_score,
            "leading_body": candidate.leading_body_score,
            "negative_conflict": candidate.negative_conflict,
            "organization": candidate.organization_score,
            "matched_title_signals": candidate.matched_title_signals,
            "matched_content_signals": candidate.matched_content_signals,
            "negative_signals": candidate.negative_signals,
        },
        "taxonomy_key": candidate.taxonomy_key,
        "taxonomy_version": candidate.taxonomy_version,
        "candidate_reason": candidate.candidate_reason,
        "purpose_basis": candidate.purpose_basis,
    }


def _join_text(values: list[str]) -> str:
    """合并文档标题类字段，供候选召回计算。"""

    return "\n".join(value for value in values if value)


def _unique_signals(values: list[str]) -> list[str]:
    """保留顺序去重，避免重复信号放大分数。"""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        signal = value.strip()
        if not signal or signal in seen:
            continue
        seen.add(signal)
        result.append(signal)
    return result


def _other_category(taxonomy: Taxonomy) -> dict[str, Any]:
    """生成无法命中时的兜底分类建议。"""

    system_other = next(
        (
            category
            for category in flatten_category_paths(taxonomy)
            if category.category_id
            == (
                taxonomy.fallback_policy.target_category_id
                if taxonomy.fallback_policy is not None
                else "system.other"
            )
        ),
        None,
    )
    if system_other is not None:
        return {
            "name": "/".join(system_other.path),
            "category_id": system_other.category_id,
            "category_path": system_other.path,
            "confidence": 0.0,
            "status": "SUGGESTED",
            "source": "system_fallback",
            "evidence": [],
            "taxonomy_key": taxonomy.key,
            "taxonomy_version": taxonomy.version,
        }
    return {
        "name": "其他",
        "category_path": ["其他"],
        "confidence": 0.2,
        "status": "SUGGESTED",
        "evidence": [],
        "taxonomy_key": taxonomy.key,
        "taxonomy_version": taxonomy.version,
    }
