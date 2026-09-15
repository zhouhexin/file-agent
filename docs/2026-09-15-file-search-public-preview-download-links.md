# file_search 直接预览/下载链接开发说明

## 1. 目标

WorkBuddy 调用 `file_search` 后，用户可见结果固定为三列：文件名、命中依据、操作。已经形成活动工作副本的
文件在操作列直接提供可点击的“预览”和“下载”；不展示文件类型、目录、分类路径、相关度或内部稳定 ID。

链接满足本次明确需求：浏览器无需登录，且不设置时间到期。该能力只改变只读访问和结果展示，不改变
`file_read`、`evidence_answer`、分类落位、重命名、OperationPlan 或工作副本生成规则。

## 2. 调用链

```text
WorkBuddy file_search
-> MCP 使用 Bearer token 调用 POST /api/search
-> 后端仅给 ACTIVE 且可打开的工作副本签发只读能力令牌
-> API 返回相对 preview_url / download_url
-> MCP 以 FILE_AGENT_API_BASE_URL 转换为绝对 URL
-> MCP 输出“文件名 / 依据 / 操作”Markdown 表格
-> 用户点击链接，浏览器匿名调用公开只读 GET 接口
```

受管源已经命中、但工作副本还在物化时，不签发链接，操作列显示“工作副本生成中”。再次搜索且工作副本
就绪后，才返回链接。

## 3. 接口与令牌

认证搜索接口响应中的单文件字段新增：

```json
{
  "preview_url": "/api/public/file-access/{signed_token}/preview",
  "download_url": "/api/public/file-access/{signed_token}/download"
}
```

新增匿名只读接口：

```text
GET /api/public/file-access/{signed_token}/preview
GET /api/public/file-access/{signed_token}/download
```

能力令牌使用 `JWT_SECRET_KEY` 做 HMAC-SHA256 签名，payload 只含独立 audience 和
`working_copy_id`；不含用户身份、文件路径、正文或 `exp`。它不能作为登录 JWT 使用。服务端每次访问仍会
检查：签名有效、工作副本属于唯一共享工作区、状态为 `ACTIVE`、当前 DocumentVersion 存在、工作副本物理
文件存在。令牌篡改返回 404，文件回收后返回 410。

按需求，令牌没有时间到期，因此拿到链接且网络可达的人都能读取对应文件。`JWT_SECRET_KEY` 轮换会使旧链接
失效；该机制当前没有单链接撤销表，也不需要数据库迁移。

## 4. 预览规则

- PDF、图片、TXT、Markdown、CSV：以 `Content-Disposition: inline` 返回当前工作副本文件。
- Word 及其他已解析文档：读取当前版本最近一次成功的 `document_pages`，HTML 转义后按页展示，最多展示
  200,000 个字符。
- Excel：读取当前版本最近一次成功解析中的 `table_cell`，按 Sheet 生成 HTML 表格；最多 10 个 Sheet、
  1,000 个单元格、每个 Sheet 的前 100 行和 30 列。
- 当前版本没有持久化解析结果或格式不支持：返回可打开的说明页，并保留“下载原文件”链接；不得在浏览器
  请求中同步触发解析。

所有预览响应设置 `Cache-Control: no-store`。HTML 正文、文件名、Sheet 名和单元格内容必须转义；页面 CSP
禁止脚本和外部资源。

## 5. MCP 展示规则

MCP 只接受后端返回的绝对 `http` 或 `https` 链接进入 Markdown，拒绝 `javascript:`、`file:` 和含控制
字符的地址。示例：

```markdown
| 文件名 | 依据 | 操作 |
|---|---|---|
| 学校推荐意见.docx | 正文主题命中：人才推荐 | [预览](http://server/api/...) / [下载](http://server/api/...) |
```

`tool_context` 继续保留给模型后续调用 `file_read`、`file_download` 或受控动作，但不进入用户可见表格。
既有 `file_download` MCP Tool 不删除，作为需要本机 `ResourceLink` 场景的兼容入口。

## 6. 部署与验证

部署时无需 Alembic 迁移，无需 Web/Caddy 新页面，也不需要 `python -m http.server`。必须同时更新 API 代码和
客户端 MCP 代码，并确保客户端配置的 `FILE_AGENT_API_BASE_URL` 是浏览器可达的局域网地址。

建议验证：

1. 登录 API 后通过 WorkBuddy 调用 `file_search`，确认只显示三列表格。
2. 在未登录 File Agent 的浏览器中分别点击“预览”和“下载”。
3. 修改链接令牌任意一字符，确认返回 404。
4. 将目标工作副本移入回收站，确认旧链接返回 410。
5. 搜索仅存在受管源、工作副本尚未物化的文件，确认显示“工作副本生成中”，而不是无效链接。

自动化回归命令：

```powershell
$env:PYTHONPATH='apps/api;apps/mcp'
& 'D:\anaconda\envs\myenv\python.exe' -m pytest apps/api/app/tests/test_file_search_api.py apps/mcp/tests/test_conversation_tools.py apps/mcp/tests/test_client.py apps/mcp/tests/test_server.py -q
```
