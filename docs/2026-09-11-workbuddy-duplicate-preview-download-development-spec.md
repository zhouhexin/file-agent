# WorkBuddy 重复文件预览与下载开发规范

- 日期：2026-09-11。
- 状态：待实施；本文完成不代表功能已开发或验收。
- 产品依据：[重复文件预览与下载补充方案](./2026-09-11-workbuddy-duplicate-preview-download-supplement.md)。
- 适用范围：WorkBuddy 已进入 File Agent 导入链路的重复候选预览、分别下载。
- 开发方式：严格按本文实施，复用现有能力，限定增量；不扩展附件桥接、分类、查重、文件决定或通用下载功能。

## 1. 执行约束

本文中的“必须”“禁止”和验收编号均为交付约束。实施者须先阅读项目 AGENTS.md、agent.md 及补充方案；当前用户明确要求和项目规范优先。本规范是本补充功能的直接实施依据，旧预览方案中的未实施阶段不自动加入范围。

每个工作包按“实现 → 对应测试 → 记录结果”完成。发现接口、数据模型或运行方式与基线不一致时，先核查实际代码；重要设计调整必须更新本文及补充方案，记录原因和新增范围，不能边开发边静默扩大功能。

冻结的实现边界：

1. 新增三个只读 HTTP 接口、一个 MCP 工具和一个受保护 Web 页面。
2. 复用现有数据库模型、存储路径策略、DOCX/XLSX/文本/PDF/图片预览组件及 JWT 登录。
3. 不新增数据库表、列、迁移、后台队列、worker、服务端预览派生件或第三方依赖。
4. 不更改 duplicate_decide 的输入、幂等语义、决定枚举、重复组规则和自动整理流程。
5. 新页面只查看和下载，决定仍回到 WorkBuddy；旧网页弹窗保留现有决定按钮及参数。
6. 修改量以职责和影响面衡量：不能为了少写几行而复用错误的权限或 latest-version 读取，也不能借机建立通用资源访问框架。

## 2. 代码基线与复用清单

| 已存在能力 | 本次复用方式 | 不可直接套用的原因 |
|---|---|---|
| IngestionDuplicateService._owned_context | 使用同一 batch/item/review 归属校验规则；必要时将该方法公开为只读上下文方法，原调用保留兼容 | get_review 会 synchronize_batch，并可能增加 review revision 和 commit，新 comparison 不得调用它推进状态 |
| IngestionDuplicateService._validate_working_copy_snapshot | 提取或复用同一 ACTIVE、version、revision、hash 判断 | 不得在新接口中放宽现有候选约束 |
| DocumentVersion、UploadDuplicateCandidate 的 compared_* 字段 | 作为两侧身份、版本和内容依据 | FileObject 无 document_version_id；不能任选该 Document 的第一个 FileObject |
| FileLifecycleStorageService | upload_path / working_copy_path 等路径安全能力 | 通用按 Document 寻找“可用原件”的方法存在历史对象及归档 fallback，不保证本次固定版本 |
| resolve_managed_relative_path | 服务端按真实 managed_file ID 找到根与相对路径后调用 | ManagedFileService.get_preview_response 会同步配置并 commit；新只读入口不能直接调用 |
| FileUploadService.get_preview 与现有页面/表格投影 | 复用格式和有界读取逻辑，查询加固定版本条件 | 原入口可读“当前最新成功解析”，不得把旧解析或别的版本当成本次预览 |
| DuplicateComparisonDialog.tsx | 抽取 PreviewSide、LocalDocxPreview、PreviewPane 等展示逻辑 | 旧 review 类型和 onDecision 参数与集成入口不同；禁止 as unknown 强转或伪造旧 review |
| StructuredSpreadsheetPreview / xlsxPreview / xlsxPreview.worker | 原样复用本地结构化工作簿、分页与资源限制 | 不重写 Excel 解析，不执行公式或外链 |
| App.tsx / auth/storage / getCurrentUser | 原有登录态与路径管理方式 | 不引入 React Router，不改造全站认证 |
| FileAgentIntegrationClient._business_json | 复用 HTTP 错误转接 | 不增加 MCP 本地下载缓存、Shell 或浏览器自动启动 |

