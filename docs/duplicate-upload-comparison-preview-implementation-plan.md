# 重复上传候选对比预览实施方案

- 状态：待实施
- 日期：2026-08-26
- 适用范围：上传查重确认卡、共享活动工作副本预览、重复上传决策

## 1. 背景与问题

当前上传查重链路会在归档前返回以下候选：

- `EXACT_SHA256`：上传内容与已有文件的 SHA-256 完全相同。
- `NEAR_DUPLICATE`：本地文本 token 相似度达到阈值。
- `SAME_FILENAME`：文件名相同，但内容未必相同。

前端确认卡目前只展示候选文件名、逻辑路径和粗粒度相似度，并提供“使用现有文件”“继续上传并独立保留”
和“取消本次上传”。用户无法直观看出：

1. 系统是否已经确定两个文件字节级完全一致。
2. 如果只是高度相似，具体内容有什么区别。
3. 同名文件究竟只是名称相同，还是正文也相同。
4. Word、Excel 等浏览器不能直接打开的文件如何在确认前查看。

项目已经具备两个可复用基础能力：

- `GET /api/files/{document_id}/content`：读取当前用户私有上传或共享 `ACTIVE` 工作副本的受控文件流。
- `GET /api/files/{document_id}/preview`：读取已经持久化在 `document_pages` 中的安全正文预览。

主要缺口不是重新建设一套文件查看器，而是把重复候选、确定性一致性结论、两侧预览和决策校验连接起来。

## 2. 目标

本阶段完成后，用户在重复确认卡中应能：

1. 直接看见候选属于“内容完全一致”“内容高度相似”还是“仅文件名相同”。
2. 点击“对比查看”，同时查看本次上传文件和现有共享工作副本。
3. 对图片、PDF、TXT、MD、CSV 使用现有文件流预览。
4. 对 DOC、DOCX、XLS、XLSX 等文件查看后端受控解析后的正文、页或工作表预览。
5. 在对比弹窗中直接选择“使用现有文件”或“继续上传并独立保留”。
6. 当候选在用户查看后发生版本变化时停止决策，要求重新检查，避免基于过期内容作出选择。

## 3. 非目标

本阶段不实现：

- 浏览器内编辑 Office 文件。
- 像素级 PDF 或 Word 排版差异比较。
- 让 LLM 判断两个文件是否相同。
- 默认把上传正文发送给外部模型或外部服务。
- 预览回收站正文、其他用户上传暂存或受管原始目录的未授权内容。
- 因预览自动归档、分类、改名、移动或覆盖文件。

## 4. 核心产品规则

### 4.1 一致性结论必须分级

前端不得把所有 `similarity_score = 1` 都显示为“完全一致”。唯一允许展示“内容完全一致”的条件是：

```text
match_type == EXACT_SHA256
+ 上传版本 SHA-256 与候选当前版本 SHA-256 相同
+ size_bytes 相同
+ 候选仍是 SYSTEM_SHARED 中的 ACTIVE 工作副本，或尚未同步/物化的当前受管文件
+ 候选不属于回收站
= 字节级完全一致
```

用户提示建议为：

```text
内容完全一致
系统已校验文件大小和内容指纹，两份文件的字节内容一致。
```

其他类型固定使用更保守的文案：

| 类型 | 标签 | 解释 |
|---|---|---|
| `EXACT_SHA256` | 内容完全一致 | 已通过大小和 SHA-256 确定性校验 |
| `NEAR_DUPLICATE` | 内容高度相似 | 仅表示正文特征相似，可能存在日期、金额、人员或段落差异 |
| `SAME_FILENAME` | 仅文件名相同 | 不能据此判断正文相同 |

API 不向普通前端返回原始 SHA-256，只返回 `byte_identical`、`size_equal` 和 `certainty` 等安全结论。

### 4.2 预览与决策分离

