# 累计表格导出的直接预览与下载链接

## 目标

累计表格成功发布后，WorkBuddy 回执同时提供浏览器可直接打开的预览和下载地址。用户无需手动添加 Bearer token。

## 工作方式

1. MCP 使用现有 Bearer token 调用已认证的 `/{dataset_id}/exports/{export_id}/browser-links`，请求服务端为指定导出签发链接。
2. 链接使用独立签名用途，只绑定一个 `dataset_id + export_id`，有效 24 小时；它不是登录 token，也不能访问其他文件或执行写入。持有链接的人可在有效期内读取该导出，因此回执需提示用户不要公开转发。
3. `/public-exports/{token}/preview` 检查导出快照仍存在且哈希匹配后，读取 XLSX 并生成只读网页预览；`/download` 返回原 XLSX 下载。
4. MCP 将相对地址补成 `FILE_AGENT_API_BASE_URL` 对应的绝对地址，并作为 `preview_url` 和 `download_url` 返回。签名 token 只在响应阶段生成，不写入发布记录、ToolInvocation 或 ChangeSet；API 应用访问日志及 Uvicorn 控制台访问日志会对公开文件链接中的 token 做脱敏。
5. 原 `/exports/{export_id}/content` 路径仍保留 Bearer 认证，供已认证客户端使用。

## 限制与部署

- 预览限制为最多 20 张工作表、每张最多 300 行和 40 列，总计最多 12,000 个单元格；超出范围时提示下载原文件查看完整内容。
- 链接可被持有者访问，回执必须把 24 小时有效期告诉用户；过期后重新导出即可签发新链接。
- 不需要数据库迁移。需要部署更新后的 API 和 MCP，并更新 WorkBuddy Skill/插件提示规则；服务端启用累计表格 Demo 的现有配置必须保持开启。