基线还需注意：EXACT_SHA256 与同批 EXACT_HASH 均可能出现；similarity_score=1 单独不构成字节一致依据。旧弹窗不认识 EXACT_HASH，本次新页面消费后端规范化 verdict。

## 3. 文件修改边界

### 3.1 必需改动

| 文件 | 允许职责 |
|---|---|
| 新增 apps/api/app/modules/ingestion/comparison_service.py | 当前 review 两侧对象解析、快照、权限、只读资源响应 |
| apps/api/app/modules/ingestion/schemas.py | 新输入/输出 schema；候选新增三个可选入口字段 |
| apps/api/app/modules/ingestion/duplicate_service.py | 复用校验、附加入口字段；不改决定流程 |
| apps/api/app/modules/integrations/router.py | 三个 GET 路由，沿用现有依赖 |
| apps/api/app/core/config.py、实际配置模板 | 新 Web 基址配置及验证 |
| apps/mcp/file_agent_mcp/client.py、server.py | 一个新工具及 HTTP 转发、现有工具说明补充 |
| 新增 apps/web/src/features/chat/DuplicatePreviewPane.tsx | 从现有弹窗抽出的共享呈现与类型，不内置业务路由或决定 |
| 新增 apps/web/src/features/chat/IngestDuplicateComparisonPage.tsx | WorkBuddy 只读对比页、资源生命周期、多候选切换 |
| 新增 apps/web/src/features/chat/duplicateComparisonAccess.ts | 本功能的 URL 校验、资源预算、鉴权下载辅助，禁止扩成通用 SDK |
| apps/web/src/features/chat/DuplicateComparisonDialog.tsx | 兼容接入共享呈现，旧数据来源和决定回调保持不变 |
| apps/web/src/App.tsx、api/client.ts、types.ts、局部样式 | 路由、登录返回和新 API 类型 |

### 3.2 仅满足明确需要时修改

- file_lifecycle/service.py：仅在候选创建处补充未物化来源的比较快照 JSON；不得修改召回、评分、过滤、rank 或决定。
- files/service.py 或 extraction_repository.py：仅抽取已存在的预览投影/固定版本读取辅助；旧接口签名和行为兼容。
- file_lifecycle/storage.py：仅在现有方法不足时增加小型只读辅助；禁止修改写入、归档、重命名和清理行为。
- Web 样式使用本页作用域或既有预览类；不改整个聊天布局。

禁止修改分类 taxonomy、规则策略、Agent Planner、导入状态枚举、worker 调度、数据库事实表结构，以及 WorkBuddy 附件缓存授权范围。若实现需要越过该列表，须先修订方案并说明必要性。

## 4. 后端输入输出契约

### 4.1 查询参数

基础路径：`/api/integrations/v1/ingest-items/{item_id}/duplicate-comparison`。

| 参数 | comparison | /content | /preview |
|---|---|---|---|
| item_id | 必填 UUID 路径参数 | 同左 | 同左 |
| review_id | 必填 UUID | 同左 | 同左 |
| review_revision | 必填整数 >=1 | 同左 | 同左 |
| candidate_id | 必填 UUID | 同左 | 同左 |
| group_revision | 有组必填整数 >=1；无组禁止传值 | 同左 | 同左 |
| side | 不接受 | UPLOAD / CANDIDATE | UPLOAD / CANDIDATE |
| snapshot_id | 不接受 | 必填，64 位小写十六进制 | 同左 |
| disposition | 不接受 | inline / attachment，默认 attachment | 不接受 |
| max_chars | 不接受 | 不接受 | 1–100000，默认 100000 |

新接口使用严格 schema，未知查询字段拒绝为 422；不能依赖 FastAPI 默认忽略未知 query 参数。UUID 对外规范化为字符串，不更改数据库主键类型。所有接口复用 get_current_user、get_db 和 require_integration_ingest_enabled。

首版 /preview 只返回有界已有正文区段，复用 FilePreviewSection；不新增服务端 Excel 网格接口或全文分页协议。XLSX 的结构化展示与分页复用当前浏览器组件。截断时返回 truncated=true，完整内容通过下载获得。

### 4.2 数据模型