- 查看预览是只读操作，不自动作出重复上传决策。
- 关闭预览不会取消上传。
- “使用现有文件”和“继续上传并独立保留”仍是显式用户决策。
- 预览准备失败不能伪装成文件不同；只能显示“暂时无法生成预览”。
- 即使预览不可用，用户仍可根据确定性结论、文件元数据或下载后查看再作决定。

### 4.3 原件与个人数据边界

- 本次上传侧只能由该 `UploadDuplicateReview.user_id` 查看。
- 现有文件侧只能是 `SYSTEM_SHARED + ACTIVE` 工作副本，或尚未同步/物化为工作副本的当前受管文件。
- 可以展示共享工作副本当前文件名、逻辑路径、大小、类型和更新时间。
- 不展示最初上传用户、上传暂存路径、来源会话、服务器绝对路径、原始哈希或个人审计记录。
- 回收站文件不提供正文预览。
- 尚未物化的当前受管文件通过受控只读源文件预览入口查看；不能把原始绝对路径返回前端。
- 回收站文件不参与上传查重；如果候选在确认期间进入回收站，候选立即失效并退出对比流程。

## 5. 推荐交互

### 5.1 重复确认卡

每个候选增加以下信息：

```text
[内容完全一致]
现有文件：2025年奖学金名单.xlsx
位置：奖助学金/2025/2025年奖学金名单.xlsx
大小：328 KB
更新时间：2026-08-20 14:30

[对比查看] [使用现有文件]
```

卡片底部继续保留：

```text
[继续上传并独立保留] [取消本次上传]
```

对 `EXACT_SHA256`，卡片直接说明已经确定字节一致；预览是帮助用户确认文件语义，不是系统判断一致性的
必要条件。

### 5.2 对比预览弹窗

弹窗采用桌面双栏、窄屏上下排列：

```text
┌─────────────────────────────────────────────────────────┐
│ 内容完全一致                                      [关闭] │
│ 已校验大小和内容指纹，两份文件字节内容一致。             │
├──────────────────────────┬──────────────────────────────┤
│ 本次上传                 │ 现有文件                     │
│ copy.xlsx · 328 KB       │ 2025年奖学金名单.xlsx · 328KB│
│ [正文/工作表预览]        │ [正文/工作表预览]            │
│                          │                              │
├──────────────────────────┴──────────────────────────────┤
│ [继续上传并独立保留] [使用现有文件] [仅关闭]             │
└─────────────────────────────────────────────────────────┘
```

预览方式：

- 图片：两侧图片预览，可缩放。
- PDF：两侧嵌入式 PDF 或“新窗口查看”，继续使用鉴权 Blob URL。
- TXT/MD/CSV：等宽文本预览；CSV 可以先按文本展示，后续复用表格组件。
- DOC/DOCX：按页或结构区段展示持久化正文。
- XLS/XLSX：按工作表切换，展示受控单元格文本；不得在浏览器执行宏。
- 不支持或加密文件：显示类型、大小、风险状态和“下载后查看”，不尝试破解。

### 5.3 加载和错误状态

对比弹窗必须覆盖：

- `READY`：两侧均可预览。
- `PREPARING`：Office 上传侧正在生成安全预览，显示任务进度。
- `PARTIAL`：一侧可预览，另一侧不支持或尚未完成。
- `UNAVAILABLE`：加密、解析失败或格式不支持；仍展示确定性匹配结论和元数据。
- `STALE`：候选当前版本已经变化，关闭决策按钮并要求刷新重复检查。

## 6. 后端设计

### 6.1 候选快照

在创建 `UploadDuplicateCandidate` 时扩展现有 `match_evidence_json`，不新增数据库字段：

```json
{
  "comparison_basis": "sha256",
  "upload_document_version_id": "upload-version-id",
  "upload_size_bytes": 335872,
  "candidate_working_copy_id": "working-copy-id",
  "candidate_document_version_id": "current-version-id",
  "candidate_size_bytes": 335872,
  "sha256_equal": true,
  "size_equal": true,
  "matcher_version": "upload-duplicate-v2"
}
```

