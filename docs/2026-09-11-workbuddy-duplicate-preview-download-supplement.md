# WorkBuddy 重复文件预览与下载补充方案

- 日期：2026-09-11。
- 状态：方案，尚未实施。
- 范围：已经进入 File Agent 导入链路的重复文件，在用户决定前查看两侧内容、分别下载。
- 对应主方案：[WorkBuddy 上传优先方案](./2026-09-08-workbuddy-upload-first-implementation-plan.md)。
- 复用基础：[重复上传对比预览方案](./duplicate-upload-comparison-preview-implementation-plan.md)第 0 节已经实施的精简能力。该文档后续规划不能直接视为已实现。
- 直接开发依据：[重复文件预览与下载开发规范](./2026-09-11-workbuddy-duplicate-preview-download-development-spec.md)。实施须遵守该规范的接口、改动范围、资源预算和逐项验收要求。

## 1. 当前代码与具体缺口

核查基于本仓库 2026-09-11 工作树，不依赖 WorkBuddy 未验证的宿主接口。

| 位置 | 当前实现 | 本次补齐内容 |
|---|---|---|
| `apps/api/app/modules/ingestion/schemas.py` | 重复候选返回 candidate ID、类型、摘要和可选 existing_document_id | 增加可发现的对比入口与能力状态 |
| `apps/api/app/modules/ingestion/duplicate_service.py` | 绑定条目、review 修订、候选和重复组进行决定；已有工作副本版本校验 | 提供统一的两侧对象解析及只读对比投影 |
| `apps/mcp/file_agent_mcp/server.py`、`client.py` | 已有 duplicate_review_get、duplicate_decide | 转接对比查询并向聊天返回浏览器入口 |
| `apps/web/src/features/chat/DuplicateComparisonDialog.tsx` | 网页上传已有双栏预览；图片、PDF、文本、DOCX、XLSX 等使用现有渲染能力 | 复用预览呈现，增加 WorkBuddy 专用只读页面与两侧下载按钮 |
| `apps/api/app/modules/files/router.py`、`service.py` | 已有鉴权文件流、已有正文预览和表格预览 | 在重复确认上下文中按固定版本复用底层能力 |
| `apps/web/src/App.tsx` | 固定路径路由；登录或引导后进入聊天 | 新增对比深链接及登录后返回原对比页 |

不能只把 existing_document_id 拼成下载地址：上传侧可能尚未正式入库；同批候选可能只有 candidate_ingest_item_id；未物化受管文件没有工作副本；浏览器也不会自动携带 MCP 的 Authorization 令牌。

现有网页弹窗使用旧上传 review 结构和旧决策回调，不能直接把 WorkBuddy 的 review 强制转换后传进去。另有类型差异：同批候选使用 EXACT_HASH，旧弹窗确定性标签识别 EXACT_SHA256，必须由新投影规范化。

## 2. 本次用户体验

推荐以“WorkBuddy 聊天入口 + File Agent 浏览器对比页”实现，不要求安装新的 WorkBuddy 插件。

1. 用户上传附件或通过已授权目录导入，后端发现重复候选。
2. WorkBuddy 展示候选名称、重复原因和“查看对比、预览或下载”链接。
3. 点击链接进入 File Agent 页面；未登录时用与 MCP 相同的 File Agent 账号登录，随后返回原对比页。
4. 页面左侧为“本次上传”，右侧为“已有文件”或“本批另一份文件”；多候选可切换，每侧独立提供预览和下载。
5. 用户查看后回 WorkBuddy 说“用已有文件”“两份都保留”或“取消这份上传”。WorkBuddy 重新读取候选状态，在确认目标仍与用户所见一致后走原 duplicate_decide。

第一版浏览器页只提供查看、下载和刷新，不增加第二套决定按钮；页上说明“查看后回到 WorkBuddy 选择”。原网页上传弹窗继续保留自己的原有决定按钮。

WorkBuddy 内嵌双栏卡片、自动弹窗和直接保存到指定本地目录不属于本次承诺。交付时实测聊天链接可点击；若宿主不支持直接打开，用户复制页面地址到浏览器仍可完成流程。MCP 输出 URL 本身不代表宿主会渲染预览组件。

