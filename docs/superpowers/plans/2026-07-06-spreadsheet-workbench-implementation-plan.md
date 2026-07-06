# Spreadsheet Workbench Implementation Plan

> 本计划用于把 File Agent 的表格能力升级为统一的 Spreadsheet Workbench。不得复制外部 Skill 内容；只借鉴公开能力方向与工程原则，所有实现按本项目 Tool 白名单、OperationPlan、ChangeSet 和原件保护规则落地。

## Goal

将 `.xlsx`、`.xlsm`、`.csv`、`.tsv` 的读取、Profile、只读分析、编辑计划、公式重算、质量校验纳入同一个表格能力入口。第一阶段先实现低风险只读能力：`profile-spreadsheet`、`validate-spreadsheet`、`.tsv` Profile 支持和 `spreadsheet-workbench/SKILL.md` 编排规则；后续阶段再实现确认后的派生件编辑和 LibreOffice 隔离重算。

## Architecture

```text
用户消息 + 附件
-> ConversationAttachmentContextService 解析真实 document_ids
-> Planner / CapabilityRouter 识别 spreadsheet-workbench 意图
-> profile-spreadsheet 发现 workbook / sheet / schema
-> 分流：
   - 只读分析：analyze-spreadsheet
   - 结构发现：profile-spreadsheet
   - 质量检查：validate-spreadsheet
   - 编辑/建表：OperationPlan -> edit-spreadsheet（后续阶段）
   - 公式重算：LibreOffice worker -> recalculate-spreadsheet（后续阶段）
-> ToolInvocation / Artifact / ChangeSet / 回执
```

## Non-Goals

- 不下载、复制或改写外部 Skill 到仓库。
- 第一阶段不覆盖原始文件。
- 第一阶段不执行宏、不解析外部链接、不运行用户公式脚本。
- 第一阶段不实现真实编辑和 LibreOffice worker，只保留清晰接口边界。

## Phase 1 Scope

- 新增 `apps/api/app/modules/spreadsheet_workbench/`。
- 新增 `profile-spreadsheet` Tool。
- 新增 `validate-spreadsheet` Tool。
- `spreadsheet_analysis.profiler` 支持 `.tsv`。
- `analyze-spreadsheet` 扩展支持 `.tsv` 只读分析。
- Capability Catalog 增加 `spreadsheet_workbench` 元数据。
- Planner 支持用户要求“检查表格、公式错误、引用错误、表结构/schema/sheet”时路由到对应 Tool。
- 新增 `skills/spreadsheet-workbench/SKILL.md`。
- 新增测试覆盖 Tool schema、Planner 路由、TSV Profile、公式错误扫描、只读权限边界。

## Phase 2 Scope

- 新增 `edit-spreadsheet` Tool schema。
- 编辑类请求只生成 OperationPlan，不直接执行。
- 确认后基于 openpyxl 写派生件，不覆盖原件。
- 支持受控操作：`set_value`、`set_formula`、`append_rows`、`insert_rows`、`delete_rows`、`create_sheet`、`rename_sheet`、`copy_cell_style`、`set_number_format`。
- 公式生成优先写 Excel 公式，不能把 Python 算出的结果硬编码进单元格。
- 生成 `SPREADSHEET_DERIVATIVE_CREATED` ChangeItem。

## Phase 3 Scope

- 新增 `recalculate-spreadsheet` Tool。
- 通过 LibreOffice headless 在隔离目录中打开保存派生件。
- 默认 `LIBREOFFICE_ENABLED=false`，未启用时返回结构化 `SKIPPED`。
- 重算后自动调用 `validate-spreadsheet`。
- 超时、失败、宏风险都写结构化 Tool 输出。

## Tool Contracts

### profile-spreadsheet

输入：

```json
{"document_id": "document-uuid"}
```

输出：

```json
{
  "kind": "spreadsheet_profile",
  "ok": true,
  "status": "COMPLETED",
  "document_id": "document-uuid",
  "filename": "demo.xlsx",
  "file_type": ".xlsx",
  "sheets": []
}
```

### validate-spreadsheet

输入：

```json
{"document_id": "document-uuid"}
```

输出：

```json
{
  "kind": "spreadsheet_validation",
  "ok": true,
  "status": "COMPLETED",
  "formula_errors": [],
  "warnings": [],
  "summary": {}
}
```

## Safety Rules

- Tool handler 必须通过 `FileExtractionRepository.resolve_original_file()` 定位原件，不能接受路径参数。
- `profile-spreadsheet` 与 `validate-spreadsheet` 是只读 Tool，`writes=[]`。
- `.xlsm` 只检查和标记宏风险，不执行宏。
- 编辑、转换、重算只能作用于副本或派生件，不能覆盖 `storage/originals/`。
- 后续编辑 Tool 必须由确认后的 OperationPlan 驱动。

## Acceptance Criteria

- `.xlsx`、`.xlsm`、`.csv`、`.tsv` 可以生成表格 Profile。
- `validate-spreadsheet` 可以发现公式错误单元格。
- Planner 能把“检查公式错误/检查表格质量”路由到 `validate-spreadsheet`。
- Planner 能把“看表结构/schema/sheet”路由到 `profile-spreadsheet`。
- `analyze-spreadsheet` 继续负责只读统计分析。
- 后端测试通过：`cd apps/api && /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -v`。