哈希仍保存在现有版本和工作副本事实中，不复制到候选 JSON，也不返回前端。候选只保存用于重验的稳定版本
ID和安全比较结果。

近似候选额外保存：

```json
{
  "comparison_basis": "local_token_jaccard_v1",
  "candidate_document_version_id": "current-version-id",
  "similarity_score": 0.91,
  "matcher_version": "local_token_jaccard_v1"
}
```

### 6.2 对比投影服务

新增 `DuplicateComparisonService`，职责保持单一：

1. 校验 review 属于当前用户。
2. 校验 candidate 属于该 review。
3. 校验上传版本与 review 一致。
4. 校验候选仍为共享 `ACTIVE` 工作副本，或仍是尚未同步/物化的当前受管文件；明确排除回收站对象。
5. 根据当前版本重新计算确定性结论。
6. 输出脱敏元数据、预览状态和决策所需的候选 ID。
7. 不直接读取或返回完整文件正文。

建议响应 schema：

```json
{
  "review_id": "review-id",
  "candidate_id": "candidate-id",
  "status": "READY",
  "verdict": "EXACT_CONTENT",
  "certainty": "VERIFIED",
  "message": "系统已校验文件大小和内容指纹，两份文件的字节内容一致。",
  "facts": {
    "byte_identical": true,
    "size_equal": true,
    "filename_equal": false,
    "content_type_equal": true,
    "similarity_percent": 100
  },
  "upload": {
    "document_id": "upload-document-id",
    "filename": "copy.xlsx",
    "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "size_bytes": 335872,
    "preview_status": "PREPARING",
    "preview_mode": "TEXT_SECTIONS"
  },
  "existing": {
    "document_id": "working-document-id",
    "filename": "2025年奖学金名单.xlsx",
    "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "size_bytes": 335872,
    "preview_status": "READY",
    "preview_mode": "TEXT_SECTIONS"
  },
  "preview_job_id": "job-id-or-null"
}
```

### 6.3 API

新增：

```text
GET  /api/uploads/{upload_version_id}/duplicate-review/candidates/{candidate_id}/comparison
POST /api/uploads/{upload_version_id}/duplicate-review/candidates/{candidate_id}/preview
```

`GET comparison`：

- 只读取结构化比较结果和预览状态。
- 不产生文件解析副作用。
- 候选版本变化时返回统一错误 `DUPLICATE_CANDIDATE_CHANGED`，HTTP 409。
- review、candidate 或用户不匹配时统一返回 404，避免枚举其他用户上传记录。

`POST preview`：

- 对图片、PDF、纯文本等浏览器可直接查看格式，可立即返回 `READY`，不创建任务。
- 如果两侧已有成功 `document_pages`，立即返回 `READY`。
- 对缺少正文页的 Office 文件创建 `PREPARE_DUPLICATE_PREVIEW` 异步任务，返回 HTTP 202。
- 使用幂等键：
  `duplicate-preview:{review_id}:{candidate_id}:{upload_version_id}:{candidate_version_id}`。
- 前端复用现有 `GET /api/files/{document_id}/content`、
  `GET /api/files/{document_id}/preview` 和 `GET /api/filesystem-jobs/{job_id}`。

不建议让 `GET comparison` 隐式启动解析，因为 GET 必须保持只读，且 Office/旧版格式解析不应占用普通查询
请求。

### 6.4 预览准备任务

新增 worker 类型 `PREPARE_DUPLICATE_PREVIEW`：

```text
load review and candidate
-> revalidate user/review/version/candidate snapshot
-> resolve upload quarantine file through StorageService
-> inspect extension/MIME/macro/encryption risk
-> reuse successful extraction when parser fingerprint matches
-> otherwise run existing read-only parser in isolated temporary directory
-> persist DocumentExtractionRun + DocumentPage/DocumentElement
-> write structured job result
-> write audit/ChangeSet
```

约束：

