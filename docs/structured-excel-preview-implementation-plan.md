# Excel 结构化预览实施方案

## 0. 实施记录（2026-08-27）

本方案已按精简边界实现：

- 查重弹窗中的 `.xlsx` 使用 `exceljs` Web Worker 在浏览器本地解析。
- 支持 Sheet 切换、真实行列坐标、行列分页、公式/缓存结果区分、合并单元格和明确截断提示。
- 后端使用 openpyxl 公式视图与缓存值视图生成 `DocumentElement(label=table_cell)`。
- 新增结构化分页接口 `GET /api/files/{document_id}/spreadsheet-preview`。
- 后端逐格事实全工作簿最多保存 100,000 个，超过后保留完整文本投影并在工作表元数据和 API 中明确标记不完整。
- 为控制本次范围，压缩 JSON 全量派生件暂未实现；如实际文件经常超过上述阈值，再独立增加派生件，不提前引入额外存储链路。

## 1. 背景

当前重复文件对比弹窗中的 Excel 内容来自通用文本预览链路：后端把工作表按行拼成制表符文本，前端再按换行和制表符还原表格。该方式只能提供粗略文本查看，不能准确表达 Excel 的单元格结构。

已确认的主要问题包括：

- 日期、百分比、货币、千分位和前导零等显示格式会丢失。
- 公式只读取缓存值；缓存不存在时可能显示为空，且无法区分公式和值。
- 合并单元格、隐藏行列、冻结窗格和工作表维度没有进入预览模型。
- 空行被跳过后，原始行号与视觉位置发生偏移。
- 前端当前只显示前 200 行、前 50 列，超出部分被静默截断。
- 通用预览 API 只返回文本，没有返回后端已经具备的页面元数据。
- 这种文本不能作为后续分类、问答和表格计算的可靠单元格证据。

因此，本次调整不能只修改 CSS 或扩大弹窗，而应增加真正的 Excel 结构化预览模型。

## 2. 目标

本方案同时区分两个目标：

1. **查重弹窗即时预览**：用户上传文件后、尚未完成后台解析时，也能在浏览器本地查看 `.xlsx` 的工作表和单元格结构。
2. **后端持久化事实**：正式解析后保存工作表、单元格坐标、公式、缓存结果和显示值，供分类、检索、问答和证据引用使用。

第一阶段优先解决查重确认弹窗的可视化准确性；后端事实结构作为第二阶段补齐。浏览器预览只用于用户判断，不能替代后端持久化证据。

## 3. 非目标

- 不实现 Excel 在线编辑。
- 不执行 VBA、宏、外部链接、数据连接或嵌入对象。
- 不在浏览器或后端自行计算复杂 Excel 公式。
- 不追求与 Microsoft Excel 像素级一致的排版。
- 第一阶段不支持旧版 `.xls` 的浏览器本地解析；`.xls` 继续走服务端 LibreOffice 派生 `.xlsx` 的既有路径。
- 不因为预览功能修改或覆盖原始文件。

## 4. 推荐总体方案

采用“浏览器即时结构化预览 + 后端持久化结构化事实”的两层方案：

```text
上传中的 .xlsx Blob
  -> 浏览器 Web Worker 本地解析
  -> WorkbookPreview
  -> 查重弹窗结构化表格

已入库 DocumentVersion
  -> 后端 openpyxl 双视图解析
  -> DocumentPage + DocumentElement(table_cell)
  -> 结构化预览 API / 分类 / 检索 / 证据回答
```

两条链路使用相同的核心字段语义，但数据来源不同：

- 本地预览数据是临时数据，关闭弹窗后释放。
- 后端数据是受审计的持久化事实，关联 `DocumentVersion` 和 `extraction_run_id`。

## 5. 第一阶段：浏览器本地结构化预览

### 5.1 解析库

建议使用 `exceljs`，通过动态导入并放入专用 Web Worker 中执行。原因：

- 能读取工作表、单元格、公式、缓存结果、数字格式和合并区域。
- 解析过程可以与 React 主线程隔离，避免大工作簿导致弹窗卡死。
- 只在用户点击 Excel 预览时加载，不增加聊天页首屏执行成本。

不使用在线第三方预览服务，文件内容不离开浏览器和现有后端。

### 5.2 前端结构化数据模型

```ts
type WorkbookPreview = {
  sheets: SheetPreview[];
  warnings: string[];
  truncated: boolean;
};

type SheetPreview = {
  name: string;
  rowCount: number;
  columnCount: number;
  frozenRows: number;
  frozenColumns: number;
  hidden: boolean;
  merges: MergePreview[];
  cells: CellPreview[];
};

type CellPreview = {
  row: number;
  column: number;
  address: string;
  rawValue: string | number | boolean | null;
  displayValue: string;
  valueType: "empty" | "string" | "number" | "boolean" | "date" | "formula" | "error";
  formula?: string;
  cachedResult?: string | number | boolean | null;
  numberFormat?: string;
  mergeRange?: string;
};
```

