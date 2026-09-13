# WorkBuddy File Agent 连接器交付开发说明

> 2026-09-13 中文称呼补充：WorkBuddy 面向用户统一显示和提示“文件助手”，中文示例使用
> “请用文件助手……”；底层 `file-agent` MCP server key、工具名、环境变量和英文内部标识保持不变。
> 连接器元数据版本递增为 1.0.1，避免已安装客户端继续缓存旧中文名称与 Skill。

## 1. 目标与范围

将原先仅适用于 CodeBuddy Code CLI 的附件桥接交付目录，补充为 WorkBuddy 可识别的 MCP + Skill 连接器包。连接器必须能够通过 WorkBuddy 的插件/连接器安装流程，配置本机 stdio MCP，并让 AI 基于自然语言选择现有 File Agent 工具。

本次不改变 File Agent API、MCP 的手工目录导入、检索、分类、查重或受控文件操作行为。

## 2. 技术选择

采用 WorkBuddy 官方推荐的 **MCP + Skill** 连接器，而不是 CLI 连接器：File Agent 已有稳定 MCP Server。连接器为 Windows 本机 stdio 场景启动 `file_agent_mcp.server`，并以 `auth_mode: token` 的本机表单采集 API Token 和本机启动配置。

连接器根目录固定为：

```text
integrations/workbuddy/file-agent-connector/
├─ connector-meta.json
├─ mcp.json
├─ token-schema.json
├─ icon.svg
├─ skills/file-agent/SKILL.md
└─ README.md
```

`connector-meta.json` 使用全局稳定 `source=file-agent-local`，声明 `type=mcp`、`auth_mode=token`、`minWorkbuddyVersion=4.24.0`。`mcp.json` 只能配置一个 MCP Server；真实 Token 和本机路径只能使用 `${VAR}` 占位符，禁止写入包文件。

## 3. 配置与安全边界

连接表单收集：Python 可执行路径、`apps/mcp` 源码目录、File Agent API 地址、Access Token、受管目录 JSON 与传输状态目录。Token 字段必须是 `password` 类型。

Windows 通过 `cmd.exe` 启动 `%FILE_AGENT_PYTHON_EXECUTABLE% -m file_agent_mcp.server`，并把 `FILE_AGENT_MCP_SOURCE_DIR` 注入 `PYTHONPATH`。MCP 保留既有 `FILE_AGENT_LOCAL_ROOTS` 白名单校验，连接器不增加任意路径读取能力。

## 4. 自然语言路由

Skill 覆盖：受管目录批量导入/归档/分类、已入库文件搜索、读取、证据回答、重复文件复核、明确的受控重命名和分类落位。Skill 仅解释现有 Tool 的正确顺序，不生成路径、文件 ID、证据、重命名参数或重复文件决定。

## 5. 聊天附件的明确限制

WorkBuddy 的公开连接器 MCP 契约未提供本轮聊天附件的稳定提交 ID、原始文件名和受控本地缓存引用，也未在连接器规范中定义 Hook 的附件提交事件。因此标准连接器无法安全调用 `workbuddy_attachment_ingest` 或 `workbuddy_submission_ingest`。

这两个工具必须在连接器中通过 `disabledTools` 隐藏。不得将用户上传附件猜测为受管目录文件，也不得要求用户手写 `local_path` 或 `attachment_id`。已有可信提交单后端实现保留，等待 WorkBuddy 发布官方附件 API / Hook 连接器契约后再接入。

## 6. 验收

1. 所有 JSON 均可解析，目录文件齐全。
2. `connector-meta.json` 包含必填中英文名称、描述、示例、稳定 source 和版本。
3. `mcp.json` 仅含一个 MCP Server，未硬编码 Token 或绝对机器路径。
4. `token-schema.json` 的所有 `${VAR}` 有对应字段，Token 为 password。
5. Skill 清楚写出自然语言能力边界，附件桥接工具不对 AI 暴露。
6. `apps/mcp/tests` 回归通过。
