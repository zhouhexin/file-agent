# 表格指定列逐行提取与路由修复实施方案

> 日期：2026-08-26
> 状态：待实施
> 适用范围：上传的 `.xls`、`.xlsx`、`.xlsm`、`.csv`、`.tsv` 表格
> 关联既有方案：`docs/superpowers/plans/2026-07-06-spreadsheet-workbench-implementation-plan.md`

## 1. 背景与问题复盘

用户针对《2024科研成果资助汇总表.xlsx》输入：

```text
提取文件中的申请人，资助金额这两列
```

对应 AgentRun 为 `9fd7221d-adfa-4027-bbc6-12a31da11c9f`。本次运行没有发生文件解析错误，也不是异步 worker 未启动，而是规划、查询能力和完成判定三层问题叠加：

1. LLM 意图识别结果为 `ANALYZE_SPREADSHEET`，但 `tool_plan_hint` 同时包含 `profile-spreadsheet` 和 `analyze-spreadsheet`。
2. `build_plan_from_user_intent()` 把 `required_capabilities` 与全部 `tool_plan_hint` 合并为一个集合，并且 Profile 分支位于 Analysis 分支之前，因此只要集合中出现 `profile-spreadsheet`，就会提前返回 `PROFILE_SPREADSHEET` 计划。
3. 最终调用的 `profile-spreadsheet` 输入只有 `document_id`，用户问题和“申请人、资助金额”两个目标列没有进入 Tool。
4. Profile Tool 只负责工作簿、Sheet、行数、表头和少量采样值，不具备逐行、同一行关联的列投影语义。
5. Profile 回执格式化器只展示 Sheet、行数和列名，不展示 `sample_values`。这是正确的安全边界，因为分列采样值不能作为正式、逐行对齐的提取结果。
6. 现有 `analyze-spreadsheet` 查询契约只支持计数、求和、平均、最大、最小、筛选和分组，要求每个可执行计划必须有 `metric`，无法表达“返回指定列的原始行”。
7. Agent response 节点把任何表格工作台成功输出直接标记为 `COMPLETED`，没有验证用户要求的列和值是否真正出现在最终结果中。

因此，本问题不能只通过修改一句回复模板解决。需要同时修复路由优先级、受控查询契约、确定性执行、响应格式和目标完成判定。

## 2. 实施目标

完成后，系统必须支持以下自然语言请求：

```text
提取文件中的申请人、资助金额这两列。
列出姓名和身份证号两列。
返回每条记录的项目名称、负责人和批准金额。
从会议 Sheet 中读取申请人、资助金额，最多显示 50 行。
```

核心结果必须满足：

- 使用当前消息附件或后端已解析的会话附件范围，不允许 LLM 猜测 `document_id`。
- 先读取真实 Workbook Profile，再把用户要求映射为真实 `sheet_id` 和 `column_id`。
- 每一行中的多个列值必须来自同一 Sheet、同一源行，不能把各列 `sample_values` 拼接成结果。
- 返回真实 Sheet 名、源行号、列名、值和单元格坐标。
- 空单元格必须明确显示为空，不能丢弃整行或把相邻行的值补过来。
- 多个结构兼容 Sheet 分开显示，不跨 Sheet 猜测行对应关系。
- 没有匹配列、列名歧义、只匹配到部分目标列时必须澄清或标记部分完成。
- 超过单次响应上限时必须明确标记截断，不能把部分结果伪装成完整结果。
- 原始文件保持不变，不生成编辑型 Artifact，不需要 OperationPlan。
- 聚合统计、表结构查看和质量校验的现有行为保持兼容。

## 3. 非目标

本次不实现：

- 表格编辑、公式写入、行列删除或覆盖原文件。
- 任意 SQL、Python 表达式、Excel 公式执行或用户脚本执行。
- 无上限输出整张大表。
- 自动导出新的 Excel/CSV 文件。用户要求导出时仍应按批量导出和 OperationPlan 规则另行设计。
- 图片中表格的 OCR 结构化提取；图片继续走现有 `structured-image-extraction` 能力。
- 把 Profile 的分列采样值升级为正式业务结果。
- 新增数据库表或迁移。

## 4. 总体设计决策

### 4.1 复用 `analyze-spreadsheet`，不新增职责重叠 Tool

保留三个清晰入口：

| Tool | 职责 | 本次变化 |
|---|---|---|
| `profile-spreadsheet` | 查看工作簿、Sheet、表头和列结构 | 不承担数据提取 |
| `analyze-spreadsheet` | 对真实表格执行受控只读查询 | 新增 `project_rows` 逐行列投影模式 |
| `validate-spreadsheet` | 检查公式错误和结构风险 | 不变 |