结构化模型必须保留真实行号、列号和单元格地址，不能再用删除空行后的数组下标代替 Excel 坐标。

### 5.3 公式规则

- 绝不执行工作簿中的公式、宏或外部链接。
- 公式单元格同时保存 `formula` 和 `cachedResult`。
- 有缓存结果时，默认显示缓存结果，并允许用户查看公式。
- 无缓存结果时显示公式，并明确标记“工作簿未保存可用计算结果”。
- 不把空缓存误判为真实空单元格。

如果业务后续必须获得重新计算后的结果，应新增独立的 LibreOffice 隔离重算任务，输出新的派生件；该任务不属于本次结构化预览，且不得覆盖原件。

### 5.4 表格渲染

结构化预览使用专用表格组件，不再复用 `text.split("\n").split("\t")`：

- 顶部显示工作表标签，可切换不同 Sheet。
- 固定显示 Excel 行号和列标。
- 支持合并单元格的 `rowSpan` / `colSpan`。
- 表格宽高跟随弹窗变化，横向和纵向滚动独立存在。
- 大表格使用每页 100 行、50 列的分页窗口渲染，只创建当前区域的 DOM 节点。
- 不再静默截断 200 行和 50 列；达到资源上限时显示明确提示，并允许按区域继续查看。
- 单元格悬停或详情区显示地址、原始值、显示值、公式和格式。
- 默认展示格式化后的 `displayValue`，需要时可切换“显示公式”。

左右对比时，两个文件各自保留 Sheet 选择状态。首版不做同步滚动和自动单元格差异高亮，避免扩大实现范围。

### 5.5 资源与安全边界

- Worker 只接收用户当前选择的本地 Blob，不接收任意文件路径。
- 解析完成或弹窗关闭后立即终止 Worker 并释放 ArrayBuffer。
- 保留现有上传大小限制，并增加可配置的工作表数、有效单元格数和解析时长限制。
- 达到限制时返回部分结果和明确警告，不伪装为完整预览。
- 不加载工作簿中的外部图片、超链接目标、远程资源或数据连接。
- `.xlsm` 可以读取普通单元格时也不得执行或导出宏；首版建议直接降级为安全提示。

## 6. 第二阶段：后端持久化结构化事实

### 6.1 双视图读取

后端解析 `.xlsx` 时使用同一文件的两个只读视图：

```python
formula_book = openpyxl.load_workbook(path, read_only=False, data_only=False)
value_book = openpyxl.load_workbook(path, read_only=False, data_only=True)
```

- `formula_book` 用于获得公式、类型、格式、合并区域和工作表属性。
- `value_book` 用于获得文件中保存的公式缓存结果。
- openpyxl 不负责公式计算；缓存不存在时必须保留“未计算”状态。

为读取合并单元格、隐藏行列、冻结窗格等结构，不能只依赖当前的 `read_only=True + iter_rows(values_only=False)` 文本拼接路径。

### 6.2 持久化模型

为减少数据库迁移，第一版复用现有 `DocumentElement`，每个非空单元格保存一个 `label="table_cell"` 元素；工作表级结构写入 `DocumentPage.metadata_json`。

单元格元素示例：

```json
{
  "label": "table_cell",
  "text_content": "1,000.00",
  "page_number": 1,
  "metadata_json": {
    "sheet_name": "汇总",
    "row": 2,
    "column": 3,
    "address": "C2",
    "raw_value": 1000,
    "display_value": "1,000.00",
    "value_type": "number",
    "formula": null,
    "cached_result": null,
    "number_format": "#,##0.00",
    "merge_range": null
  }
}
```

工作表页面元数据示例：

```json
{
  "sheet_name": "汇总",
  "max_row": 1800,
  "max_column": 36,
  "merged_ranges": ["A1:F1"],
  "hidden_rows": [8],
  "hidden_columns": [12],
  "freeze_panes": "B2",
  "structure_complete": true,
  "warnings": []
}
```

`DocumentPage.text_content` 仍保留，作为全文检索和旧功能兼容投影，但它不再是 Excel 的唯一事实来源。文本投影应带稳定的 Sheet 和单元格范围标识。

### 6.3 大文件策略

每个空单元格不单独入库，只保存非空单元格和影响结构的合并锚点。当前实现对全工作簿最多持久化
100,000 个逐格事实；超过阈值时：

- 数据库保存工作表元数据、可索引文本和必要证据单元格。
- API 分页读取区域，不一次返回整个工作簿。
- 结果明确标注 `structure_complete` 和截断原因。

压缩 JSON 全量派生件保留为后续增强，不属于当前精简实现。当前超过阈值时仍保留完整工作表文本投影，
但逐格预览和单元格证据只覆盖已持久化范围，系统必须明确提示，不能声称结构完整。

后续只有在查询量和数据规模证明必要时，才新增专用工作表/单元格表；本次不提前引入迁移。

## 7. 结构化预览 API

现有通用文本预览接口保持兼容，新增 Excel 专用只读接口：