新增模型建议名固定为 IngestDuplicateComparisonQuery、IngestDuplicateContentQuery、IngestDuplicatePreviewQuery、IngestDuplicateComparisonSide、IngestDuplicateComparisonResponse、IngestDuplicatePreviewResponse。

ComparisonResponse 字段：

```text
item_id, review_id, review_revision, candidate_id
group_revision: int | null
snapshot_id: string
status: READY | PARTIAL | UNAVAILABLE
verdict: EXACT_CONTENT | SIMILAR_CONTENT | SAME_NAME | UNKNOWN
comparison_url: string | null
upload: ComparisonSide
candidate: ComparisonSide
```

ComparisonSide 字段：

```text
filename: string（不可用时允许安全占位名称）
source_kind: UPLOAD | WORKING_COPY | SAME_BATCH_UPLOAD | MANAGED_SOURCE
size_bytes: int | null
content_type: string | null
preview_status: AVAILABLE | UNAVAILABLE
preview_mode: IMAGE | PDF | TEXT | DOCX | XLSX | SECTIONS | NONE
download_available: bool
reason_code: string | null
```

PreviewResponse 返回 item_id、review_id、candidate_id、snapshot_id、side、filename、sections、truncated；正文只返回给浏览器。不能把此结果加入 MCP comparison 输出。

状态计算固定：单侧 preview AVAILABLE 或 download_available=true 即“可查看/下载”；两侧可用为 READY，一侧为 PARTIAL，均不可用为 UNAVAILABLE。下载可用不承诺浏览器无限大小保存；第 7 节规定客户端预算。

候选新增字段：comparison_available: bool=false、comparison_unavailable_reason: str|null=null、comparison_url: str|null=null。它们仅表示入口是否能提供，不等同于两侧实时内容均可用。原字段和字段语义保持不变。

### 4.3 只读算法

comparison、content、preview 均执行下列统一验证；无 commit、add、flush、状态同步或新 job：

1. 按 item_id 读取 item、batch 和 review；校验 batch.user_id、review.user_id、upload_document_version_id 的一致性，未经授权返回 404。
2. 校验 review_id、WAITING_CONFIRMATION 和 expires_at；过期只返回状态错误，不在新 GET 中写回 EXPIRED。
3. 校验 review_revision、candidate 归属及存在的冻结 candidate_ids；存在重复组时校验真实组、成员关系和 group_revision，无组不接受组参数。
4. 解析两侧固定对象。候选来源优先级：candidate_ingest_item_id → candidate_working_copy_id → candidate_managed_file_id；同时带 managed 与 working ID 时以 working 为准。某种指向已经失效时，不向另一来源偷偷 fallback。
5. 重新核验权限、状态、版本、内容大小/指纹及实际受控路径。资源不存在、格式不支持等单侧问题进入 Side 状态；对象被换版、撤权、回收、改名移动等过期问题整体返回 409/404。
6. 生成两侧快照和能力；content/preview 必须与请求 snapshot_id 一致。
7. 读取选定资源或返回投影；HTTP 连接结束即释放文件句柄，不能保持数据库行锁等待浏览器传输。

review 读取的原接口继续保留原 synchronize 行为；追加链接投影不得重新调用 get_review 或进行全候选字节读取。batch_get 通过现有 pending_duplicate_reviews 取得新增字段，不再新增一次全批扫描。

## 5. 固定对象与快照

### 5.1 版本绑定

- UPLOAD：读取 item.upload_document_version_id 对应 DocumentVersion，校验 Document 归属、storage_tier 与受控存储位置；不得使用“这个 Document 最新版本”。
- WORKING_COPY：复用现有 compared_version_id / compared_working_copy_revision / compared_sha256 与 ACTIVE 判断，取固定版本的 storage_path 和当前经校验的 filename。
- SAME_BATCH_UPLOAD：目标 item 必须属于相同 batch、相同用户和真实重复组，取 compared_version_id；主条目状态变化不授权读取另一人的暂存或另一个版本。
- MANAGED_SOURCE：按 managed_file ID 找到真实 root、相对路径、当前修订；复用根 enabled、角色与路径策略，不接收用户提交的 root/path。不触发 SOURCE_ANALYSIS 或 MATERIALIZE。

