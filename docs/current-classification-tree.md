# 当前文件分类目录（taxonomy v13）

- Source of truth：`apps/api/app/modules/classification/taxonomies/unified_school_file_classification.json`
- taxonomy key：`unified_school_file_classification`
- taxonomy version：`2026-09-v13`
- 受控范围 fallback：学校/学院各部门下“发文/其他”；最终全局兜底为 `system.other`。

本文列出业务节点和全局兜底。每个 fallback_policy 中登记的学校/学院部门业务父节点会物化“发文/其他”子节点；它们 `recall_enabled=false`，不参加普通 matcher 竞争，但可以在正文已确定组织范围和部门时作为新 PRIMARY 落位。

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

分类选择顺序是：本轮明确目标 → 有效人工 PRIMARY → 经验证的冻结用途包 → 充分正文证据的业务节点 → 有依据的父类 → 已验证组织/部门下的“发文/其他” → `system.other`。普通文件名只能帮助召回；文件名严格文号中的机关前缀若唯一映射到受控部门，可作为窄范围例外确定该部门“发文”，但不能证明具体业务。近似编号或多个部门竞争时不应用。每个文件可以保留多个辅助候选，但当前版本只有一个有效 PRIMARY。

材料包用途继承只接受不可变成员清单：系统从叶目录向上选择最近的具体材料容器，可包含“照片/附件”等子目录，但不会越过年份或宽泛集合根；容器内所有成员均完成分析、冻结文档版本和 SHA，且至少一个成员由原始正文“精确题名＋成组字段”形成唯一用途锚点时，其他成员才可继承。当前结构锚点覆盖人才申报（含高层次人才特殊支持计划、三秦英才、科技创新领军和青年拔尖等真实题名变体）、聘任考核、教师招聘、职称、专业认证、专业/培养/课程建设和科研项目材料；竞争用途、宽泛根目录或不完整成员均不继承。分类器版本刷新会重新尝试包识别，以修复首次分析时因旧锚点范围不足产生的漏建。

组织范围词在 v11 中按角色处理：`西安理工大学计算机科学与工程学院`、计算机相关专业和系所优先进入学院根；教务处、人事处、信息化处等校级部门作为发布主体时进入学校根，作为学院报送接收方时不覆盖学院。组织范围词只决定根，具体业务节点仍须正文业务证据。

用户明确提交 `SET_PRIMARY` 或 `MOVE` 时，后端在验证稳定对象、当前版本、revision、taxonomy、受控目标和冲突后直接执行；不会追加第二次确认。这个例外不适用于删除、覆盖、独立重命名、恢复或外发。
