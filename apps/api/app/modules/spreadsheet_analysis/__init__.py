"""
spreadsheet_analysis 模块：电子表格智能分析。

在 File Agent 架构中，本模块负责对上传的电子表格文件（XLSX、CSV 等）
进行结构化分析，包括：
- 表格结构探查（sheet、列、数据类型、统计摘要）
- 自然语言查询规划与执行
- 查询结果校验与格式化

本模块是 Agent Tool `table-extract` 和 `evidence-answer` 的下游能力，
不直接暴露为 HTTP API，而是通过 service.py 对 Agent Runtime 暴露调用接口。
"""