为最小化变更，上传暂存已清理时本功能返回单侧 COMPARISON_CONTENT_GONE，不启用通用归档 fallback；用户仍可查看另一侧。该行为必须有真实提示，不得显示已成功加载两份文件。

已持久化正文必须属于固定 document_version_id，提取成功且完整性符合既有预览条件；源侧还须绑定该 ManagedFileRevision 的 analysis_document_version_id。只有 document_id 一致但 version 缺失或不同的历史页面不可复用，不重新解析来填补。

### 5.2 未物化来源的最小补充

先检查已有 compared_sha256 与候选 evidence 是否足够；新增候选确实缺少来源修订时，仅在原候选创建事务中为 match_evidence_json 添加以下命名空间：

```json
{
  "comparison_snapshot_v1": {
    "managed_file_id": "uuid",
    "managed_revision_id": "uuid-or-null",
    "size_bytes": 12345,
    "quick_fingerprint": "internal-value",
    "file_identity": "internal-value-or-null"
  }
}
```

这些字段仅供内部重验，不返回 MCP/浏览器。保留原有 evidence 内容，不改变原候选列表。补充快照不额外启动哈希计算或扫描；缺少可靠内容指纹时标记 SNAPSHOT_UNAVAILABLE。

旧候选若已具有固定版本、大小与可靠内容指纹，可直接读；否则返回明确不可用，禁止在 GET 中取当前版本写成“历史快照”。现有 review_get 未承诺重建候选，不能提示“刷新必定修复”。新增重建查重 API 不在本次范围。

### 5.3 snapshot_id 和确定性标签

服务端对规范 JSON 计算 SHA-256 形成 snapshot_id：包含协议版本、item/review/candidate ID、review/group revision、两侧对象 ID、版本 ID、工作副本 revision 或源修订、内部内容指纹与大小。键排序、UTF-8、无随机值；不包含 token、路径、当前时刻或预览加载状态。

这是组合版本指纹，不返回原文件 SHA-256，不作为鉴权密钥，不新增签名 URL 或票据表。相同事实得到相同 snapshot_id，内容对象变化必须改变值或直接拒绝。

verdict 规则：EXACT_SHA256 / EXACT_HASH 只有在两侧可靠哈希和大小一致时为 EXACT_CONTENT；NEAR_DUPLICATE 为 SIMILAR_CONTENT；SAME_FILENAME 为 SAME_NAME；未知类型为 UNKNOWN。精确候选事实不一致时返回 STALE 错误，不能改成“近似”继续沿用决定。预览失败不改变已有确定性匹配结论。

### 5.4 字节传输与并发

content 使用受控版本定位、打开只读句柄并验证实际大小和 SHA-256，再回到句柄起点流式发送；复用现有哈希算法，小型辅助缺失时可新增，不引入缓存服务。文件须为普通文件且符合现有路径策略。使用同一已验证句柄，禁止验证后由 FileResponse 重新按路径选择别的文件。

流式响应采用有界块（建议 256 KiB），传输时累计哈希并在最后一块发送前校验实际总大小、内容哈希和文件 stat；变化立即终止传输。响应带准确 Content-Length，客户端长度不足或网络异常不得报告成功。已发送响应头后不能再返回 JSON 409，此时必须中断流并写结构化错误日志；发送前发现变化才返回 409。

这项完整性校验仅发生在用户实际加载/下载的那一侧，不在 batch_get/review_get 中执行。文件变化可能导致已有部分字节到达浏览器，但客户端只能在完整下载成功后提供 Blob 保存入口；失败时丢弃部分内容。

首版不实现 Range/断点续传；带 Range 的请求按普通完整 200 响应处理，不能返回虚假的 206。PDF 使用完整且有预算的鉴权 Blob 预览。无服务端临时复制或永久下载副本。

## 6. 错误与链接契约

