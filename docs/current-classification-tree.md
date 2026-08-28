# 当前文件分类目录树

- Taxonomy：`unified_school_file_classification`
- 版本：`2026-08-v3`
- 分类节点：60 个
- 根节点：2 个
- 实际候选分类节点：58 个
- 已配置物理路径：58 个

> 方括号内为稳定分类 ID。核对分类结果或数据库记录时，应优先使用分类 ID；`学校`和`学院`是根节点，不直接作为文件分类。全部 58 个候选分类均已配置安全 `organization_path`。

```text
分类目录
├─ 学校 [school]
│  ├─ 行政综合管理类 [school.admin]
│  │  ├─ 发展规划 [school.admin.development-planning]
│  │  ├─ 年度计划、总结 [school.admin.annual-plan-summary]
│  │  ├─ 规章制度 [school.admin.rules]
│  │  └─ 会议纪要 [school.admin.meeting-minutes]
│  ├─ 人事师资 [school.hr]
│  │  ├─ 职称 [school.hr.title-review]
│  │  ├─ 考核聘任 [school.hr.appointment-assessment]
│  │  ├─ 人才工作 [school.hr.talent-work]
│  │  ├─ 师资招聘 [school.hr.faculty-recruitment]
│  │  ├─ 教师发展 [school.hr.faculty-development]
│  │  ├─ 劳资社保 [school.hr.salary-social-security]
│  │  └─ 博士后 [school.hr.postdoc]
│  ├─ 财务 [school.finance]
│  ├─ 后勤资产 [school.logistics-assets]
│  ├─ 本科教学 [school.undergraduate-teaching]
│  ├─ 研究生 [school.postgraduate]
│  ├─ 学科 [school.discipline]
│  ├─ 科研 [school.research]
│  ├─ 党委相关 [school.party]
│  │  ├─ 干部工作 [school.party.cadre-work]
│  │  ├─ 组织 [school.party.organization]
│  │  ├─ 宣传 [school.party.publicity]
│  │  ├─ 统战 [school.party.united-front]
│  │  ├─ 纪委 [school.party.discipline-inspection]
│  │  └─ 工会 [school.party.union]
│  ├─ 国际合作交流 [school.international-cooperation]
│  ├─ 国内合作、校友 [school.domestic-cooperation-alumni]
│  ├─ 审计 [school.audit]
│  ├─ 学生工作 [school.student-affairs]
│  ├─ 安全稳定 [school.safety-stability]
│  ├─ 实验室管理 [school.laboratory-management]
│  └─ 其他 [school.other]
└─ 学院 [college]
   ├─ 党建 [college.party-building]
   ├─ 行政管理 [college.admin]
   │  ├─ 发展规划 [college.admin.development-planning]
   │  ├─ 年度计划、总结 [college.admin.annual-plan-summary]
   │  ├─ 规章制度 [college.admin.rules]
   │  └─ 会议纪要 [college.admin.meeting-minutes]
   ├─ 人事师资 [college.hr]
   │  ├─ 职称 [college.hr.title-review]
   │  ├─ 考核聘任 [college.hr.appointment-assessment]
   │  ├─ 人才工作 [college.hr.talent-work]
   │  └─ 师资招聘 [college.hr.faculty-recruitment]
   ├─ 请示报告 [college.request-report]
   ├─ 财务管理 [college.finance]
   ├─ 教学 [college.teaching]
   ├─ 科研 [college.research]
   ├─ 研究生 [college.postgraduate]
   ├─ 学科 [college.discipline]
   ├─ 学生工作 [college.student-affairs]
   ├─ 学院情况 [college.profile]
   │  ├─ 学院介绍 [college.profile.introduction]
   │  ├─ 机构设置 [college.profile.organization-structure]
   │  └─ 教工信息 [college.profile.faculty-info]
   ├─ 干部任命 [college.cadre-appointment]
   ├─ 学院新闻 [college.news]
   └─ 安全稳定 [college.safety-stability]
```

## 核对说明

- 系统会从根节点以下的分类中选择候选，因此带有子分类的父级分类也可能成为 Top1 分类。
- 实际工作副本目录使用分类名称组成路径，例如：`学校/行政综合管理类/会议纪要`。
- 文件名和目录名只能作为弱信号，最终分类应以正文、OCR、表格单元格等内容证据为准。
- 同一个文件可以有多个分类建议，但当前测试策略会把 Top1 分类作为工作副本的主目录。
