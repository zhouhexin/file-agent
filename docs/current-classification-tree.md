# 当前文件分类目录（taxonomy v10）

- Source of truth：`apps/api/app/modules/classification/taxonomies/unified_school_file_classification.json`
- taxonomy key：`unified_school_file_classification`
- taxonomy version：`2026-09-v10`
- 新版唯一兜底：`system.other`，显示及物理路径均为“其他”。

本文只列出可作为新候选或新 PRIMARY 的稳定节点。历史 `.issued`、分支 `.other` 和其他 fallback 节点仍可兼容读取，但 `recall_enabled=false`，不得作为新的自动召回或默认落位目标。

```text
学校 [school]
├─ 行政综合管理类 [school.admin]
│  ├─ 发展规划 [school.admin.development-planning]
│  ├─ 年度计划、总结 [school.admin.annual-plan-summary]
│  ├─ 规章制度 [school.admin.rules]
│  └─ 会议纪要 [school.admin.meeting-minutes]
├─ 人事师资 [school.hr]
│  ├─ 职称 [school.hr.title-review]
│  ├─ 考核聘任 [school.hr.appointment-assessment]
│  ├─ 人才工作 [school.hr.talent-work]
│  ├─ 师资招聘 [school.hr.faculty-recruitment]
│  ├─ 教师发展 [school.hr.faculty-development]
│  ├─ 劳资社保 [school.hr.salary-social-security]
│  └─ 博士后 [school.hr.postdoc]
├─ 财务 [school.finance]；后勤资产 [school.logistics-assets]
├─ 本科教学 [school.undergraduate-teaching]
│  └─ 教学评估 [school.undergraduate-teaching.quality-evaluation]
├─ 研究生 [school.postgraduate]；学科 [school.discipline]；科研 [school.research]
├─ 党委相关 [school.party]
│  ├─ 干部工作 [school.party.cadre-work]；组织 [school.party.organization]
│  ├─ 宣传 [school.party.publicity]；统战 [school.party.united-front]
│  ├─ 纪委 [school.party.discipline-inspection]；工会 [school.party.union]
├─ 国际合作交流 [school.international-cooperation]
├─ 国内合作、校友 [school.domestic-cooperation-alumni]
├─ 审计 [school.audit]；学生工作 [school.student-affairs]
├─ 安全稳定 [school.safety-stability]；实验室管理 [school.laboratory-management]
└─ 信息化 [school.digital-services]

学院 [college]
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
│  ├─ 师资招聘 [college.hr.faculty-recruitment]
│  ├─ 教师发展 [college.hr.faculty-development]
│  ├─ 劳资社保 [college.hr.salary-social-security]
│  └─ 博士后 [college.hr.postdoc]
├─ 请示报告 [college.request-report]；财务管理 [college.finance]
├─ 教学 [college.teaching]
│  ├─ 专业认证 [college.teaching.program-accreditation]
│  └─ 教学评估 [college.teaching.quality-evaluation]
├─ 科研 [college.research]；研究生 [college.postgraduate]；学科 [college.discipline]
├─ 学生工作 [college.student-affairs]
├─ 学院情况 [college.profile]
│  ├─ 学院介绍 [college.profile.introduction]
│  ├─ 机构设置 [college.profile.organization-structure]
│  └─ 教工信息 [college.profile.faculty-info]
├─ 干部任命 [college.cadre-appointment]；学院新闻 [college.news]
├─ 安全稳定 [college.safety-stability]
└─ 信息化 [college.digital-services]

参考资料 [reference]
└─ 办公知识与工具 [reference.office-guides]

其他 [system.other]
```

分类选择顺序是：本轮明确目标 → 有效人工 PRIMARY → 经验证的冻结用途包 → 充分正文证据的业务节点 → 有依据的父类 → `system.other`。目录名称、来源路径、扩展名和泛化文种只能帮助候选召回，不能单独证明最终业务分类。每个文件可以保留多个辅助候选，但当前版本只有一个有效 PRIMARY。

用户明确提交 `SET_PRIMARY` 或 `MOVE` 时，后端在验证稳定对象、当前版本、revision、taxonomy、受控目标和冲突后直接执行；不会追加第二次确认。这个例外不适用于删除、覆盖、独立重命名、恢复或外发。
