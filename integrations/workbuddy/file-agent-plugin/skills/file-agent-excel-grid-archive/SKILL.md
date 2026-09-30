---
name: file-agent-excel-grid-archive
description: 将 WorkBuddy 已授权电子表格读取工具返回的真实逐格网格，转换为 File Agent 的 SHEET 提取单元和 TABLE_CELL 坐标证据。用于上传目标 Excel 归档或复杂模板冻结前；不读取 PDF，也不写 Excel。
---

# 目标 Excel 网格归档

在 `document_extraction_claim` 返回的固定源版本上，使用 WorkBuddy 已安装的电子表格读取工具取得真实工作表、
合并区域、已用范围及逐格值。`.xls` 可以使用能返回真实网格的本地 Office/editor_sdk 能力；先核对该工具当前
schema。只读取 claim 提供的受控本地资源，不根据聊天文字、文件名或模型推断单元格坐标。工具无法返回真实
0-based 行列时停止，不用 OCR、普通正文或自编 XLS 解析器补造 `TABLE_CELL`。

## 转换与提交

对要作为写入目标、且有真实表头的工作表保存读取工具的原始
`{"cells":[{"row":0,"col":0,"string_value":"..."}]}` 网格 JSON；`row`、`col` 必须是读取工具给出的
0-based 坐标。先根据真实网格与合并区域确定该表的表头范围，
并让它与后续 checkout 使用的 `layout.header_range` 一致。表头范围不能只凭参考文件名确定。

运行本 Skill 的 `scripts/grid_to_payload.py`，每个工作表使用不同的 `--order` 和输出前缀：

```text
python <本 Skill 目录>/scripts/grid_to_payload.py --grid <真实网格.json> --sheet <真实工作表名> --header <真实表头范围> --order 0 --out <输出前缀>
```

仅在明确只填物理 A–I 的任务中再加 `--require-a-i`。其他 Excel 任务不得套用 A–I 列限制。已用范围包含
末尾空行或空列时，用 `--rows`、`--cols` 指定读取工具报告的实际网格尺寸；尺寸小于任何已读取单元格时
脚本会拒绝，不会丢格。

退出码为 0 时，`<前缀>_unit.json` 是可直接放入 `document_extraction_submit.units` 的一个 `SHEET`
单元；另有 `<前缀>_unit_text.txt` 与 `<前缀>_blocks.json` 供核对。脚本保留单元格中的真实换行和制表符，
因此不能通过 `unit.text.splitlines()` 推断 Excel 行号；只使用 `TABLE_CELL.row_index/column_index`
和原网格确定物理位置。表头每个非空格都有一个块，`block.text` 与 `unit.text[start:end]` 完全相同；
`sheet_name` 在 unit 上，不放进 block。脚本遇到重复坐标、表头越界、超出服务端大小上限或块不一致时
退出码非 0，**不得提交**。

所有真实工作表都必须形成对应的 unit；没有表格表头的说明页可用真实正文形成 `READ` 单元，不补造
`TABLE_CELL`；空工作表按服务端契约提交 `BLANK` 和真实原因，不把它伪装成 `READ`。
`coverage.unit_count` 与工作表总数一致，只有实际完整读取后才报告 `COMPLETE`。提交之后核对
提取回执及归档状态，再继续调用方的目标冻结或写入流程。已经完成的提取 run 不能用新载荷覆盖；若缺少
表头块，停止并按受控重处理流程重新读取，不对同一完成任务反复 submit。