选择扩展 `analyze-spreadsheet` 的原因：

- 它已经通过 `SpreadsheetAnalysisInput(document_id, question)` 接收用户问题。
- 它已经具备文件权限解析、旧版 XLS 持久化 XLSX 派生件解析、Workbook Profile、LLM 可选规划、Validator 和确定性 Executor 链路。
- 列投影与聚合统计都属于只读表格查询，共用同一安全边界。
- 不增加 `extract-spreadsheet-columns` 与 `analyze-spreadsheet` 两套重复的 Profile、权限和执行逻辑。

### 4.2 Profile 仍是内部发现步骤，不作为用户提取请求的最终 Tool

`analyze-spreadsheet` 内部继续执行：

```text
受控 document_id
-> FileExtractionRepository / ReadableDocumentSourceResolver
-> profile_workbook
-> build_query_plans
-> validate_plan
-> execute_query
-> combine results
-> format response
```

Planner 不需要先显式调用一次 `profile-spreadsheet`。这样可以避免两次读取同一文件，也避免 Profile 的中间结果抢占最终回复。

### 4.3 列投影优先走确定性规划

当用户明确写出真实表头，例如“申请人、资助金额这两列”时，查询规划器应直接根据 Profile 中的真实列名匹配，不调用外部 LLM。

只有用户使用同义表达且无法唯一匹配，例如用户说“姓名、补助”而实际列名是“申请人、资助金额”时，才允许可选 LLM 从 Profile 提供的稳定 `column_id` 候选中选择；LLM 仍不能生成路径、SQL、公式或不存在的列。

这条规则同时降低延迟、减少文件采样信息外发，并确保本次缺陷场景在 `LLM_ENABLED=false` 时也能正确执行。

## 5. 目标架构

```text
用户消息 + 已解析附件 document_ids
-> UserIntentPlan
   intent = EXTRACT_SPREADSHEET_COLUMNS 或 ANALYZE_SPREADSHEET
   required_capabilities = [analyze_spreadsheet]
   tool_plan_hint = [analyze-spreadsheet]
-> CapabilityRouter 按 intent 精确匹配能力
-> Planner 生成 analyze-spreadsheet(document_id, question)
-> SpreadsheetAnalysisService
   -> Workbook Profile
   -> 识别 query_type=project_rows
   -> 将目标列解析为真实 column_id
   -> Validator 校验 Sheet、列、筛选和行数上限
   -> Executor 逐行读取、保留同行关系和单元格坐标
   -> 多 Sheet 结果合并
-> result_summary.spreadsheet_analysis_results
-> 目标完成判定
-> Markdown 表格回执 + 跳过/空值/截断说明
```

## 6. 查询计划契约

### 6.1 扩展 `SpreadsheetQueryPlan`

为保持现有聚合调用兼容，使用同一个严格 Pydantic 模型并新增带默认值的 `query_type`：

```python
class SpreadsheetQueryType(StrEnum):
    """区分聚合计算与逐行列投影，禁止执行任意表达式。"""

    AGGREGATE = "aggregate"
    PROJECT_ROWS = "project_rows"


class ProjectionColumn(StrictModel):
    """用户目标列与真实稳定列 ID 的受控绑定。"""

    requested_name: str = Field(min_length=1, max_length=120)
    column_id: str = Field(min_length=1)


class SpreadsheetQueryPlan(StrictModel):
    query_type: SpreadsheetQueryType = SpreadsheetQueryType.AGGREGATE
    clarification_required: bool = False
    clarification_question: str | None = None
    sheet_id: str | None = None

    # aggregate 模式
    metric: MetricSpec | None = None
    group_by_column_id: str | None = None

    # project_rows 模式
    selected_columns: list[ProjectionColumn] = Field(default_factory=list, max_length=12)
    offset: int = Field(default=0, ge=0, le=10000)

    # 两种模式共用
    filters: list[SpreadsheetFilter] = Field(default_factory=list, max_length=3)
    sort_direction: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=50, ge=1, le=100)
```

模型校验规则：

- `clarification_required=true` 时必须提供一个澄清问题，不执行查询。
- `aggregate` 必须有 `sheet_id` 和 `metric`，不得提供 `selected_columns`。
- `project_rows` 必须有 `sheet_id` 和 1 到 12 个 `selected_columns`，不得提供 `metric` 或 `group_by_column_id`。
- `selected_columns.column_id` 必须唯一。
- `project_rows` 默认保持源文件行顺序；第一阶段不支持按任意列排序，避免引入新的表达式和类型转换歧义。
- `filters` 继续复用白名单操作符 `equals`、`contains`、`in`、`between`。
- 单次最多返回 100 行；大结果使用 `offset + limit` 分页，不能静默丢弃。