| 条件 | HTTP/结果 | 用户行为 |
|---|---|---|
| 无登录/令牌失效 | 沿用 401 | 登录后返回本页 |
| 错用户、错 item/review/candidate、无权根 | 404 | 显示无权或不存在，不泄漏候选内容 |
| 非法参数/未知参数 | 422 | 修正调用，不自动猜测 |
| review revision 变化 | 409 DUPLICATE_REVIEW_REVISION_CONFLICT | 重新读取列表，用户重新核对 |
| 组 revision 变化 | 409 DUPLICATE_GROUP_REVISION_CONFLICT | 同上 |
| 候选换版/改名/移动/回收/源变化 | 409 DUPLICATE_CANDIDATE_CHANGED | 旧预览失效，不自动应用旧决定 |
| review 已解决/过期 | 410 DUPLICATE_REVIEW_CLOSED / DUPLICATE_REVIEW_EXPIRED | 展示终态，结束当前查看 |
| 单侧内容丢失 | metadata 标单侧不可用；content 返回 410 COMPARISON_CONTENT_GONE | 另一侧继续使用 |
| 无快照/无已有正文 | 单侧 SNAPSHOT_UNAVAILABLE / PREVIEW_NOT_AVAILABLE；preview 返回 409 | 允许时下载，不触发解析 |
| 不支持安全 inline | 415 COMPARISON_INLINE_UNSUPPORTED | 允许时 attachment 下载 |
| Web 基址未配置 | 链接 null、REVIEW_WEB_URL_NOT_CONFIGURED | 明确要求部署配置；不影响旧查重决定 |

错误格式复用现有 error 包装。整体权限/修订错误优先于两侧内容错误，不能用 PARTIAL 掩盖越权或换版。

固定配置名：INTEGRATION_REVIEW_WEB_BASE_URL，默认空。仅接受 http/https origin，可有末尾斜杠；不接受账号密码、查询、fragment、非根 path 或外部输入拼接。去除末尾斜杠后追加 `/duplicate-comparison` 及白名单 query。无效非空配置按现有配置验证方式报错；空配置保留旧服务可用。

URL 仅含 item_id、review_id、review_revision、candidate_id、可选 group_revision；参数必须编码，不带 token、原路径、原文件名或下载授权。Web URL 只来自部署配置，不能从 Host/X-Forwarded-Host 或 MCP 参数推断。

## 7. 前端具体实现

### 7.1 路由与登录

在现有 AppPath、readInitialPath 和页面渲染分支增加 `/duplicate-comparison`，不替换路由体系。新增解析辅助仅接受本站该路径、合法 UUID 和整数参数，拒绝重复 query 字段、未知字段及外站 return target。

保存的是经过校验的相对目标，可用现有状态加本标签页 sessionStorage；不能存 token 到 URL。登录、token 校验失败、首次引导、刷新和 popstate 均保留该目标，成功进入后清除 pending target。普通 `/chat` 登录和引导路径保持原行为；用户主动退出对比页时清除目标，防止下一次登录意外跳回旧页。

Web 与 MCP 使用相同 File Agent 账号；登录为另一账号只给无权提示和现有退出/登录入口，不由 MCP 注入凭证。

### 7.2 页面流程

1. 解析链接参数并用浏览器 JWT 调用 comparison；不先用有状态 review_get 刷新并覆盖链接修订。
2. 展示两侧名称、格式、大小、匹配结论和下载入口；仅加载当前候选。
3. 新页可通过既有 review_get 明确刷新候选列表；若得到新 revision，清空旧快照和预览，要求重新查看，不能静默把旧页面切到新候选。
4. 切换候选先取消旧请求，释放旧 Blob URL，取得新 comparison 后再读取；每个异步结果绑定 candidate_id + snapshot_id。
5. 页面底部说明“查看后回到 WorkBuddy 选择”，不调用任何 duplicate-decision 接口。

### 7.3 组件提取

DuplicatePreviewPane 保留现有 DOCX iframe sandbox/CSP、外链清理、XLSX 组件及转义文本行为。组件只接收 PreviewSide、标题和可选下载控件，不知道 review、candidate、user 或决定枚举。

旧 DuplicateComparisonDialog 保留原 props、loadSide、onDecision 和按钮行为；只替换相同呈现的组件引用。新页自行使用新 API loader，不能去调用 fetchUploadedFileBlob(document_id) 或 fetchManagedFileBlob(root,path)。