## 3. 功能边界

本次覆盖两类已接入入口：file_batch_ingest / file_ingest 本地文件导入，以及宿主已经成功调用 workbuddy_attachment_ingest 的会话附件。未进入 File Agent 的 WorkBuddy 附件不在本方案内，本次不补附件桥接插件。

必须支持：

- 本次上传与共享活动工作副本的对比。
- 同一用户、同一批次中另一条目尚未完成导入时的对比。
- 当前候选指向尚未物化受管文件时的受控只读查看。
- 每侧分别下载所展示版本对应的文件字节；下载名称包含“本次上传/候选”标识，避免用户本地混淆。
- 预览不支持时明确降级为下载；一侧失败不影响另一侧展示。
- 刷新、重新登录、MCP 重连后从后端恢复入口和状态。

不包含：分类规则、查重算法或阈值变更，全文差异高亮、自动判定保留哪份、Office 编辑、批量 ZIP 导出、全局文件下载工具、外部模型内容分析、新 OCR 或 Office 转换 worker。

## 4. 后端只读契约

### 4.1 新增接口

下列均为拟新增路由，沿用 integrations 的登录依赖和 INTEGRATION_INGEST_ENABLED 边界：

```text
GET /api/integrations/v1/ingest-items/{item_id}/duplicate-comparison
GET /api/integrations/v1/ingest-items/{item_id}/duplicate-comparison/content
GET /api/integrations/v1/ingest-items/{item_id}/duplicate-comparison/preview
```

comparison 查询必须携带 review_id、review_revision、candidate_id；有重复组时同时携带 group_revision。服务端解析当前所属关系并重验修订，不接受文件路径、任意 URL 或任意 document_id 作为目标。

content / preview 查询除上述字段外增加：

- side：仅 UPLOAD 或 CANDIDATE。
- snapshot_id：comparison 返回的两侧身份与版本指纹。
- content 的 disposition：仅 inline 或 attachment。
- preview 的受控分页参数，首版限制正文最多 100,000 字符；表格复用现有有界分页预算，不接收执行代码。

snapshot_id 是服务端从 review/组修订、两侧版本、工作副本 revision 或受管源修订计算的稳定指纹；它是并发条件，不是访问凭证。每次请求重新授权和重算，不靠客户端提供的版本或指纹授予访问权。

响应示意（省略 UUID 的具体取值）：

```json
{
  "item_id": "item-id",
  "review_id": "review-id",
  "review_revision": 2,
  "group_revision": null,
  "candidate_id": "candidate-id",
  "snapshot_id": "opaque-fingerprint",
  "status": "READY",
  "verdict": "EXACT_CONTENT",
  "comparison_url": "https://file-agent.example/duplicate-comparison?item_id=item-id&candidate_id=candidate-id&review_id=review-id&review_revision=2",
  "upload": {
    "filename": "材料.docx",
    "source_kind": "UPLOAD",
    "size_bytes": 12345,
    "preview_status": "AVAILABLE",
    "preview_mode": "DOCX",
    "download_available": true,
    "reason_code": null
  },
  "candidate": {
    "filename": "已有材料.docx",
    "source_kind": "WORKING_COPY",
    "size_bytes": 12345,
    "preview_status": "AVAILABLE",
    "preview_mode": "DOCX",
    "download_available": true,
    "reason_code": null
  }
}
```

两侧各自保留状态；总体 READY 表示可查看，PARTIAL 表示仅部分内容可用，UNAVAILABLE 表示均不可用。状态不是导入任务或分类状态，不推进原业务状态机。AVAILABLE 只表示存在可用资源，客户端渲染仍可能失败，失败后展示下载降级。

### 4.2 对象绑定与权限

新增独立 IngestDuplicateComparisonService；复用认证、存储解析和现有预览底层，避免通过通用 content(document_id) 取得“当前任意版本”。