### 6.2 确定性列解析

在 `query_planner.py` 增加独立的投影识别步骤，并置于聚合规划之前：

```text
1. 判断是否存在提取动词：提取、列出、返回、展示、读取、导出明细中的“读取明细”语义。
2. 判断是否存在列范围语义：列、字段、栏，或至少两个被真实表头命中的名称。
3. 对每个 Sheet 的真实表头做 Unicode 归一化、空白和常见标点归一化。
4. 从用户原句中匹配完整真实表头，不按业务关键词猜列。
5. 同一 Sheet 中表头唯一且包含全部目标列时生成 project_rows 计划。
6. 一个 Sheet 中同名表头出现多次时返回 NEEDS_CLARIFICATION，不按列位置猜测。
7. Sheet 完全不包含任何目标列时标记 NOT_APPLICABLE；只包含部分目标列时记录 MISSING_COLUMNS。
8. 没有任何 Sheet 能完整解析目标列时返回澄清结果，并展示各 Sheet 可用字段。
```

列名匹配必须优先采用“用户原文包含真实完整表头”。不能把 `sample_values` 用于识别用户要求的列，也不能依据列位置猜测。

### 6.3 LLM 降级规划

更新 `SPREADSHEET_QUERY_PLAN_PROMPT`：

- 增加 `query_type=project_rows` 和 `selected_columns` 输出规则。
- 只能引用 Profile 中存在的 `sheet_id`、`column_id`。
- `requested_name` 只能复述用户请求中的名称，最终展示名称始终来自真实 Profile。
- 如果同义词不能唯一映射，必须返回 `clarification_required=true`。
- 禁止返回单元格地址、文件路径、SQL、Python、公式或任意表达式。
- 不允许用 `sample_values` 直接组成提取结果；样本只可辅助理解列语义。

## 7. Validator 规则

`validate_plan()` 增加投影分支：

- 校验 `sheet_id` 存在。
- 校验所有 `column_id` 都属于该 Sheet。
- 校验列 ID 无重复。
- 校验每个筛选列属于该 Sheet，筛选值符合现有操作符约束。
- 校验 `project_rows` 不含聚合字段。
- 校验 `aggregate` 不含投影字段。
- 校验 `limit <= 100`、`offset <= 10000`。
- 对不存在或歧义列返回 `SpreadsheetPlanValidationError`，由 Service 转换为 `NEEDS_CLARIFICATION`，不能降级成 Profile 成功。

## 8. Executor 与输出契约

### 8.1 逐行执行规则

在 `executor.py` 中保留现有 `iter_data_rows()`，新增 `execute_projection_query()`，由 `execute_query()` 根据 `query_type` 分发：

```text
真实 Sheet
-> 从 header_row + 1 开始逐行读取
-> 应用受控 filters
-> 保持源行顺序
-> 跳过 offset
-> 收集最多 limit 行
-> 每个选中列从同一个 row dict 读取
-> 生成单元格坐标与空值标记
-> 多读一条或依据匹配计数判断 truncated
```

行保留规则：

- 只要所选列中至少一个值非空，就保留该行。
- 所选列全部为空的行默认不展示，并计入 `rows_ignored_empty_projection`。
- 某一列为空但其他目标列有值时必须保留该行，并把空值显示为 `（空）`。
- 公式单元格沿用 `data_only=True` 的计算缓存值；缓存为空时保持空值并给出提示，不执行公式。
- 日期、数值、布尔值统一经过安全显示转换，不能由 LLM格式化或计算。

### 8.2 单 Sheet 输出

```json
{
  "kind": "spreadsheet_analysis",
  "query_type": "project_rows",
  "ok": true,
  "status": "COMPLETED",
  "sheet_id": "sheet_1",
  "sheet_name": "论文",
  "requested_columns": ["申请人", "资助金额"],
  "columns": [
    {
      "column_id": "sheet_1_col_2",
      "column_name": "申请人",
      "requested_name": "申请人",
      "value_type": "string"
    },
    {
      "column_id": "sheet_1_col_7",
      "column_name": "资助金额",
      "requested_name": "资助金额",
      "value_type": "number"
    }
  ],
  "rows": [
    {
      "row_number": 2,
      "cells": [
        {
          "column_id": "sheet_1_col_2",
          "column_name": "申请人",
          "cell": "B2",
          "display_value": "都双丽",
          "is_empty": false
        },
        {
          "column_id": "sheet_1_col_7",
          "column_name": "资助金额",
          "cell": "G2",
          "display_value": "500",
          "is_empty": false
        }
      ]
    }
  ],
  "rows_scanned": 2,
  "rows_matched": 2,
  "rows_returned": 2,
  "rows_ignored_empty_projection": 0,
  "offset": 0,
  "limit": 50,
  "truncated": false,
  "warnings": []
}
```