下载按钮在预览正常时也必须显示，不仅在失败降级分支出现。文件名使用安全 basename，前缀分别为“本次上传_”“候选_”；剔除 CR/LF、分隔符和控制字符，正确处理中文 Content-Disposition。只改变浏览器建议下载名。

### 7.4 首版资源预算与下载方式

为避免新增浏览器授权桥接，首版固定采用现有 JWT fetch → 有界读取 → Blob 保存，不实现 File System Access、签名 URL、认证 Cookie 改造或宿主保存适配。

本功能专用预算集中定义在 duplicateComparisonAccess.ts：

- DOCX 本地预览：复用现有 20 MiB 上限。
- XLSX 本地预览：复用 XLSX_MAX_LOCAL_BYTES（当前 25 MiB），不复制一个独立常量。
- 图片/PDF/纯文本原字节预览：每侧最多 25 MiB；超过则尝试已有正文或提示下载。
- 每次 Blob 下载：最多 128 MiB；页面同时最多一项下载，超限在发起正文请求前说明当前浏览器下载限制。
- 所有正文预览最多 100,000 字符并显示截断；不对整个超大 Blob 调用 text() 后才截断。

这些是本次查看器的资源预算，不是系统上传限制；开发不得修改导入大小设置。128 MiB 是本功能建议初值，若实测需调整须同步本文和烟测记录，不得移除上限。

下载时按响应流逐块读取、更新进度、检查 Content-Length 和累计大小，支持 AbortController 取消；任何超限、长度不符、网络失败都丢弃当前结果，最终成功才生成保存链接。不用 Promise.all 同时缓存多个大文件。

已有预览 Blob 可用于界面显示，但用户新点“下载”仍需调用当前快照 content 接口重验权限与状态；不要直接保存可能已失效的旧预览 Blob。关闭/切换时撤销所有 object URL。

本机与局域网 HTTP 均可使用此方式；超过 128 MiB 时明确不支持本页下载，不能以“下载后查看”作为一个实际不可完成的降级承诺。首版该限制属于验收记录的一部分。

## 8. MCP 接入规范

新增 duplicate_comparison_get，输入仅为第 4 节 comparison 参数；工具 schema、客户端校验和后端校验均拒绝路径、URL、未知字段及非法枚举。不新增下载工具或本地缓存目录。

Client 增加一个方法，调用 GET comparison，复用 _business_json 和请求关闭习惯。server 注册真实 handler、structured_output=true，工具说明包含“重复候选查看/预览/分别下载、返回浏览器页面链接、不会提交决定”。工具不把 /preview 输出交给模型。

duplicate_review_get 和 batch_get 的候选附带可选链接；元数据字段构造必须轻量且脱敏，不为整个批次打开所有候选文件。旧客户端忽略新字段仍能决定。

WorkBuddy 展示服务器返回的完整 comparison_url，不自己拼接，不声称已经预览或下载。用户说“第二个”时按最近一次真实候选列表解析；跨条目或同名不唯一时先选对象，不靠文件名搜索替换候选。

用户看完后再决定，继续传所见 review_revision、candidate_id 和原有组参数。新旧修订冲突按旧接口拒绝，不自动用新 revision 重试决定；不存在浏览器“已查看”事实时，不能伪称系统记录了用户已读。本次不新增用户阅读确认审计或第二次确认。

## 9. 实施顺序和验证门槛

| 工作包 | 实现 | 必须先通过的验证 | 禁止提前宣称 |
|---|---|---|---|
| D1 | schema、只读上下文、来源解析、快照 | T01–T08 | 仅 schema 就说已支持对比 |
| D2 | 三个路由、内容流、已有预览、Web 基址 | T09–T15 | 只返回链接就说可以下载 |
| D3 | 共享呈现提取、新页、登录返回、下载 | T16–T22、旧弹窗回归 | 只复用旧弹窗就说 WorkBuddy 已接通 |
| D4 | 新 MCP 工具、候选可选字段和说明 | T23–T26、旧 MCP 回归 | 只更新 API 就说客户端工具已更新 |
| D5 | 发布说明、实机烟测和结果记录 | T27–T30 | 自动化通过就标记真实 WorkBuddy 验收通过 |

