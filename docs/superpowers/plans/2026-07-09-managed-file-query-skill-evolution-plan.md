# managed-file-query Skill 后续演进计划

## 1. 当前状态

当前 `managed-file-query` 已完成第一阶段能力边界：

- `skills/managed-file-query/SKILL.md` 已定义触发条件、输入输出、Allowed Tools、失败处理、禁止事项和反馈样本格式。
- Planner 中受管目录文件查询的 `selected_skills` 和 step `skill` 已标识为 `managed-file-query`。
- 当前执行实现仍保留在 `apps/api/app/modules/agent/planner.py`，用于稳定解析 root_key、path_prefix、extension 和 filename_contains。
- `feedback-record` 已支持把 `managed-file-query` 的解析纠错样本写入本地 JSONL。
- 反馈样本只用于后续 Skill Candidate 和回归测试，不会自动修改生产规则。

本阶段功能先保持当前实现，不继续扩展自动演进逻辑。

## 2. 当前执行边界

当前线上执行路径仍是：

```text
用户消息
-> deterministic preflight
-> managed-file-query Skill 边界
-> planner.py 生成 managed-file-list Tool 参数
-> Tool schema 校验
-> ManagedFileRepository 查询
-> response 节点生成文件清单
```

当前不做：

- 不让 LLM 直接调用文件系统。
- 不让反馈自动改生产 Skill。
- 不让 Skill 自动写入 Python 代码。
- 不新增数据库迁移保存 Skill Candidate。
- 不把当前 Planner 规则立即迁出到配置文件。

## 3. 后续阶段 1：规则配置化

目标：把当前写在 `planner.py` 中的低风险解析规则逐步迁移到 Skill 规则配置。

新增文件建议：

```text
skills/managed-file-query/rules.json
```

首批配置内容：

- extension aliases：
  - `pdf`
  - `doc/docx/word`
  - `xls/xlsx/xlsm/excel`
  - `csv/tsv`
  - `txt/md`
  - `png/jpg/jpeg`
- filename_contains patterns：
  - `文件名包含{keyword}`
  - `名称里有{keyword}`
  - `名字带有{keyword}`
- path_prefix patterns：
  - `{root_key}下{path}目录中的文件`
  - `{root_key}/{path}下的文件`
- negative patterns：
  - 不把扩展名识别为 path_prefix
  - 不把“文件名包含 xxx”识别为 path_prefix

验收标准：

- `planner.py` 仍负责安全校验和 Tool Plan 生成。
- 规则配置只影响解析候选，不直接执行 Tool。
- 当前 76 条相关测试必须继续通过。
- 增加 rules.json 加载失败兜底测试。

## 4. 后续阶段 2：反馈样本管理

目标：让 `storage/skill-artifacts/managed-file-query-feedback.jsonl` 中的样本可查询、可筛选、可转测试。

建议新增：

```text
GET /api/admin/skills/managed-file-query/feedback-samples
POST /api/admin/skills/managed-file-query/feedback-samples/{sample_id}/mark-reviewed
```

样本字段：

```json
{
  "sample_id": "uuid",
  "skill_id": "managed-file-query",
  "user_id": "user-id",
  "feedback_type": "BAD_SKILL_PARSE",
  "comment": "pdf 被识别成 path_prefix",
  "context_json": {
    "message": "列出 Downloads 下所有 pdf 文件",
    "actual_input": {"path_prefix": "pdf"},
    "expected_input": {"extension": "pdf"}
  },
  "status": "OPEN"
}
```

验收标准：

- admin/ops 可查看样本。
- 普通用户不能查看所有样本。
- API 响应不得泄露本地绝对路径、JWT、API key 或文件正文。
- 样本可以一键导出为 pytest 参数化用例草稿。

## 5. 后续阶段 3：Skill Evaluation

目标：建立固定评测集，防止新增规则导致旧能力退化。

建议新增：

```text
skills/managed-file-query/evals/basic_cases.json
apps/api/app/tests/test_managed_file_query_skill_eval.py
```

评测用例类型：

- root_key 解析。
- path_prefix 解析。
- extension 解析。
- filename_contains 解析。
- extension + filename_contains 组合。
- path_prefix + extension 组合。
- 隐藏文件不展示。
- 未授权路径拒绝。
- 语义模糊请求不应被 deterministic preflight 误拦截。

验收标准：

- 每个 eval case 包含 user_message、expected_tool、expected_input。
- pytest 可离线执行，不访问真实 LLM。
- LLM planner 测试必须使用 deterministic fake。

## 6. 后续阶段 4：Skill Candidate

目标：从反馈样本中生成候选规则变更，但不自动启用。

建议新增：

```text
storage/skill-artifacts/managed-file-query-candidates/
```

候选内容：

```json
{
  "candidate_id": "uuid",
  "skill_id": "managed-file-query",
  "source_sample_ids": ["sample-id"],
  "proposed_rules_patch": {
    "extension_aliases": {},
    "filename_patterns": [],
    "negative_patterns": []
  },
  "status": "DRAFT"
}
```

验收标准：

- Candidate 只生成配置 patch，不修改生产文件。
- Candidate 必须附带来源样本。
- Candidate 必须跑完 Skill Evaluation 才能进入待审核。
- 审核前不得影响线上 Planner。

## 7. 后续阶段 5：人工审核与发布

目标：形成最小 Skill 发布闭环。

建议状态：

```text
DRAFT
EVALUATED
APPROVED
REJECTED
ACTIVE
ROLLED_BACK
```

发布规则：

- 只有 admin/ops 可以审核。
- 审核通过后才更新 `skills/managed-file-query/rules.json`。
- 每次发布记录 rules 版本、测试结果、审核人和发布时间。
- 必须支持回滚到上一版本 rules。

验收标准：

- 生产规则更新前自动跑 eval。
- eval 不通过不能发布。
- 发布后 AgentRun 记录规则版本。
- 回滚后旧请求结果恢复。

## 8. 风险与约束

- 不允许 LLM 直接写 `rules.json` 并生效。
- 不允许用户反馈自动改变文件查询结果。
- 不允许跳过 Tool schema。
- 不允许把受管目录查询扩展成任意文件系统搜索。
- 不允许展示隐藏文件、隐藏目录、宿主机绝对路径。
- 不允许把“重要文件、相关文件、像申请表”这类语义判断硬塞进 deterministic preflight；这些后续走 LLM intent + search Tool。

## 9. 推荐开发顺序

1. 保持当前功能稳定，不继续扩展运行逻辑。
2. 下一次开发先做 `rules.json` 只读加载和回归测试。
3. 再做反馈样本查询 API。
4. 再做 eval case 文件和参数化测试。
5. 最后做 Skill Candidate 和人工审核发布闭环。

