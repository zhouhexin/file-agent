# 人事合同优先分类与规章制度误召回修复方案

## 背景与目标

`01引进人才工作合同-王磊磊.doc` 的正文具有合同期限、甲乙双方、聘用和终止等合同结构，但历史候选同时将正文中的“国家规定”召回为“学校/学院—行政管理—规章制度”。该候选不是文件主旨，且容易干扰分类解释。

本次修复目标是：对具有充分正文结构的人才引进合同，优先归入学校或学院的“人事师资—人才工作”，并排除不相容的“规章制度”候选；不改变普通制度、招聘、考核聘任、合同模板或其他现有分类的通用召回逻辑。

## 受限判定规则

1. 先确认人事合同正文结构：正文必须同时出现甲乙双方（或双方）、合同期限/续签/终止/退休年龄之一，以及聘用/聘期/受聘/岗位/教职工之一。
2. 再确认人才引进语义：文件题名或正文开头必须出现“引进人才”“人才引进”或“高层次人才”。题名只用于候选召回；最终候选仍必须有上述正文合同结构作为可定位依据。
3. 仅当上述两项都满足时：
   - 生成 `school.hr.talent-work` 或 `college.hr.talent-work` 强候选；组织范围继续由既有学校/学院范围检测确定。
   - 删除 `school.admin.rules` 与 `college.admin.rules` 候选。
4. 不满足人才引进语义的普通合同不新增“人才工作”结论；不满足完整合同结构的普通“规定”文本也不受本规则影响。

## 修改范围

| 文件 | 修改内容 |
| --- | --- |
| `apps/api/app/modules/classification/matcher.py` | 增加受限的人事合同结构检测、人才引进合同强候选及制度候选抑制。 |
| `apps/api/app/modules/classification/taxonomies/school_file_classification.json` | 补充人才引进合同的精确正向信号、规章制度的精确负向信号，并递增 taxonomy 版本。 |
| `apps/api/app/modules/classification/primary_selection.py` | 业务主类已确定时，防御性地不把 `system.other` 放入次级建议。 |
| `apps/api/app/modules/classification/evidence_reader.py` | 读取历史建议时，若已有有效业务 PRIMARY，隐藏未生效、零置信度的历史 `system.other` 兜底行。 |
| `apps/api/app/tests/test_taxonomy_matcher.py` | 覆盖人才引进合同优先分类及真实制度文件不受影响。 |
| `apps/api/app/tests/test_placement_entrypoints.py` | 覆盖历史兜底建议展示过滤。 |

不涉及数据库迁移、前端路由、MCP Tool 协议或 WorkBuddy 套件。

## 兼容与发布

- 分类目录仍是 source of truth；现有节点 ID 和目录路径不变。
- 本次规则仅影响后续分类或重分类生成的候选。已持久化的旧候选不会被批量改写。
- 部署后可按既有“受控重分类”流程重跑指定文件或目录；重跑前不改变原件、解析页、索引或用户已确认的主类。

## 验收

1. 人才引进合同在学院范围下首选 `college.hr.talent-work`，且候选中不出现 `college.admin.rules` / `school.admin.rules`。
2. 标题和正文具有“管理办法/制度/细则”等规范性结构的真实制度文件仍可召回“规章制度”。
3. 已有正式业务 PRIMARY 的历史分类详情不再展示未生效的零分 `system.other`。
4. 相关 pytest 回归测试通过。