`cells.cell` 是证据定位，不得包含服务器路径。`display_value` 是确定性读取结果，不允许由 LLM补写。

### 8.3 多 Sheet 合并

扩展 `SpreadsheetAnalysisService._combine_sheet_results()`：

- `aggregate` 继续使用现有聚合合并逻辑，避免回归。
- `project_rows` 不做跨 Sheet 行合并，返回有序 `sheet_results`。
- 每个 Sheet 单独保留列定义、行号和单元格证据。
- 完全没有任何目标列的 Sheet 写入 `skipped_sheets`，原因是 `NOT_APPLICABLE`。
- 只命中部分目标列的 Sheet 写入 `skipped_sheets`，原因是 `MISSING_COLUMNS`，并列出缺失列。
- 多文件任务仍由 Agent 的多个 ToolInvocation 分文件展示，不跨文件合并行。

合并输出摘要至少包含：

```json
{
  "query_type": "project_rows",
  "status": "COMPLETED",
  "requested_columns": ["申请人", "资助金额"],
  "sheet_results": [],
  "skipped_sheets": [],
  "total_rows_returned": 3,
  "truncated": false
}
```

## 9. 状态与目标完成判定

不能再用“Tool handler 正常返回”替代“用户目标已经完成”。状态规则如下：

| 条件 | Tool 业务状态 | AgentRun 最终状态 | 用户说明 |
|---|---|---|---|
| 至少一个 Sheet 完整解析全部目标列，结果未截断 | `COMPLETED` | `COMPLETED` | 展示明确列和值 |
| 兼容 Sheet 有数据但某个相关 Sheet 只匹配部分列 | `PARTIAL` | `NEEDS_REVIEW` | 展示已提取内容和缺失列 |
| 返回行达到上限，后面仍有数据 | `PARTIAL` | `NEEDS_REVIEW` | 标明仅展示第几到第几行 |
| 没有任何 Sheet 能完整匹配目标列 | `NEEDS_CLARIFICATION` | `NEEDS_REVIEW` | 列出可用字段并只问一个问题 |
| 列存在但没有符合筛选条件的行 | `COMPLETED` | `COMPLETED` | 明确“没有符合条件的数据” |
| 文件读取、计划或执行异常 | `FAILED` | `FAILED` | 返回结构化错误 |

补充规则：

- 完全不包含任何目标列的辅助 Sheet 视为 `NOT_APPLICABLE`，不会单独把整个任务降级为 `PARTIAL`，但回执应说明已跳过。
- 用户请求的每个目标列必须出现在 `requested_columns` 和至少一个兼容 Sheet 的 `columns` 中，否则不能标记完成。
- `result_summary` 继续只保存本次运行的轻量结构化结果；最多 100 行的上限防止把大表全文写入 AgentGraphState/checkpoint。
- `graph.py` 的表格响应分支必须根据业务状态映射 AgentRun 状态，不能固定返回 `COMPLETED`。

## 10. 路由修复

### 10.1 LLM 意图提示词

修改 `apps/api/app/modules/llm/prompts.py`：

- 增加 `EXTRACT_SPREADSHEET_COLUMNS` 意图说明。
- “提取、列出、返回指定列/字段的逐行值”必须选择 `analyze_spreadsheet` 和 `analyze-spreadsheet`。
- 明确 `profile-spreadsheet` 只用于用户要求查看结构、Sheet、表头或列定义时。
- 明确分析/提取请求不需要把 `profile-spreadsheet` 作为前置 Tool hint，因为 `analyze-spreadsheet` 内部会完成 Profile。
- 要求单一用户目标只给出必要 Tool hint，避免同时返回互斥最终能力。

### 10.2 Capability Catalog

修改 `apps/api/app/modules/agent/capabilities/catalog.json`：

- `spreadsheet_analysis` 名称或描述扩展为“表格逐行查询、汇总和统计”。
- intents 增加 `EXTRACT_SPREADSHEET_COLUMNS`。
- examples 增加“提取申请人和资助金额两列”。
- `tool_names` 仍只有 `analyze-spreadsheet`。
- `spreadsheet_workbench` 保持 Profile/Validate 边界，不加入列提取 intent。

