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

## 当前附件限制

此标准连接器能够导入“已授权目录”中的文件，但**不能自动导入聊天框上传的附件**。原因是 WorkBuddy 的公开连接器 MCP 契约没有把当前消息附件的稳定提交清单传给 MCP。连接器特意隐藏了需要该清单的两个工具，避免模型捏造路径或附件 ID。

若 WorkBuddy 后续发布官方附件提交 API 或 Hook 连接器契约，再把现有 `workbuddy_submission_ingest` 重新启用；后端可信提交单校验代码已保留。