| 侧别/来源 | 解析依据 | 必须校验 |
|---|---|---|
| 本次上传 | item.upload_document_version_id | batch 和 review 属于当前用户、版本归属匹配、暂存仍存在且允许读取 |
| 活动工作副本 | candidate_working_copy_id、compared_version_id、compared_working_copy_revision | 当前权限、ACTIVE、版本和 revision 与候选一致 |
| 同批未完成文件 | candidate_ingest_item_id、compared_version_id | 同用户同批、真实重复组成员、固定上传版本仍可读；不得跨用户读取暂存 |
| 未物化受管文件 | candidate_managed_file_id、源修订和内容指纹 | 当前授权根、当前文件、候选身份和字节未变；不因查看启动物化 |

compared_* 已存在，优先复用，不重复新增一套快照表。未物化来源缺少足够修订信息时，可在创建候选时补充 match_evidence_json 的版本化快照；旧候选缺少快照时显示需要重新检查，不能自动把当前内容冒认为原候选。

文件流必须绑定经过核验的真实存储对象，重验已有大小/指纹及存储完整性条件；源文件校验到传输期间发生变化时终止或返回变化错误，不能只验证数据库字段后重新按名称定位。已经失效的文件路径不从本机源目录或另一同名文件补位。

不返回服务器绝对路径、客户端缓存路径、原始 SHA-256、其他用户身份或来源会话。浏览器获得的内容只供用户查看，不经 MCP 返回给模型。未授权对象统一 404；过期或失效资源返回结构化错误。

### 4.3 状态变化与清理

- 版本、工作副本 revision、重复组成员或源字节变化：409，DUPLICATE_CANDIDATE_CHANGED / DUPLICATE_REVIEW_REVISION_CONFLICT / DUPLICATE_GROUP_REVISION_CONFLICT，停止继续读取旧链接。
- review 已解决或过期：410，显示对应状态；不延长暂存生命周期。
- 文件已清理：410，COMPARISON_CONTENT_GONE；仍保留原审计，不重新下载或导入源文件。
- 等待同批主条目时，主条目发布或取消造成原暂存不可读：重新取 comparison；不能静默切到另一个新版本。若需刷新重复候选，必须调用已有合法重验流程；现有读取接口不能重建候选时显示明确限制，不能无限提示刷新即可修复。
- 同批其他文件继续处理；查看、下载失败不能使整个批次失败。

本次不生成服务端预览派生件，不新增后台任务和清理规则。纯只读访问使用现有请求日志/工具记录能力记录用户、目标 ID、结果、耗时，不创建虚假的 ChangeSet 或 OperationPlan。

## 5. 预览和下载实现

| 格式 | 本次预览方式 | 降级 |
|---|---|---|
| PNG/JPEG 等受支持图片 | 复用图片展示；仅允许安全 MIME | 无法展示则下载 |
| PDF | 复用受控 Blob / PDF 查看器；可打开单侧查看 | 浏览器不支持或超过预览预算则下载 |
| TXT/MD/CSV/TSV | 转义文本展示；长度截断必须提示 | 下载完整字节；Markdown/HTML 不作为可执行页面 |
| DOCX | 复用现有沙箱 docx-preview，当前本地上限 20 MB | 超限/失败时读取固定版本已有正文，否则下载 |
| XLSX | 复用现有结构化工作簿组件，当前本地上限 25 MB | 超限/失败时读取固定版本已有表格/正文，否则下载 |
| DOC/XLS/其他已允许上传格式 | 固定版本已有正文或已有安全派生件 | 没有预览结果则下载，不为本功能启动转换 |
| 加密、宏或被安全策略拒绝的文件 | 保留既有格式与风险边界，不执行宏、不破解 | 只有现有权限及下载策略允许时才提供原文件下载 |

从现有弹窗抽出可复用的呈现和资源加载接口；旧聊天调用保持兼容，新页面的 loader 必须走上述重复上下文接口。不能因为复用 UI 而回退到无版本约束的 latest-document 读取。

每侧始终展示明确的“下载本次上传文件”“下载候选文件”，不可用时禁用并说明原因；下载按钮不只出现在预览失败分支。同名文件下载采用安全文件名，如“本次上传_材料.docx”“候选_材料.docx”，仅影响客户端下载名称，不改服务器文件名。

