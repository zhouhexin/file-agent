# WorkBuddy 文件分类浏览集成实现说明

## 1. 目标

在不复制分类页面、不改变分类算法和落位规则的前提下，让 WorkBuddy 能够：

1. 查看当前活动文件、具体业务分类和“其他”的汇总数量。
2. 查看一级分类及文件数量。
3. 按稳定 `category_id` 分页查看该节点及其全部子分类下的文件。
4. 在结果表格中直接预览或下载文件。
5. 跳转到 File Agent 原有 `/files` 页面浏览完整分类树。
6. 未登录时完成登录后返回原分类节点和页码。

本功能使用现有数据库分类关系和现有分类查询服务作为唯一事实源，不在 MCP、WorkBuddy 套件或前端维护第二份分类树。

## 2. 边界

### 2.1 本次实现

- 后端分类文件分页响应增加永久只读能力链接 `preview_url`、`download_url`。
- MCP 增加 `classification_overview`、`classification_files` 两个只读 Tool。
- MCP 返回面向用户的固定 Markdown 展示文本，并把稳定对象 ID 放在结构化上下文中。
- Web 分类页支持 `/files?category_id=<稳定ID>&page=<页码>`。
- 登录门禁保存受控 `/files` 返回目标。

### 2.2 明确不做

- 不改分类候选、主类选择、材料包继承、部门兜底和物理落位规则。
- 不在 WorkBuddy 对话中展开完整深层 taxonomy。
- 不新增分类写接口，也不放宽已有 `SET_PRIMARY`、`MOVE` 校验。
- 不修改或升级 WorkBuddy 套件压缩包和 Skill。
- 不公开服务器文件系统路径。

## 3. 调用流程

```text
WorkBuddy
  -> classification_overview
  -> GET /api/classification/organization/tree
  -> 返回统计、一级分类入口、完整 /files 页面链接

WorkBuddy
  -> classification_files(category_id, page, page_size)
  -> GET /api/classification/organization/files
  -> 返回文件名、主分类、状态、预览/下载链接

浏览器打开 /files?category_id=...&page=...
  -> 未登录：显示登录页并保存受控返回目标
  -> 登录成功：恢复相同 category_id 和 page
  -> 前端继续调用现有分类树与分页接口
```

## 4. 后端契约

`GET /api/classification/organization/files` 的每个文件项新增：

```json
{
  "preview_url": "/api/public/file-access/<capability-token>/preview",
  "download_url": "/api/public/file-access/<capability-token>/download"
}
```

链接由 `public_file_links(working_copy_id)` 生成。公开访问服务每次打开时仍重新校验：

- 工作副本属于共享工作区。
- 工作副本状态仍为 `ACTIVE`。
- 当前版本和物理内容仍存在。

因此分类接口不返回绝对存储路径，也不会绕过工作副本活动状态。

## 5. MCP Tool 契约

### 5.1 `classification_overview`

输入：无。

副作用：无。

用户展示：

- 活动文件总数。
- 具体业务分类数量。
- “其他”数量。
- 一级分类、子树文件数及 Web 页面入口。

结构化结果的 `category_options` 保留全部受控节点的稳定 `category_id`、完整显示路径和计数，供下一次
`classification_files` 调用使用；用户可见表格只展示一级分类，避免在对话中展开整棵深层 taxonomy。

### 5.2 `classification_files`

输入：

```json
{
  "category_id": "school.hr",
  "page": 1,
  "page_size": 20
}
```

- `category_id` 可省略；省略时查看全部活动文件。
- `page >= 1`。
- `1 <= page_size <= 100`。
- 查询范围固定为 `descendants`。

副作用：无。

用户展示列固定为：

| 文件名 | 主分类 | 状态 | 操作 |
|---|---|---|---|
| 示例.docx | 学校 / 人事师资 / 人才工作 | 自动归类 | 预览 / 下载 |

`working_copy_id`、`document_id`、`document_version_id` 仅进入 `structuredContent.files[].tool_context`，不得作为用户表格列展示。

## 6. Web 深链接规则

分类页只解析以下查询参数：

- `category_id`：最多 200 字符的稳定分类 ID。
- `page`：正整数，非法值回退到 1。

选择分类节点、选择全部文件或翻页时，页面使用 `history.replaceState` 同步当前 URL。分类显示名称仍从服务端 taxonomy 树查找，不能从 URL 接受显示名称。

登录返回只允许保存同源 `/files` 及其查询字符串，不接受任意 `return_to` URL，避免开放重定向。

## 7. 开发文件清单

- `apps/api/app/modules/classification/organization_schemas.py`
- `apps/api/app/modules/classification/organization_query_service.py`
- `apps/mcp/file_agent_mcp/client.py`
- `apps/mcp/file_agent_mcp/conversation_tools.py`
- `apps/mcp/file_agent_mcp/server.py`
- `apps/web/src/App.tsx`
- `apps/web/src/features/files/ClassificationFilesPage.tsx`
- `apps/web/src/features/files/classificationDeepLink.ts`
- 对应 API、MCP、Web 测试。

## 8. 验收标准

1. `classification_overview` 的计数与 `/files` 页面一致。
2. 一级分类链接能打开对应分类节点。
3. `classification_files` 翻页总数与后端分类分页接口一致。
4. 表格中的预览和下载链接在未登录浏览器中也可打开；工作副本被回收后链接拒绝访问。
5. 直接访问 `/files?category_id=...&page=2` 时能恢复节点和页码。
6. 未登录访问上述地址，登录后仍回到同一地址。
7. 两个 MCP Tool 不创建分类关系、落位任务、OperationPlan 或 ChangeSet。
8. 原有 `file_search`、分类页面和分类写操作回归测试通过。

## 9. 部署影响

本功能同时修改 API、MCP 和 Web 三层代码：

- 服务端需要重建 API 代码镜像和 Web 镜像。
- 用户端需要重新安装包含新 MCP Python 包的环境，或升级本地安装包。
- MCP 配置应增加 `FILE_AGENT_WEB_BASE_URL`，例如 `http://10.102.4.241`；它用于生成原 Web 分类页链接。
  `FILE_AGENT_API_BASE_URL` 仍可保持为 API 直连地址，例如 `http://10.102.4.241:8000`。
- 不需要升级 WorkBuddy 套件版本；套件仅在需要自然语言固定路由提示时才是可选增强。
- 数据库没有新增字段或表，不需要 Alembic 迁移。
