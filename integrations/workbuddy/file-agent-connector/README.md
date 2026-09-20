# 文件助手 WorkBuddy 连接器

这是 WorkBuddy 官方 MCP + Skill 连接器结构，可用于安装后以自然语言调用文件助手的受管目录导入、检索、读取、归档、分类、查重和受控文件操作。用户可说“请用文件助手查找奖学金材料”，无需输入英文产品名；底层 `file-agent` MCP 标识和配置键不变。

目录内容符合 WorkBuddy 连接器要求：`connector-meta.json`、`mcp.json`、`token-schema.json`、`icon.svg` 和 `skills/`。

## 安装前提

1. 文件助手 API 已启动。
2. 本机具备可运行 `file_agent_mcp` 的 Python 环境。
3. 你拥有文件助手访问令牌。

## 安装

在 WorkBuddy 左侧打开“插件”，通过插件市场添加并安装此连接器包。连接时按表单填写 Python 路径、`apps/mcp` 源码目录、API 地址、Token、授权根 JSON 和传输状态目录。不要把 Token 写入本目录的任何 JSON 文件。

Windows 示例：

- Python：`D:\anaconda\envs\myenv\python.exe`
- MCP 源码目录：`E:\PycharmProject\file-agent\apps\mcp`
- API：`http://127.0.0.1:8000`

## 当前附件边界

连接器保留 `workbuddy_attachment_ingest` 和 `workbuddy_submission_ingest`，用于配合同套件中的可信附件 Hook。标准连接器本身不会生成附件提交单；只有本轮上下文已经包含 `FILE_AGENT_HOST_SUBMISSION`，或宿主直接提供稳定提交 ID、附件 ID、原文件名和位于预配置缓存根内的真实文件引用时，才能调用对应附件工具。

缺少这些宿主事实时，不得猜测本地路径、附件 ID 或提交 ID。用户明确指定已授权目录时，继续使用 `file_batch_ingest`。