- 只能读取 review 对应的上传版本，不接受前端路径。
- 旧版 `.doc/.xls` 继续使用隔离 LibreOffice Headless 转换边界。
- 不执行宏、链接、脚本或嵌入对象。
- 不调用外部 LLM、embedding 或网络服务。
- 预览任务只做解析，不做分类、摘要、索引或工作副本导入。
- 完整正文不得写入 job result、日志或 Graph State。
- 文本只进入 `document_pages` 等持久化事实，API 返回受限区段。

### 6.5 预览解析数据生命周期

预览准备产生的 `DocumentExtractionRun.extractor` 使用稳定标识，例如：

```text
duplicate-preview
```

处理规则：

- review 等待确认期间保留预览数据。
- 选择 `CANCEL_UPLOAD` 或 `USE_EXISTING_FILE` 后，由现有 cleanup job 删除上传暂存文件，并清理仅由
  `duplicate-preview` 生成的页面、结构元素和临时派生件；Review、ToolInvocation、ChangeSet 等审计事实保留。
- 选择 `CONTINUE_UPLOAD` 后，预览数据可以保留到共享工作副本正式解析成功，再由补偿任务清理，避免确认后
  立刻失去预览又留下长期重复正文。
- 正式工作副本解析仍绑定规范 `WorkingCopy.document_id/current_version_id`；不能把上传 Document 的 State
  快照直接当成共享工作副本长期事实。

### 6.6 决策参数收敛

推荐把前端提交目标从工作副本 ID 收敛为候选 ID：

```json
{
  "duplicate_review_id": "review-id",
  "decision": "USE_EXISTING_FILE",
  "selected_duplicate_candidate_id": "candidate-id"
}
```

后端根据 `review_id + candidate_id` 解析工作副本并再次检查版本快照。旧字段
`selected_existing_working_copy_id` 可以保留一个兼容周期；两个字段同时出现但指向不一致目标时返回 422。

确认时必须重新校验：

- review 仍为 `WAITING_CONFIRMATION`。
- candidate 仍属于 review。
- 工作副本仍为 `ACTIVE`。
- `current_version_id` 与候选快照一致。
- 当前内容哈希和大小仍满足当时展示的结论。

任何一项变化都返回 409，并提示“现有文件已经更新，请重新查看后选择”。

## 7. 前端设计

### 7.1 组件拆分

建议新增：

```text
DuplicateUploadReviewCard
├─ DuplicateMatchBadge
├─ DuplicateCandidateSummary
└─ DuplicateComparisonDialog
   ├─ DuplicateComparisonHeader
   ├─ DuplicatePreviewPane (upload)
   ├─ DuplicatePreviewPane (existing)
   └─ DuplicateComparisonActions
```

不要把新的 Blob URL、任务轮询和两侧预览状态全部继续堆入 `DuplicateUploadReviewCard`。

### 7.2 复用现有能力

- 抽取 `ChatPage.openAttachment` 中的预览/下载逻辑为可复用 hook，例如 `useDocumentPreview()`。
- 复用 `DocumentPreviewDialog` 的区段渲染逻辑，但新增双栏容器；不要复制 Office 正文渲染代码。
- 继续复用 `fetchUploadedFileBlob`、`getFilePreview`、`getFilesystemJob`。
- 所有 Blob URL 在关闭弹窗和组件卸载时调用 `URL.revokeObjectURL`。

### 7.3 决策状态

- 打开、关闭和滚动预览不影响 review 状态。
- 提交任一决策后禁用该 review 的全部按钮，防止重复提交。
- 多个候选分别打开对比，但一次只能有一个活动对比弹窗。
- 预览任务轮询不能阻塞同批其他文件的重复确认卡。
- review 被其他标签页解决或过期时，弹窗自动退出并刷新卡片。

### 7.4 可访问性