### 10.3 CapabilityRouter 冲突决策

当前 Router 按 catalog 顺序返回第一个相交能力，容易把配置顺序变成隐式优先级。调整为候选评分：

```text
标准 intent 精确匹配：100
intent alias 匹配：90
required_capabilities 匹配：50
tool_plan_hint 匹配：20
附件类型兼容：仅作为过滤条件，不单独决定能力
```

同分且属于不同最终 Tool 时返回 `None` 交给 Planner 的确定性语义纠偏或澄清，不能按 catalog 顺序猜测。

对于本次冲突输入：

```json
{
  "intent": "ANALYZE_SPREADSHEET",
  "required_capabilities": ["spreadsheet_analysis"],
  "tool_plan_hint": ["profile-spreadsheet", "analyze-spreadsheet"]
}
```

必须选择 `analyze-spreadsheet`，因为 intent 精确匹配高于冲突 Tool hint。

### 10.4 Planner 分支顺序

修改 `build_plan_from_user_intent()`：

1. 表格质量校验的明确 intent/关键词仍优先进入 `validate-spreadsheet`。
2. `ANALYZE_SPREADSHEET`、`EXTRACT_SPREADSHEET_COLUMNS` 或明确逐行提取语义进入 `analyze-spreadsheet`。
3. 只有明确 `PROFILE_SPREADSHEET` 或纯结构查看语义才进入 `profile-spreadsheet`。
4. 不再把全部 `tool_plan_hint` 与 `required_capabilities` 无差别合并后直接决定 Profile 分支。
5. `_has_spreadsheet_analysis_intent()` 增加受限的逐行提取检测：必须同时存在表格附件，以及提取动词与列/字段语义；不能把普通“读取文件正文”误判为表格列投影。

计划输出：

```json
{
  "intent": "EXTRACT_SPREADSHEET_COLUMNS",
  "slots": {
    "document_ids": ["document-uuid"],
    "question": "提取文件中的申请人，资助金额这两列",
    "requested_outputs": ["spreadsheet_row_projection"]
  },
  "steps": [
    {
      "tool_name": "analyze-spreadsheet",
      "input": {
        "document_id": "document-uuid",
        "question": "提取文件中的申请人，资助金额这两列"
      },
      "writes": []
    }
  ]
}
```

## 11. 最终回执设计

`format_spreadsheet_analysis_response()` 根据 `query_type` 分流：

- `aggregate` 使用现有统计格式，不改变既有输出。
- `project_rows` 使用 Markdown 表格逐 Sheet 展示。

目标示例：

```text
已从《2024科研成果资助汇总表.xlsx》中提取“申请人、资助金额”，共 3 行。

Sheet“论文”
| 源行 | 申请人 | 资助金额 |
|---:|---|---:|
| 2 | 都双丽 | 500 |
| 3 | 都双丽 | 3000 |

Sheet“会议”
| 源行 | 申请人 | 资助金额 |
|---:|---|---:|
| 2 | （空） | 1000 |

已跳过 Sheet“Sheet2”：未包含目标列。
原始文件未发生变化。
```

格式化规则：

- 列顺序按用户请求顺序，不按源文件物理列顺序擅自重排。
- 每个 Sheet 都展示“源行”，便于定位证据。
- 空值显示 `（空）`。
- Markdown 单元格内容必须转义 `|`、换行和控制字符，避免破坏表格结构。
- 大结果只展示本次页，明确 `offset`、`limit` 和剩余数据提示。
- 不展示服务器绝对路径。
- 不使用 Profile `sample_values` 作为结果或回退内容。

前端现有文本消息卡可以直接渲染 Markdown 表格，本阶段不要求新增专用前端卡片。若当前 Markdown 渲染器不支持表格，则前端只需补充 GFM table 支持，不改变后端结果契约。

## 12. 审计、持久化与安全

- 文件访问继续通过 `FileExtractionRepository.resolve_original_file()` 和 `ReadableDocumentSourceResolver`，Tool 输入不接受路径。
- `.xls` 继续消费已持久化的 `.xlsx` 派生件，原件不覆盖。
- `.xlsm` 不执行宏。
- 列投影不修改文件，不生成编辑型 Artifact，不需要 OperationPlan。
- `ToolInvocation` 必须记录 `tool_name`、脱敏输入摘要、业务状态、耗时和行数统计。
- 日志只记录 `query_type`、目标列数量、匹配 Sheet 数量、返回行数、截断状态和错误码，不记录具体单元格值、申请人姓名或资助金额。
- `ToolInvocation.output_json` 和 `AgentGraphState.result_summary` 都受 100 行上限约束。
- 现有聚合结果可以继续调用 `persist_deterministic_calculation()`；`project_rows` 不是计算结果，不应写成 deterministic calculation QA 记录。
- 本次无需数据库迁移；AgentRun 与 ToolInvocation 使用现有 JSON 字段保存结构化结果。

