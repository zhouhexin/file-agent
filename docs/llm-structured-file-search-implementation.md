# LLM 结构化文件检索实施说明

> 日期：2026-08-26
> 状态：已实施
> 适用范围：对话入口的 `hybrid-search` 文件发现链路

## 1. 问题

“帮我找学校的工作总结”原先只把自然语言查询传给后端。确定性解析器虽然能得到
“学校”和“工作总结”，但异常降级使用 2～6 字滑动片段，可能让只出现“工作”的文件进入结果。
取消交集前 30 份候选限制后，一次富化数百个候选还可能触发 PostgreSQL statement timeout。

## 2. 实现边界

LLM 只输出受控 `FileSearchSemanticPlan`，不读取文件系统、数据库或文件正文，也不能生成 SQL、
绝对路径和文件事实。Tool Registry 继续负责 schema 校验、权限范围、真实检索和结果审计。

结构化计划包含：

- `core_topics`：不可拆分的完整核心短语，当前只允许 `EXACT_PHRASE`。
- `scope.organization_level`：`ANY`、校级、院级、部门级或专项级。
- `scope.organization_terms`：用户明确给出的真实机构完整名称。
- `preferred_results`：只影响排序，不排除其它高相关文件。
- `group_by`：机构层级、业务主题和年份。
- `response_style`：分组文件列表或平铺列表。

示例：

```json
{
  "core_topics": [
    {"phrase": "工作总结", "required": true, "match_mode": "EXACT_PHRASE"}
  ],
  "scope": {
    "type": "CURRENT_SCHOOL_WORKSPACE",
    "organization_level": "ANY",
    "organization_terms": []
  },
  "preferred_results": [
    {"organization_level": "UNIVERSITY", "boost": 1.0}
  ],
  "group_by": ["organization_level", "business_topic", "year"],
  "response_style": "GROUPED_FILE_LIST"
}
```

## 3. 执行规则

1. “学校的工作总结”中的“学校”默认表示当前学校业务工作区；核心主题是完整“工作总结”。
2. 普通请求使用 `organization_level=ANY`，校级结果优先，但学院、部门和专项总结不会被排除。
3. 只有“只找学校层面/校级”才使用 `UNIVERSITY` 硬过滤。
4. “计算机学院”等明确机构名称进入 `organization_terms`，与核心主题分别完整召回后按文件 ID 求交集。
5. 所有必需短语的召回在交集前不应用普通 30 份候选上限；最终大结果集仍经过现有展示确认门。
6. 候选显示字段按每批 100 个工作副本富化，避免巨大 `IN` 和 JOIN 查询超时。
7. 两阶段检索失败时，摘要降级仍必须验证所有完整受保护短语；n-gram 只能排序，不能放宽准入。
8. 摘要降级结果统一标记为 `POSSIBLE` 和 `partial=true`，不能冒充正文已验证结果。
9. 机构层级、年份、路径和分组由后端确定性生成；LLM 只选择展示偏好。

## 4. 无模型降级

LLM 关闭或输出失败时，Planner 仅为后端可以明确识别的复合文种生成安全计划，例如“工作总结”、
“述职报告”“工作计划”和“会议纪要”。未知主题继续走既有确定性解析，不进行自由语义猜测。

## 5. 验证

回归测试覆盖：

- “工作总结”不会拆成单独“工作”。
- LLM 非法模糊匹配模式被 Pydantic schema 拒绝。
- 多个必需短语执行文件级交集，不会变成 OR。
- 校级偏好保留学院结果；明确校级范围才过滤学院结果。
- 分组回执展示机构层级、主题、年份和逻辑相对路径。
- 大候选富化继续遵守共享工作区、活动工作副本和当前版本边界。