- 弹窗使用 `role="dialog"`、`aria-modal="true"`。
- 打开时聚焦标题或关闭按钮，关闭后焦点回到“对比查看”。
- 支持 Escape 关闭。
- `EXACT/NEAR/SAME_NAME` 不能只通过颜色表达，必须有文字标签。
- 双栏在窄屏下按“本次上传 -> 现有文件”顺序排列。

## 8. 审计、日志与 ChangeSet

- 纯 GET 预览不创建 ChangeSet，不记录正文。
- 首次解析上传文件以准备预览会写 `DocumentExtractionRun`，并创建真实 ChangeSet。
- 解析成功写 `TEXT_EXTRACTED`、`DOCUMENT_PAGES_CREATED`；复用已有结果写 `TEXT_REUSED`、
  `DOCUMENT_PAGES_REUSED`；失败写 `DOCUMENT_PROCESSING_FAILED`。
- Tool/Job 日志只记录 review ID、candidate ID、document ID、状态、耗时和错误码，不记录正文、哈希、路径或
  prompt。
- 用户最终选择继续、使用现有或取消，仍沿用现有重复上传决策审计。

## 9. 错误与降级

| 场景 | 行为 |
|---|---|
| 上传文件为图片/PDF/TXT | 直接使用鉴权 Blob 预览 |
| Office 已有 `document_pages` | 直接展示安全文本区段 |
| Office 尚未解析 | 创建预览任务并显示进度 |
| 加密文件 | 不尝试解析，显示“文件已加密，无法生成正文预览” |
| 宏文件 | 可读取安全文本时读取，但明确“不执行宏”；否则下载查看 |
| LibreOffice 不可用 | 返回结构化预览失败，允许下载或继续决策 |
| 候选被更新 | 标记 `STALE`，禁用使用现有文件，要求重新检查 |
| 候选尚未物化工作副本 | 使用受管源文件的受控只读预览；需要选择现有文件时先按需物化并重验身份 |
| 候选进入回收站 | 候选失效并退出上传对比；回收站不会成为新的上传查重候选 |
| review 过期或已解决 | 关闭对比弹窗并刷新确认卡 |
| 单侧解析失败 | 展示另一侧与元数据，不推断两份文件相同或不同 |

## 10. 测试计划

### 10.1 后端

至少覆盖：

1. `EXACT_SHA256` 只有在当前版本哈希和大小均相同时返回 `VERIFIED/EXACT_CONTENT`。
2. `NEAR_DUPLICATE` 永远不能返回 `byte_identical=true`。
3. `SAME_FILENAME` 明确返回 `NAME_ONLY`。
4. comparison API 只能访问当前用户自己的 review。
5. candidate 必须属于 review，不能用其他 review 的 candidate ID 拼接请求。
6. 任意用户可以预览共享 `ACTIVE` 工作副本和受控的未物化当前文件，但不能预览回收站或其他用户上传暂存。
7. 候选当前版本变化后 comparison 和 decision 都返回 409。
8. 预览任务幂等，同一快照重复点击只产生一个有效 job。
9. Office 预览只生成页面/元素，不触发分类、索引或工作副本导入。
10. 加密文件和 LibreOffice 失败返回结构化降级结果。
11. `CANCEL_UPLOAD` 和 `USE_EXISTING_FILE` 后清理 preview-only 正文和派生件。
12. `CONTINUE_UPLOAD` 后不覆盖原件，并在正式工作副本解析成功后清理重复预览数据。
13. API 响应和日志不包含 SHA-256、绝对路径、其他用户 ID 或正文全文。

### 10.2 前端

至少覆盖：

1. 三类候选显示不同且准确的文字标签。
2. “对比查看”可以打开两侧预览。
3. Office 预览从 `PREPARING` 轮询到 `READY`。
4. 一侧失败时另一侧仍可查看。
5. `STALE` 状态禁用“使用现有文件”。
6. 从对比弹窗提交决策后，原确认卡同步消失或更新。
7. 多候选不会串用 candidate ID 或预览内容。
8. 关闭弹窗和组件卸载时释放 Blob URL。
9. Escape、焦点返回和窄屏布局可用。
10. 前端测试、TypeScript 检查和 Vite build 通过。