元数据先返回，按需加载当前候选；不一次下载全批候选。前端按类型和现有资源预算决定是否预览，不能先无条件拉取大文件 Blob 再判断超限。大文件下载采用鉴权流式读取及明确进度/取消；实现使用浏览器支持的保存机制，若退回 Blob 则必须设置内存预算并明确提示限制，不声称无限大小下载。

开发规范进一步固定最小实现：使用现有 JWT fetch 的有界流读取及 Blob 保存，每次下载上限 128 MiB、页面同时一项下载；超限明确不可用，不新增签名 URL、浏览器文件系统授权或宿主保存适配。首版服务端正文预览只返回最多 100,000 字符的已有区段，XLSX 网格与分页复用现有浏览器组件。

预览 URL 在关闭、切换候选和卸载时释放；取消未完成请求，防止旧请求覆盖新候选。文件流设置合适的 Content-Type、Content-Disposition、nosniff 与 private/no-store；不把上传 HTML/SVG 当应用同源活动页面执行。正文、表格和文件名均按不可信内容处理，不访问文件内外部资源。

## 6. MCP 最小增量

新增只读工具 duplicate_comparison_get：

- 输入：item_id、review_id、review_revision、candidate_id，可选 group_revision；严格 schema，拒绝未知字段。
- 输出：对比状态、安全元数据、两侧预览/下载能力、comparison_url；不输出完整正文或二进制。
- 范围：仅当前用户当前重复 review 的真实候选。
- 副作用：无文件或业务状态写入；无需额外确认；不创建 ChangeSet。
- 失败：结构化返回无权、过期、候选变化、页面地址未配置等原因；不得伪造 URL 或替用户做决定。

duplicate_review_get 的每个候选新增可选 comparison_available、comparison_unavailable_reason、comparison_url；保持旧字段。batch_get 复用这一投影，历史客户端可忽略新增字段。

工具描述写明：用户说“看看重复文件”“预览两份材料”“下载下来比较”时，先读取候选，必要时让用户选具体一组，再调用本工具并展示返回的链接。聊天话术不要求用户手写内部 ID；“第二个候选”只能绑定本轮真实列表中的稳定 ID。

用户查看后作决定时保留所见候选 ID 和 revision。若再次读取发现变化，提示重新查看，不把用户对旧内容的决定套用到新候选。已有 duplicate_decide 的幂等键、组修订与决定语义全部保留。

## 7. 浏览器登录与局域网部署

新增前端路径 `/duplicate-comparison`，查询参数只有业务 ID 和修订。页面 URL 不含访问令牌；浏览器用自己的登录态请求 API。MCP token 不进入链接、日志、页面脚本参数或 localStorage 自动注入。

App.tsx 的入口解析、popstate、登录成功与首次引导都要保存并恢复这一受保护目标；return target 仅允许本站固定路径及白名单参数，不能接受外站跳转地址。浏览器登录账号与该批次所有者不符时返回无权并提示切换账号，不能自动改用 MCP 的凭证。

新增后端配置 `INTEGRATION_REVIEW_WEB_BASE_URL`（建议名，当前尚不存在），仅由部署者设置为用户浏览器可达的 Web 根地址。未配置时仍可查重和决定，comparison_url 为 null 并返回 REVIEW_WEB_URL_NOT_CONFIGURED，不自动猜测 5173、127.0.0.1 或使用请求 Host 拼接地址。

本机开发例：`http://127.0.0.1:5173`。其他电脑参与时使用服务器局域网地址，如 `http://<服务器局域网IP>:5173`；该地址指向前端而非仅 API 的 8000。Vite 已有同源 `/api` 代理到本机 8000，生产部署需保证同样的路由和 SPA 深链接回退。正式部署使用 HTTPS。

部署本补充功能时：