建议新增结构化日志字段：

```text
query_type
requested_column_count
resolved_column_count
compatible_sheet_count
skipped_sheet_count
rows_scanned
rows_returned
truncated
```

## 13. 具体文件变更

### 13.1 Agent 与 LLM 路由

- 修改 `apps/api/app/modules/llm/prompts.py`
  - 增加指定列提取的 intent 和 Tool 选择约束。
- 修改 `apps/api/app/modules/agent/capabilities/catalog.json`
  - 扩展 spreadsheet analysis 能力描述、intent 和示例。
- 修改 `apps/api/app/modules/agent/capability_router.py`
  - 从 catalog 顺序匹配改为显式评分和冲突处理。
- 修改 `apps/api/app/modules/agent/planner.py`
  - 调整表格 Profile/Analysis 分支优先级。
  - 新增逐行列投影语义识别。
  - 设置 `requested_outputs=["spreadsheet_row_projection"]`。

### 13.2 表格查询链路

- 修改 `apps/api/app/modules/spreadsheet_analysis/schemas.py`
  - 新增 `SpreadsheetQueryType`、`ProjectionColumn` 和投影字段。
- 修改 `apps/api/app/modules/spreadsheet_analysis/query_planner.py`
  - 新增确定性列投影计划和 LLM 降级规则。
- 修改 `apps/api/app/modules/spreadsheet_analysis/validator.py`
  - 校验投影模式、列归属、唯一性和分页限制。
- 修改 `apps/api/app/modules/spreadsheet_analysis/executor.py`
  - 新增逐行列投影执行和单元格证据。
- 修改 `apps/api/app/modules/spreadsheet_analysis/service.py`
  - 合并多 Sheet 投影结果、跳过原因和业务状态。
- 修改 `apps/api/app/modules/spreadsheet_analysis/formatter.py`
  - 输出明确的逐 Sheet Markdown 数据表。

### 13.3 Runtime、Tool 与回执

- 修改 `apps/api/app/modules/agent/tool_registry.py`
  - 投影结果不调用 deterministic calculation 持久化。
  - 增加安全日志摘要。
- 修改 `apps/api/app/modules/agent/graph.py`
  - 根据表格业务状态设置 AgentRun 最终状态。
  - 保持从 `result_summary` 消费结果，不重新扫描原始 Tool 输出。
- 视现有回执测试结果修改 `apps/api/app/modules/agent/file_task_receipt.py`
  - 确保 `project_rows` 仍归类为 Spreadsheet 任务，并保留逐文件状态。
- 修改 `skills/spreadsheet-workbench/SKILL.md`
  - 增加指定列提取的触发条件、Tool 白名单、失败和验收规则。

### 13.4 测试

- 修改 `apps/api/app/tests/test_spreadsheet_analysis.py`
- 修改 `apps/api/app/tests/test_agent_runtime.py`
- 修改 `apps/api/app/tests/test_persistent_runtime.py`
- 修改 `apps/api/app/tests/test_file_task_receipt_presentation.py`
- 必要时修改 `apps/api/app/tests/test_spreadsheet_workbench.py`，保护 Profile 仍不承担正式数据提取。

## 14. 分阶段实施任务

### Task 1：先补本次缺陷回归测试

新增一个与实际文件结构等价的临时工作簿：

- `论文`：两行，包含“申请人、资助金额”。
- `Sheet2`：不包含目标列。
- `会议`：一行，申请人为空、资助金额为 1000。

先写失败测试，固定以下期望：

- 冲突 hint 下必须选择 `analyze-spreadsheet`。
- Tool 输入必须保留完整用户问题。
- 最终回复必须出现“申请人”“资助金额”“都双丽”“500”“3000”“1000”。
- “会议”空申请人必须显示为空，不能错配。
- 不得调用 `profile-spreadsheet` 作为最终 Tool。
- AgentRun 不得仅因 Profile 成功而标记完成。

### Task 2：实现查询 schema 与 Validator

先完成 `query_type`、`selected_columns`、`offset` 和模式互斥校验，确保执行器只接收严格计划。

验收：

- 旧聚合计划不写 `query_type` 时仍按 `aggregate` 解析。
- 投影计划能通过 schema。
- 同时提交 `metric` 与 `selected_columns` 必须失败。
- 不存在、跨 Sheet、重复的列 ID 必须失败。

