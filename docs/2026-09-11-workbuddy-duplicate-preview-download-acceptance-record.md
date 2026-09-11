# WorkBuddy 重复文件预览与下载验收记录

日期：2026-09-11
状态：实现完成，待本机与局域网实机验收。

## 已实现范围

- 新增固定重复候选的只读 comparison、content、preview 三个接口，以及 `duplicate_comparison_get` MCP 工具。
- 浏览器页 `/duplicate-comparison` 复用既有双栏安全预览，支持图片、PDF、文本、DOCX、XLSX 和已有正文区段；页面没有重复决定按钮。
- comparison/content/preview 均校验当前用户、review、候选、修订、可选重复组修订和 `snapshot_id`；拒绝未知参数、路径、自由 URL 与自由 Document ID。
- 下载采用浏览器 JWT 的有界响应流。原始图片/PDF/文本预览最多 25 MiB；DOCX 为 20 MiB；XLSX 为既有 25 MiB；每次下载最多 128 MiB，页面同时只允许一个下载任务。
- 未配置 `INTEGRATION_REVIEW_WEB_BASE_URL` 时旧查重和决定链路保持可用，链接为 null 并返回 `REVIEW_WEB_URL_NOT_CONFIGURED`。

## 自动化验证

在项目根目录的 PowerShell 执行，结果如下：

```powershell
$env:PYTHONPATH='apps/api'
& 'D:\anaconda\envs\myenv\python.exe' -m pytest apps/api/app/tests/test_integration_ingest_api.py::test_duplicate_comparison_reads_only_the_fixed_candidate_snapshot -q
# 1 passed

$env:PYTHONPATH='apps/mcp;apps/api'
& 'D:\anaconda\envs\myenv\python.exe' -m pytest apps/mcp/tests/test_server.py apps/mcp/tests/test_client.py -q
# 9 passed, 1 skipped

Set-Location apps/web
npm run build
# TypeScript 检查和 Vite 生产构建通过
```

新增 API 用例覆盖固定快照、精确内容结论、未知参数拒绝、无已有正文时 preview 的 409 降级、preview 不创建新任务，以及受控内容流响应。

## T01–T30 状态

| 范围 | 状态 | 说明 |
|---|---|---|
| T07、T09、T10、T14、T23 | 已自动验证（部分） | 覆盖精确结论、只读 preview、非法参数、MCP 注册与错误边界。 |
| T01–T06、T08、T11–T13、T15 | 待补充自动化/实机 | 需补齐四类来源、文件传输中变更、过期和跨用户等完整矩阵。 |
| T16–T22 | 待浏览器实测 | 已完成构建；仍需人工验证登录返回、格式矩阵、取消、网络中断和旧上传弹窗回归。 |
| T24–T26 | 待 WorkBuddy 实测 | 需在实际客户端重连 MCP 后确认工具发现、链接和旧工具兼容。 |
| T27–T30 | 待本机与 LAN 实测 | 尚未启动真实 WorkBuddy 或另一台局域网设备，不能标记通过。 |

## 实机验收前置条件

1. 部署 API、Web 和每台 WorkBuddy 本地 MCP 包的同一版本；仅更新服务器不能刷新客户端工具列表。
2. 设置 `INTEGRATION_REVIEW_WEB_BASE_URL` 为浏览器可达的 Web 地址。本机可用 `http://127.0.0.1:5173`；局域网测试必须使用服务器的局域网 IP，不能使用其他电脑的 `127.0.0.1`。
3. 重启 API 和 Web；重连或重启 WorkBuddy。原有 `mcp.json` 的 command、args 和 API 地址不变时无需修改。
4. 使用全新的测试文件，依次验证两侧预览、两侧下载、返回 WorkBuddy 后的原 `duplicate_decide` 决定，以及过期/超限候选的受控失败提示。

不记录样本文字、真实路径、令牌、SHA-256 或其他敏感内容。