1. 更新并重启 API，使新增接口与 Web 基址配置生效。
2. 更新并启动 Web；本机使用 `npm run dev`，局域网开发使用现有 `npm run dev:lan`，均在 apps/web 下执行。
3. 更新每台用户电脑实际运行的 apps/mcp 包/源码，重新连接或重启 WorkBuddy 以刷新工具列表。
4. 如果 mcp.json 的启动路径、解释器、API 地址和环境变量没变，通常无需修改 JSON；只更新服务器代码不能更新其他电脑上的本地 MCP 程序。
5. 现有文件导入仍使用原有 worker；本功能本身不新增 worker、scheduler、数据库表或迁移。若实施时实际增加 schema，必须补迁移和部署说明，不能保持“无需迁移”的旧结论。

如果为了固定未物化候选身份而在现有候选创建处补充快照 JSON，必须重启执行该代码的原有相关 worker；仅更新只读接口和前端不要求新增后台进程。旧候选缺少足够快照时明确降级，不在 GET 中回填历史事实。

## 8. 实施工作包与不影响现有功能的约束

| 工作包 | 主要文件/模块 | 完成条件 |
|---|---|---|
| A：后端投影与版本化读取 | ingestion 新 comparison service、schemas；integrations/router | 四类来源授权、版本绑定、流和已有预览读取、结构化降级通过 |
| B：浏览器对比页 | App.tsx、新只读页面、现有预览呈现、api/client.ts、types | 登录返回、双侧查看下载、多候选切换和失效状态通过 |
| C：MCP 接入 | server.py、client.py、工具 schema 与相关测试 | 原工具兼容；新工具能发现、返回实际可用链接 |
| D：发布与回归 | runbook、api-contract、Web/MCP 部署说明和烟测记录 | 本机与另一台局域网电脑真实打开、预览、下载并回到原决定流程 |

只读数据解析独立于决定服务；不为了拿到上传侧内容而提前发布工作副本，不放宽现有通用文件权限，不把 shared ACTIVE 权限扩展到跨用户暂存。旧网页上传对比的 UI 提取只能做兼容性重构，不改变原决定参数、分类和命名链路。

## 9. 验收标准

后端自动化覆盖：

1. 上传侧 + 已有工作副本；同批未完成候选；未物化受管候选，均能按固定对象访问。
2. 已验证 EXACT_SHA256/EXACT_HASH 显示字节一致；NEAR_DUPLICATE 和 SAME_FILENAME 不根据 similarity_score=1 冒充字节一致。
3. 错用户、错 review、跨候选、跨批暂存、回收站、过期及已清理文件被拒绝。
4. 打开页面后改名、换版、移动、取消、重复组变化，content/preview/decision 均遵守现有 revision 和快照约束。
5. 预览、下载不创建归档/分类/索引/文件操作任务，不改 review 决定和批次终态；无需外部模型。
6. 两侧下载内容与固定源版本字节一致，响应名称正确；无令牌、正文、哈希或绝对路径泄漏到链接及日志。

前端和 MCP 自动化覆盖：

1. 直接深链接、未登录、令牌过期、首次引导后返回相同对比目标；拒绝外站 return target。
2. 每侧均有下载按钮，预览失败/超限单侧降级；关闭、切换、快速返回不串内容且释放资源。
3. 旧网页上传弹窗继续正确工作；新页面不调用旧决定回调。
4. 工具 schema 拒绝路径/任意 URL；HTTP 错误透传；旧 review/batch 输出兼容；新增工具被真实注册。
5. Web 的 npm test 与 npm run build、MCP 测试及后端相关 ingestion/files/file_lifecycle 回归通过。

真实 WorkBuddy 烟测至少包含：同内容不同名 TXT、同名不同内容文件、有少量改动的 DOCX/XLSX、多页 PDF、同批重复、无可用预览的已允许文件、另一个用户的共享活动候选、过期候选。记录本机与局域网电脑的链接访问、登录、双侧预览、下载字节核对及原 duplicate_decide 后批次续跑结果。

验收话术：

```text
这份文件重复了，先让我看看本次上传和已有文件。
给我查看第二个重复候选的链接。
我想把两份文件分别下载下来比较。
我看完了，使用刚才那个已有文件。
我看完了，两份都保留。
```

前三句只查询和展示入口，后两句才按现有规则提交明确决定。页面可查看和可下载、权限与版本校验、原决定流程续跑、两种部署环境烟测全部通过，才算本补充功能交付。