D1–D4 可以按可验证单元提交。所有新增/修改代码按项目规范添加中文说明，测试说明所保护的真实边界。D5 完成前状态写“实现完成、待实机验收”，不得写“全部交付”。

## 10. 验收用例清单

| 编号 | 场景 | 必须观察到的结果 |
|---|---|---|
| T01 | 上传 + 活动工作副本 | 两侧固定版本身份正确，原件字节不变 |
| T02 | 同批未完成重复 | 读取同用户同组固定上传版本，无需等归档；WAIT_AND_REUSE 原流程保持 |
| T03 | 未物化受管候选 | 受控读取与当前快照一致；无物化/解析 job |
| T04 | 跨用户、跨批、错候选 | 404，响应无对方名称、路径、正文 |
| T05 | 已回收/禁用根 | 拒绝访问，不 fallback 历史文件 |
| T06 | 旧候选缺快照 | 明确不可用，无 GET 写回 |
| T07 | EXACT 两种类型、近似/同名 | 只有可靠字节一致显示 EXACT_CONTENT；相似度 1 不误判 |
| T08 | 相同/变化快照 | 相同事实指纹稳定；改名/换版/组变化拒绝旧指纹 |
| T09 | GET 只读性 | 无 sync_configured_managed_roots、synchronize_batch、commit、正式关系或 job 写入 |
| T10 | /preview 固定版本 | 旧/无版本页面不被误复用，无新抽取，截断正确 |
| T11 | 分别下载与中文同名 | 下载字节哈希等于对应版本；名称区分两侧且无响应头注入 |
| T12 | 文件校验后被替换/传输中改变 | 发送前 409；发送中中断且客户端不保存成功文件 |
| T13 | 暂存清理/过期/已决定 | 410 或单侧不可用；不自动重传、不延长保留期 |
| T14 | 非法参数、活动内容、安全头 | 未知参数 422；危险 inline 拒绝；no-store/nosniff 正确 |
| T15 | 配置为空、非法、Host 伪造 | 不猜链接；非法配置明确报错；链接不受请求 Host 影响 |
| T16 | 未登录/过期/首次引导/刷新 | 登录后回原合法对比目标；正常聊天登录行为不变 |
| T17 | 外站/非法 return target | 不跳外站，不接受任意回调路径 |
| T18 | 两侧常见格式/单侧失败 | 可分别查看下载；失败侧不阻塞成功侧 |
| T19 | 多候选快速切换 | 请求取消、URL 释放、不串候选正文 |
| T20 | Web 页副作用 | 新页无决定按钮/POST；旧弹窗决定按钮参数与行为原样 |
| T21 | 超限/取消/网络断开 | 元数据先拦截；流累计超限也拒绝；无部分成功保存 |
| T22 | DOCX/XLSX 安全呈现 | 沙箱/外链/公式边界与既有实现相同，无新增第三方网络访问 |
| T23 | MCP 注册、schema、HTTP 错误 | 真实工具可发现；错误保留；不返回二进制或正文 |
| T24 | 旧 review/batch 输出 | 只增加可选字段；旧 duplicate_decide 和 batch_resume 回归通过 |
| T25 | 查看后旧修订提交决定 | 后端拒绝；客户端不自动换新候选或 revision 重试 |
| T26 | 只更新服务器/更新客户端 | 记录新工具需更新本地 MCP；启动配置不变无需重写 JSON |
| T27 | 本机 WorkBuddy 实测 | 点链接、登录、两侧预览下载、回聊天选择成功 |
| T28 | 另一台局域网电脑实测 | Web 可达，地址非该客户端 localhost，API 代理正常 |
| T29 | 混合批次 | 查看/失败项不阻塞非重复文件；原件、分类、命名及索引回执保持 |
| T30 | 真实样本矩阵 | TXT、DOCX、XLSX、PDF、无预览格式、同批重复、过期候选和超限均有记录 |

自动化必须检查实际结果，不能只检查源代码里出现了函数名。后端使用隔离测试数据和临时文件；不要以生产批次作为 pytest fixture。LLM 与宿主相关自动化用 deterministic fake；T27–T30 必须额外实测。

## 11. 测试命令与交付证据