```text
GET /api/files/{document_id}/spreadsheet-preview
    ?sheet_name=汇总
    &row_offset=0
    &row_limit=100
    &column_offset=0
    &column_limit=50
```

响应示例：

```json
{
  "document_id": "uuid",
  "filename": "统计表.xlsx",
  "sheets": [
    {
      "name": "汇总",
      "row_count": 1800,
      "column_count": 36,
      "hidden": false
    }
  ],
  "selected_sheet": {
    "name": "汇总",
    "row_offset": 0,
    "row_limit": 100,
    "column_offset": 0,
    "column_limit": 36,
    "merges": ["A1:F1"],
    "cells": []
  },
  "truncated": false,
  "warnings": []
}
```

接口必须复用现有文档访问控制，不返回本地绝对路径。上传草稿尚未形成 `document_id` 时仍使用浏览器本地解析，不为即时预览提前改变文件生命周期。

## 8. 分类、问答与证据使用

结构化预览完成后，Agent 的 Excel 事实使用规则调整为：

- 普通全文分类可以继续读取工作表文本投影，但证据必须落到 Sheet 和单元格地址。
- 日期、金额、计数、汇总和公式结果优先读取结构化单元格，不从制表符文本猜测。
- 引用至少包含 `sheet_name` 和 `cell_range`，例如“汇总!C2:C18”。
- 公式无缓存结果时，回答必须说明无法确认计算结果，不能把公式字符串当成结果。
- 确定性统计由表格分析 Tool 完成，不能让 LLM 自行计算。
- 浏览器本地预览结果不进入 `AgentGraphState`，也不作为后端回答证据。

## 9. 最小改动范围

### 第一阶段

- `apps/web/package.json`：增加 Excel 本地解析依赖。
- `apps/web/src/features/chat/`：增加 Excel 解析 Worker、结构化模型和表格组件。
- `DuplicateComparisonDialog.tsx`：Excel 分支改用结构化表格，其他格式维持现状。
- `chat.css`：补充 Sheet 标签、行列标题和表格滚动样式。
- 前端测试：覆盖结构化转换、公式状态、合并单元格和显式截断提示。

### 第二阶段

- `apps/api/app/modules/files/extractors.py`：由 TSV-only 提取升级为工作簿结构提取。
- 文件解析持久化服务：写入 `DocumentElement(table_cell)` 和工作表元数据。
- `apps/api/app/modules/files/schemas.py`：新增结构化预览响应模型。
- 文件预览路由和服务：增加分页区域查询。
- chunk/evidence 构建：保留 Sheet 和单元格范围。
- 后端测试：覆盖数值格式、公式、合并单元格、多 Sheet 和坐标证据。

## 10. 实施顺序

1. 增加 `.xlsx` 浏览器 Worker 和结构化数据模型。
2. 在对比弹窗中增加 Sheet 标签、行列标题、行列分页窗口和公式详情。
3. 删除 Excel 预览中的 200 行、50 列静默截断，改为显式资源边界。
4. 用固定样例完成前端准确性验证。
5. 改造后端 Excel 提取，持久化单元格结构和工作表元数据。
6. 增加结构化预览 API，并让已入库文件优先读取持久化事实。
7. 调整 chunk、分类和证据回答，使引用可以定位到单元格范围。

第一阶段可以独立交付，直接改善当前查重确认体验；只有完成第二阶段后，才能认为 Agent 对 Excel 的分类、问答和计算事实也得到修复。

## 11. 测试与验收

至少准备以下固定工作簿：

- 日期、百分比、货币、千分位、前导零和布尔值。
- 有缓存结果与无缓存结果的公式单元格。
- 合并单元格、空行、隐藏行列和冻结窗格。
- 多工作表以及隐藏工作表。
- 超过 200 行、50 列的工作表。
- 中文工作表名、中文文件名和特殊字符。
- 接近资源上限的大工作簿。
- `.xls`、`.xlsm` 和损坏文件的降级提示。

验收标准：

- 用户能切换所有可见工作表。
- 行号、列标和单元格地址与 Excel 原文件一致。
- 日期、百分比、货币和前导零的展示不再被普通 `str(value)` 破坏。
- 公式与缓存结果可区分；无缓存时不显示伪造结果。
- 合并单元格可识别，空行不会导致后续行号偏移。
- 超过显示/资源上限时有明确提示，不再静默丢失数据。
- 调整弹窗大小或最大化后，表格自适应可用区域。
- 原文件未被修改，宏、公式、链接和外部资源均未执行。
- 后端阶段完成后，分类和问答证据能够定位到 Sheet 与单元格范围。

## 12. 推荐结论

当前应先实现第一阶段：只在查重对比弹窗中为 `.xlsx` 增加浏览器本地结构化预览，不改其他文件类型，也不先引入数据库迁移。这样能以最小范围修复用户直接看到的“不准确”问题。

随后再完成后端结构化持久化。若只做前端，预览会更准确，但 Agent 的分类、检索和问答仍会继续使用现有的 TSV 文本事实，不能视为 Excel 提取能力已经完整修复。