### 10.3 手工验收样例

至少准备：

- 同内容不同文件名 TXT。
- 同名不同内容 TXT。
- 只改一段文字的 DOCX。
- 只改一个金额或日期的 XLSX。
- 多页 PDF。
- PNG/JPEG 扫描件。
- 旧版 `.doc/.xls`。
- 加密 PDF 或 Office 文件。
- 用户 A 上传、用户 B 再次上传相同内容的共享候选。

## 11. 实施顺序

### 阶段一：明确一致性结论与即时预览

1. 扩展候选快照和 `DuplicateCandidateResponse`。
2. 实现 `DuplicateComparisonService` 和 comparison API。
3. 前端增加三类标签和“对比查看”。
4. 图片、PDF、TXT、MD、CSV 先复用现有 Blob 预览。
5. 已有 `document_pages` 的 Office 文件复用现有正文预览。
6. 把重复决策参数收敛到 candidate ID，并保留旧字段兼容。

这一阶段即可解决“系统明明已通过 SHA-256 确认完全一致，但用户看不出来”的主要问题。

### 阶段二：上传侧 Office 按需预览

1. 新增 `PREPARE_DUPLICATE_PREVIEW` job。
2. 接入现有解析器和 LibreOffice 隔离转换。
3. 加入任务轮询、失败降级和清理策略。
4. 补齐 ChangeSet、日志和资源上限。

### 阶段三：确定性差异摘要（可选增强）

在两侧都已有结构化正文后，可以增加本地确定性差异摘要：

- 文本/Word：段落级新增、删除、修改数量和有界差异片段。
- Excel：工作表增删、行列数量变化和有界单元格差异范围。
- PDF：页数变化和抽取文本段落差异。

差异计算不得使用 LLM，不得把大文件全文放入 API；只返回有界差异片段及页、sheet、cell 定位。该增强不应
阻塞阶段一和阶段二上线。

## 12. 预计改动范围

后端：

- `apps/api/app/modules/file_lifecycle/schemas.py`
- `apps/api/app/modules/file_lifecycle/router.py`
- `apps/api/app/modules/file_lifecycle/service.py`
- `apps/api/app/modules/file_lifecycle/repository.py`
- `apps/api/app/modules/managed_files/worker.py`
- `apps/api/app/modules/files/service.py`（仅在需要抽取公共预览投影时调整）
- `apps/api/app/tests/test_file_lifecycle.py`
- `apps/api/app/tests/test_files.py`

前端：

- `apps/web/src/features/chat/DuplicateUploadReviewCard.tsx`
- `apps/web/src/features/chat/DocumentPreviewDialog.tsx`
- 新增 `DuplicateComparisonDialog.tsx`
- `apps/web/src/features/chat/ChatPage.tsx`
- `apps/web/src/api/client.ts`
- `apps/web/src/types.ts`
- `apps/web/src/features/chat/chat.css`
- `apps/web/tests/`

文档：

- `docs/api-contract.md`
- 本实施方案
- 如果最终授权或清理策略发生变化，同步更新 `agent.md` 和阶段六方案。

## 13. 完成标准

只有同时满足以下条件才算完成：

1. 用户能明确区分完全一致、正文相似和仅同名。
2. 用户能在重复确认卡中打开本次上传与现有文件的对比预览。
3. 常见浏览器格式无需额外解析即可查看。
4. Office 文件可以按需生成受控正文预览，失败时有明确降级。
5. 候选变化后旧结论和旧决策不能继续生效。
6. 所有用户可查看共享活动工作副本，但其他用户上传暂存和个人数据仍隔离。
7. 预览不修改原件，不执行宏，不调用外部模型，不泄漏正文或绝对路径到日志。
8. 异步任务、解析事实、ChangeSet 和最终决策均可审计。
9. 后端测试、前端测试和生产构建全部通过。