建议新增后端 test_ingest_duplicate_comparison.py、MCP test_duplicate_comparison.py，以及 Web 的 duplicateComparisonAccess.test.ts / IngestDuplicateComparisonPage.test.mjs；遵循现有测试框架，不为本功能新增测试依赖。新增文件实现后执行，不能在文件未存在时称以下命令已通过。

从项目根 PowerShell 执行：

```powershell
$env:PYTHONPATH = 'apps/api'
& 'D:\anaconda\envs\myenv\python.exe' -m pytest apps/api/app/tests/test_ingest_duplicate_comparison.py apps/api/app/tests/test_integration_ingest_api.py apps/api/app/tests/test_files.py apps/api/app/tests/test_file_lifecycle.py apps/api/app/tests/test_file_lifecycle_storage.py apps/api/app/tests/test_managed_files_api.py apps/api/app/tests/test_managed_files_path_policy.py apps/api/app/tests/test_alembic_migration_graph.py -q
```

```powershell
$env:PYTHONPATH = 'apps/mcp;apps/api'
& 'D:\anaconda\envs\myenv\python.exe' -m pytest apps/mcp/tests -q
```

```powershell
Set-Location 'E:\PycharmProject\file-agent\apps\web'
npm test
npm run build
```

如果改动实际触及文件流、目录映射或生命周期公共辅助，补跑对应 storage/managed-source/filesystem-jobs 回归；若触及范围超过本文，先更新规范。禁止以只运行新增 happy-path 用例代替回归。

实施时创建独立验收记录文档，至少记录提交号、环境、实际测试命令、通过/失败/skipped 原因、T01–T30 结果、浏览器和 WorkBuddy 版本、本机/LAN 实测，以及下载预算限制。敏感样本及正文不入 Git；下载字节校验报告只保留脱敏 ID 和一致性布尔结果。

## 12. 发布、重启和回退

实施时同步 README.md、docs/runbook.md、docs/api-contract.md 和实际使用的 .env.example；无需更改 database-schema 的表定义，只在需要时注明现有 evidence JSON 的新命名空间。

发布顺序：

1. 更新服务器代码，配置 INTEGRATION_REVIEW_WEB_BASE_URL 为用户浏览器可达的 Web origin。
2. 重启 API；更新 Web 并启动。开发在 apps/web 执行 npm run dev，本机地址为 127.0.0.1:5173；局域网用 npm run dev:lan 并配置服务器局域网 IP。生产 Web 必须支持 `/duplicate-comparison` 的 SPA 回退与 `/api` 同源代理。
3. 若此次修改了原候选创建处的快照 JSON，重启运行该生命周期代码的原有相关 worker，使下一批候选包含新快照；没有该修改则本只读功能不要求单独重启 worker。不新增启动脚本或队列。
4. 更新每台客户端实际运行的 MCP 包/源码，重连或重启 WorkBuddy 刷新工具列表。mcp.json 的 command、args、API 地址不变时无需改文件；只更新服务器无法替换客户端本地 MCP。
5. 用一组全新测试文件完成本机和 LAN 烟测，再进行更大批次测试。

回退时恢复 API/Web/MCP 的前一版本；可选字段与 evidence JSON 必须允许旧代码忽略，无数据库 downgrade。浏览器已下载的文件不回收，旧聊天里的页面链接可能不可用，应明确提示；批次、原候选和决定事实全部保留，不运行 reset 或重分类脚本。

## 13. 最终交付检查表

- [ ] 修改范围符合第 3 节，没有新增框架、依赖、worker 或数据库结构。
- [ ] 三个 GET、一个 MCP 工具、一个 Web 页面实际接通。
- [ ] 四种对象来源按稳定身份处理，缺失/过期有明确降级。
- [ ] 常见格式预览与两侧下载可用，128 MiB 下载边界明确且经过验证。
- [ ] 浏览器登录返回、本机和 LAN 深链接均通过。
- [ ] 原网页上传、重复决定、批次恢复、分类和命名回归通过。
- [ ] T01–T30 结果有可核验记录，未执行项没有标为通过。
- [ ] README、runbook、API 契约与实际启动/限制一致。

所有必要项完成后方可标记本功能“已交付”。只有方案、代码或自动化测试之一完成，必须按实际阶段报告。
