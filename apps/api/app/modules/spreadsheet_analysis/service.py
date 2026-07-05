"""
spreadsheet_analysis 模块的服务入口（Service）。

职责：
- 对 Agent Runtime 暴露统一的电子表格分析接口
- 编排 profiler → query_planner → validator → executor → formatter 全链路
- 处理错误、降级和日志记录

安全边界：
- 不绕过 Tool 白名单直接暴露给 LLM
- 所有文件路径必须来自 StorageService，不接受用户直接传入路径
"""