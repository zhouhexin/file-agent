# WorkBuddy Excel 网格归档 Skill 集成记录

日期：2026-09-29。来源为用户提供的 `file-agent-excel-grid-archive` 草案及 `changes.diff`。集成到
`integrations/workbuddy/file-agent-plugin/skills/file-agent-excel-grid-archive/`，并由 A–I 定向填表、
盖章模板、累计表和通用文档提取 Skill 在目标 Excel 归档环节引用。插件版本为 0.1.46。

## 契约调整

草案的核心做法可用：从已授权电子表格工具返回的真实 0-based 网格确定性生成表头单元格坐标，并在提交前
检查。原补丁不能原样落地，因为 `DocumentExtractionBlock` 不接受 `sheet_name` 字段，工作表名应放在
`DocumentExtractionUnit.sheet_name`；而把多行表头的换行转成字面 `\\n` 会让服务端与真实转换后单元格
文字的核对失败。集成脚本保留单元格原文和真实字符偏移，以 `TABLE_CELL.row_index/column_index` 表示
物理行列，不再通过 `unit.text.splitlines()` 推断 Excel 行号。

`--require-a-i` 仅供明确只填 A–I 的任务使用；普通 Excel 目标可有其他列宽。脚本拒绝重复或负数坐标、
被 `--rows/--cols` 截断的网格、越界或空表头、超出服务端正文/块上限的结果。它输出符合现行
`DocumentExtractionUnit` schema 的 `<前缀>_unit.json`、单独正文和块清单。所有真实工作表仍须分别报告；
非目标说明页不能伪造表头块，空表须用 `BLANK` 状态和真实原因。

## 验证与使用边界

新增测试用带真实换行的多层表头、数值序号和 0-based 网格生成 unit，并用服务端 Pydantic 契约验证；
同时检查 A–I 限制只在明确启用时生效，重复坐标和截断网格会拒绝。插件打包脚本检查新 Skill 和脚本均在
发行包中。脚本只转换宿主已经读出的网格，不解析 `.xls` 字节，不调用 OCR，不读取任意聊天路径，也不写
目标 Excel。真实 WorkBuddy 运行仍需要宿主可用的逐格电子表格读取能力；若工具无法提供可靠坐标，停止。

既有 `document_extraction_submit` 已完成的 run 不能用新载荷覆盖。过去漏提交表头坐标的目标需走受控
重新读取/重处理流程；安装新 Skill 不会修改既有归档结果或数据库。

## 2026-09-29 实际 checkout 失败修复

指定 job `34d2c77d-1d8b-44f2-be25-e587b71c78b5` 的日志显示 LibreOffice 转换成功，随后在读取目标 schema 时因 checkout 请求的 `sheet_name=null`、`layout=null` 抛出 `TARGET_EXCEL_LAYOUT_REQUIRED`。WorkBuddy 已提交的网格从第 2 行开始；服务端保留的转换派生件经只读验证，`A2:AA4` 的 27 列表头、`A5:AA6` 的预留数据区可读取，表头非空格与网格坐标无缺失或不一致。

0.1.47 Skill 要求 checkout 传入真实 `sheet_name` 和 layout；API 在旧 XLS 转换任务入队前检查已提交网格，发现首行不构成完整字段表头时直接返回 `TARGET_EXCEL_LAYOUT_REQUIRED`。worker 同时将该确定性错误码安全返回，避免仅给出“格式提升失败”。已失败的 job 和幂等请求键保持不可变；修正参数后使用新的 `request_key`，不能重用旧键。此次本地检查没有运行真实 WorkBuddy OCR 或发布，完整宿主验收仍须在更新 API、worker、MCP 与插件后执行。

本轮已归档目标若仍是上述固定版本，在 WorkBuddy 新会话中可从以下 checkout 参数继续测试；应由 WorkBuddy 调用 MCP 工具，不能把 JSON 当作服务器本地命令执行：

```json
{
  "filename": "申报人员业绩汇总-教学科研型教授.xls",
  "request_key": "wb-ai-scoped-excel-checkout-2",
  "uploaded_document_id": "a5e2efef-2ca3-4186-8b33-37d14c43d94c",
  "uploaded_document_version_id": "e4643f4d-533c-46a6-9f12-b285c114c008",
  "uploaded_extraction_run_id": "0637de24-0b08-430f-82e4-06946e85f50c",
  "sheet_name": "教学科研教授",
  "layout": {"header_range": "A2:AA4", "data_range": "A5:AA6"}
}
```

如果版本已变化或本轮重新上传，应使用新归档任务返回的三个 ID 与新请求键，并重新核对真实工作表名及布局。先等待 `filesystem_job` 成功，以同一新请求键再次 checkout，之后才进行 PDF 定向 OCR、apply 和 publish。