### Task 3：实现确定性列解析与执行器

完成真实表头匹配、同行投影、空值、证据坐标和分页。

验收：

- 关闭 LLM 时也能执行“提取申请人、资助金额这两列”。
- 返回值来自同一源行。
- CSV/TSV/XLSX 与持久化 XLSX 派生件复用同一结果契约。
- 重复表头或无法唯一解析时要求澄清。

### Task 4：实现 Service 多 Sheet 合并与状态

按 `query_type` 分发合并策略，聚合逻辑保持原样，投影逻辑按 Sheet 展示。

验收：

- 多 Sheet 不跨表拼接行。
- 完全无目标列的 Sheet 被明确跳过。
- 部分列匹配和截断返回 `PARTIAL`。
- 无兼容 Sheet 返回 `NEEDS_CLARIFICATION`。

### Task 5：修复 LLM、CapabilityRouter 和 Planner 路由

完成 intent 规则、候选评分和 Planner 分支优先级。

验收：

- `ANALYZE_SPREADSHEET + [profile-spreadsheet, analyze-spreadsheet]` 选择分析 Tool。
- “查看有哪些字段”仍选择 Profile。
- “检查公式错误”仍选择 Validate。
- “提取两列”选择 Analyze，并保留 question。
- 普通 Word/PDF“提取正文”不误走表格查询。

### Task 6：实现响应和目标完成判定

完成 Markdown 表格、空值、跳过 Sheet、截断提示和 AgentRun 状态映射。

验收：

- 最终回复明确展示目标列和值。
- Profile sample 不进入正式答案。
- `PARTIAL`、`NEEDS_CLARIFICATION` 不显示为 AgentRun `COMPLETED`。
- 原始文件未发生变化的说明保留在统一任务回执中。

### Task 7：更新 Skill 文档并执行回归

同步更新 `skills/spreadsheet-workbench/SKILL.md`，然后执行局部、模块和全量回归。

## 15. 自动化测试矩阵

### 15.1 Schema 与 Validator

- `aggregate` 旧计划兼容。
- `project_rows` 正常计划。
- 空 `selected_columns` 拒绝。
- 超过 12 列拒绝。
- 重复列 ID 拒绝。
- 不存在列 ID 拒绝。
- 跨 Sheet 列 ID 拒绝。
- 投影与聚合字段混用拒绝。
- `limit > 100`、非法 offset 拒绝。

### 15.2 Query Planner

- 精确中文表头：申请人、资助金额。
- 英文表头和 CSV/TSV。
- 多 Sheet 同结构生成多个计划。
- 某 Sheet 完全无关时跳过。
- 某 Sheet 只包含部分目标列时报告缺失。
- 同名重复表头要求澄清。
- LLM 关闭时精确表头仍可执行。
- Fake LLM 只能选择 Profile 中存在的列 ID。
- Fake LLM 编造列 ID 被 Validator 拒绝。

### 15.3 Executor

- 多列值保持同行关系。
- 单列空值但其他列非空时保留行。
- 所选列全空时按规则跳过并计数。
- 单元格坐标准确。
- 日期、金额、布尔值安全显示。
- 筛选后投影。
- offset/limit 正确。
- 截断标志正确。
- XLSX、CSV、TSV 一致。

### 15.4 路由与 Agent Runtime

- 本次真实冲突 hint 回归。
- 纯 Profile 请求回归。
- 聚合统计请求回归。
- Validate 请求回归。
- 多附件每文件一个 ToolInvocation。
- 用户问题完整传入 Tool。
- `PARTIAL` 映射为 `NEEDS_REVIEW`。
- `NEEDS_CLARIFICATION` 不执行第二个猜测 Tool。

### 15.5 Formatter 与回执

- Markdown 表头和值完整。
- 空值显示。
- `|`、换行转义。
- 多 Sheet 分块。
- 跳过 Sheet 说明。
- 截断和分页说明。
- 不泄漏本地路径。
- 不展示 `sample_values` 作为正式结果。

## 16. 建议验证命令

在 `apps/api` 目录使用当前已配置的 Python 环境执行：

```bash
python -m pytest -v app/tests/test_spreadsheet_analysis.py
python -m pytest -v app/tests/test_spreadsheet_workbench.py
python -m pytest -v app/tests/test_agent_runtime.py -k "spreadsheet or capability_router"
python -m pytest -v app/tests/test_persistent_runtime.py -k spreadsheet
python -m pytest -v app/tests/test_file_task_receipt_presentation.py -k spreadsheet
```

