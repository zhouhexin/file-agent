# spreadsheet-workbench

## Trigger

用户请求处理 `.xlsx`、`.xlsm`、`.csv` 或 `.tsv` 表格，包括查看结构、发现 Sheet/字段、只读统计分析、检查公式错误、校验质量、规划编辑、创建表格、转换格式或重算公式。

## Inputs

会话 ID、用户 ID、后端已解析的 `document_ids`、用户目标、目标范围、是否需要确认、表格任务类型。

## Outputs

表格 Profile、只读分析结果、校验报告、OperationPlan、派生件记录、ChangeSet 回执。第一阶段只输出 Profile、分析结果和校验报告。

## Allowed Tools

第一阶段允许：

```text
profile-spreadsheet
analyze-spreadsheet
validate-spreadsheet
operation-plan-create
document-lineage-read
```

后续确认后编辑阶段允许：

```text
edit-spreadsheet
recalculate-spreadsheet
change-report
```

## Open Source Backing

使用开源 Tool Adapter：openpyxl 用于 Excel 结构、公式文本、样式和后续受控编辑；pandas 用于只读数据分析；LibreOffice headless 作为可选公式重算 worker。项目地址分别为：

```text
https://foss.heptapod.net/openpyxl/openpyxl
https://github.com/pandas-dev/pandas
https://www.libreoffice.org
```

## Steps

1. 只使用后端附件解析服务提供的 `document_ids`，不得猜测“刚刚上传”或“上一个文件”。
2. 用户要求统计、汇总、筛选、分组、金额计算时，调用 `analyze-spreadsheet`。
3. 用户要求查看结构、Sheet、字段、表头或 schema 时，调用 `profile-spreadsheet`。
4. 用户要求检查公式错误、引用错误、异常值或质量问题时，调用 `validate-spreadsheet`。
5. 用户要求编辑、创建、转换、覆盖、重算或批量修改时，先生成 OperationPlan；未经确认不得执行。
6. 后续编辑 Tool 必须写派生件，不能覆盖原始文件。

## Evidence Rules

结构发现必须展示文件名、Sheet 名、字段名和行数。校验问题必须定位到 Sheet 和单元格；无法定位时必须明确说明只得到文件级警告。

## ChangeSet Rules

第一阶段 Profile 和 Validation 为只读 Tool，不写 ChangeSet。后续编辑、重算或派生件生成必须写 ChangeSet / ChangeItem，且记录原件未改变。

## OperationPlan Rules

新增列、修改单元格、插入/删除行列、创建 Sheet、转换格式、覆盖文件、重算派生件都必须先创建 OperationPlan。确认前状态只能是 `PLANNED` 或 `WAITING_CONFIRMATION`。

## Failure Handling

不支持的格式返回 `UNSUPPORTED_FILE_TYPE`。本地文件缺失、权限不匹配、路径越界、损坏工作簿、LibreOffice 未启用都返回结构化错误或 `SKIPPED`，不能吞掉失败。

## Tests

必须覆盖 TSV Profile、Excel 公式错误扫描、ToolRegistry 白名单、Planner 路由、Graph 回执和原件不变。

## Forbidden

不得执行宏。不得把本地绝对路径交给 LLM。不得用 `data_only=True` 打开工作簿后再保存。不得覆盖原件。不得让 LLM 输出 Python、SQL、Shell、未校验公式脚本或任意文件路径。