局部测试通过后执行：

```bash
python -m pytest -v
```

如果未修改前端，不要求因为本任务单独执行前端构建；如果为 Markdown 表格增加前端 GFM 支持，则必须执行：

```bash
cd apps/web
npm run build
```

## 17. 手工验收用例

### 用例 A：复现文件

1. 上传《2024科研成果资助汇总表.xlsx》。
2. 输入“提取文件中的申请人，资助金额这两列”。
3. 确认 AgentRun 的 intent 为 `EXTRACT_SPREADSHEET_COLUMNS` 或受控等价意图。
4. 确认唯一最终数据 Tool 是 `analyze-spreadsheet`。
5. 确认 Tool 输入包含原始 question。
6. 确认回复按 Sheet 展示“申请人、资助金额”和具体值。
7. 确认会议 Sheet 的空申请人显示为 `（空）`。
8. 确认无目标列的 Sheet2 被明确跳过。
9. 确认原始文件未变化。

### 用例 B：纯结构查看

输入“这个 Excel 有哪些 Sheet 和字段”，必须调用 `profile-spreadsheet`，只展示结构，不伪装成数据提取。

### 用例 C：聚合回归

输入“统计每个申请人的资助金额合计”，必须继续调用 `analyze-spreadsheet` 的 `aggregate` 模式，并由确定性 Executor 计算。

### 用例 D：歧义列

构造包含两个同名“金额”列的表格，输入“提取金额列”，系统必须要求用户明确具体列，不按第一列猜测。

### 用例 E：大表

构造超过 100 行的表格，确认只返回本页数据，并把 Tool 与 AgentRun 状态标为 `PARTIAL/NEEDS_REVIEW`，明确提示下一页范围。

## 18. 发布与回滚

### 发布顺序

1. 先合入 schema、Validator、Executor 和单元测试。
2. 再合入 Query Planner、Service 与 Formatter。
3. 最后切换 LLM Prompt、CapabilityRouter 和 Planner 路由。
4. 完成局部回归和全量后端测试后发布。

这样可以避免路由先切到一个尚未支持投影的执行器。

### 兼容策略

- `query_type` 默认是 `aggregate`，既有测试、Fake LLM 和历史调用方无需立即补字段。
- 既有聚合输出结构和格式化逻辑保持不变。
- API 路由和 `SpreadsheetAnalysisInput(document_id, question)` 不变。
- 不做数据库迁移，历史 AgentRun 仍可读取。

### 回滚策略

- 回滚 Planner/Prompt 后，新请求恢复旧聚合与 Profile 行为。
- Schema 默认聚合，因此即使保留新增字段代码，也不会影响旧调用。
- 本功能只读且不修改原件，不涉及数据回滚。

## 19. 风险与控制

| 风险 | 控制措施 |
|---|---|
| 重复表头导致错列 | Validator 和 Planner 遇到同名多列必须澄清 |
| 分列采样值被误当同行数据 | Executor 只从同一个 row dict 读取；Formatter 禁止 sample 回退 |
| 大表撑大 Agent State | 单次最多 100 行，分页并标记截断 |
| LLM 编造列或 Sheet | 只允许稳定 ID，Validator 对真实 Profile 校验 |
| 用户 PII 进入日志 | 日志只记数量和状态，不记单元格值 |
| 公式值过期或缺缓存 | `data_only=True` 只读缓存，空缓存明确提示，不执行公式 |
| Profile hint 再次抢占 | intent 精确匹配、候选评分和 Planner 分支测试三层保护 |
| 辅助 Sheet 无目标列 | 标记 NOT_APPLICABLE 并明确跳过，不跨 Sheet 猜测 |
| 前端不支持 Markdown table | 验收时确认；必要时仅补 GFM table 渲染并执行前端构建 |

## 20. 完成定义

以下条件全部满足才算修复完成：

- 本次实际问题的回归测试通过。
- 指定列提取在 LLM 关闭时可确定性完成。
- `profile-spreadsheet` 不再抢占分析或提取请求。
- 返回结果逐行对齐，包含 Sheet、行号、目标列和值。
- 空值、缺列、歧义、无数据和截断都有明确状态与回执。
- 聚合、Profile、Validate 既有测试不回归。
- Tool/Agent 状态不再把未满足用户目标的结果标记为完成。
- 原始文件保持不变，不执行宏，不泄漏路径或具体单元格值到日志。
- 局部测试和后端全量测试通过；若有前端 Markdown 变更，前端构建通过。
- `skills/spreadsheet-workbench/SKILL.md` 与最终实现保持一致。
