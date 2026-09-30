# WorkBuddy 文档内容提取到 Excel：可执行开发计划

日期：2026-09-20
状态：待执行；本文不表示功能已经实现或验收。
需求依据：[三格式扩展方案](2026-09-20-workbuddy-document-to-excel-demo-extension-plan.md)。

> 2026-09-20 设计修订：用户明确要求提取正文、字段或表格时，必须先展示提取结果并在后续消息中取得确认，
> 确认后才允许把正文和结构化记录保存到 File Agent。纯归档内部提取保持直接提交。完整安全契约见
> [显式内容提取保存前确认方案](2026-09-20-workbuddy-explicit-extraction-confirmation.md)，该方案优先于本文中
> “提取完成后直接 submit/source/append”的旧步骤。

> 2026-09-20 目标表设计修订：支持本轮上传 Excel 作为新目标、按完整文件名续写已有目标以及提示词新建目标；
> 当前可见文件名、稳定 ID、版本冻结和同名冲突规则以
> [Excel 目标表与按文件名持续追加规范](2026-09-20-workbuddy-excel-target-continuation-spec.md) 为准。

> 2026-09-21 图片 OCR 来源修订：所有 WorkBuddy 入口图片，无论 `DIRECT_ARCHIVE` 还是
> `CONFIRM_BEFORE_SAVE`，都必须由 WorkBuddy 宿主侧的可用 OCR 能力识别。不固定 Skill、工具或
> Provider；`provider_name` 必须回填实际来源。宿主 OCR 不可用时关闭式阻塞/失败，不得使用模型觉、
> 通用读取工具或 File Agent 内部 OCR 降级。普通网页/API 非 WorkBuddy 入口继续保留原 OCR 配置能力。

> 2026-09-21 复核分流修订：显式内容提取根据本轮提示词冻结两条路径。明确要求无需复核/确认、直接生成并保存
> 时使用 `DIRECT_SAVE + DIRECT_ARCHIVE`，submit 后继续 Excel 写入；明确要求预览/复核/确认或没有说明时使用
> `REVIEW_REQUIRED + CONFIRM_BEFORE_SAVE`。表达冲突先澄清。盖章审批仅在标题下全部必需小项均验证通过时
> 写入 `标题√`，小项文字不加勾，原始证据不变。

## 1. 执行规则与交付范围

按 D00 → D10 顺序执行。每一步完成代码、对应测试和记录后才能勾选；不以“接口已写”“fake 已通过”代替后续真实宿主验收。本文给出确定的接口、状态及修改边界，实施中如必须改变这些契约，先同步本文和 API 文档再继续。

最终交付：用户在 WorkBuddy 一次提交图片、Word 或 PDF 和字段要求，宿主完成内容提取，File Agent 接收全文、保存证据，沿用整理流程发布共享工作副本，累计记录并返回可下载 Excel。

硬性约束：

1. 新通道的 OCR、Word/PDF 正文与表格读取、必要转换及渲染都在 WorkBuddy 宿主工具中完成。File Agent 允许读取字节计算哈希、识别文件类型及 PDF 页数，不提取业务正文。
2. 新通道宿主失败时明确失败/待重试，不调用服务器 Word/PDF 解析、LibreOffice 或 OCR 兜底。
3. 老图片 Demo 和旧 OCR v1 保持接口兼容，WorkBuddy 附件及本地目录图片统一要求宿主侧 OCR，
   同时保留每次实际 `provider_name`，不设固定 Provider 白名单；普通网页/API 上传和已完成文件操作不受影响。
   不要通过全局禁用解析功能来满足第 1 条。
4. 所有已登录用户可以操作共享 ACTIVE 工作副本和数据集；上传暂存、个人会话和任务租约保留现有隔离，不新增 owner 授权页面或权限模型。
5. Excel 由 File Agent 生成。首轮支持新建累计表、向既有累计表追加并导出；不实现任意 Excel 模板的单元格回填。
6. 原件不可变；每次业务写入可审计，字段不能只来自模型猜测或短摘要。
7. 批次冻结 `extraction_save_policy`：纯归档为 `DIRECT_ARCHIVE`；显式内容提取按提示词冻结为
   `DIRECT_SAVE + DIRECT_ARCHIVE` 或 `REVIEW_REQUIRED + CONFIRM_BEFORE_SAVE`。两种显式提取链路不得在执行中切换。

格式：PNG/JPG/JPEG/WebP/BMP、DOCX/DOC、PDF。Word 内嵌图片、PDF 页内混合图文若宿主不能完整读取，返回 PARTIAL，不宣称全量成功。首轮不保证复杂续表、嵌套表和跨文件实体合并。

## 2. 开工基线 D00

当前图片累计表格代码仍含未提交和未跟踪文件，包括 `structured_datasets/`、迁移 `20260920_0001`、MCP `dataset_tools.py`。这些是本计划依赖，不能从仅有远端分支的旧版本直接开始，也不能清理掉。

- [ ] 阅读 `AGENTS.md`、`agent.md` 及所引用产品、蓝图、API、数据库文档；当前会话明确要求优先。
- [ ] 记录当前 commit、`git status --short` 和现有迁移 head，确认图片 Demo 文件完整。
- [ ] 保留其他工作区变更；按文件选择提交，不使用 `git add .` 混入 runtime、上传材料或本地配置。
- [ ] 使用已配置 Python 环境，不新建虚拟环境，不强制改包管理器。
- [ ] 跑本文第 10 节 A 组基线测试，写入执行记录；若有基线失败，先定位是否与本改动有关。
- [ ] 新建 `docs/2026-09-20-workbuddy-document-to-excel-execution-record.md`，记录每步变更、命令、结果、未完成项及真实烟测证据；不要将未执行步骤预先标成功。

已知历史情况仅供核对：上一轮 MCP 全套测试曾因缺少 `integrations/workbuddy/file-agent-marketplace-0.1.10.zip` 失败。重新确认现状，不能为获得全绿删除测试或伪造旧安装包。

## 3. 先冻结的实现契约

### 3.1 模式、来源与配置

| 名称 | 固定值/含义 |
| --- | --- |
| `extraction_profile` | `DEFAULT` 或 `WORKBUDDY_DOCUMENT`，默认前者 |
| `extra_request_executor` | `FILE_AGENT` 或 `WORKBUDDY`，默认前者；新模式固定后者 |
| `provider_contract_version` | `workbuddy-document-v1` |
| `extraction_save_policy` | `DIRECT_ARCHIVE` 或 `CONFIRM_BEFORE_SAVE`，默认前者 |
| `DocumentExtractionRun.extractor` | `workbuddy-external-document` |
| API/MCP 新开关 | `WORKBUDDY_DOCUMENT_EXTRACTION_ENABLED=false` |
| 图片数据集原开关 | 继续使用 `WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED` |

累计 Excel 的最终发布调整为单一普通工作副本：首次 export 在
`WORKING_COPY_STORAGE_ROOT/structured-datasets` 创建一个 `WorkingCopy`，后续 export 只为同一
`Document` 增加版本并更新 `current_version_id`。不再同时维护独立 derivative 导出文件和普通文件；
详见 `docs/2026-09-20-structured-dataset-working-copy-publication.md`。
| 集成接口总开关 | 继续使用 `INTEGRATION_INGEST_ENABLED` |

新建本功能批次要求总开关、数据集 Demo 开关、新文档开关均开启；旧 `INTEGRATION_EXTERNAL_OCR_ENABLED` 只控制旧 OCR 通道，不决定新通道是否可以回退。所有新开关默认关闭。

开关约束新批次创建，已经持久化为新模式的任务仍按其冻结模式执行，不能因为关开关而走服务器解析。部署关闭前先处理或取消存量任务，避免宿主工具隐藏后任务无人处理。

`extra_request_executor` 由服务端根据新模式固定，MCP 只暴露 `extraction_profile`；原始用户请求仍原样保存。不要把 `WorkBuddySubmissionService` 捕获的文字改成 null。普通导入保持原有附加请求执行规则。

### 3.2 数据模型：增加一条迁移，复用现有业务表

新迁移建议 `20260920_0002_workbuddy_document_extraction.py`，`down_revision=20260920_0001`；若执行时 head 已变化，按真实 head 建连续迁移，禁止制造双 head 或重写已应用迁移。

| 表/位置 | 新增或使用字段 | 用途 |
| --- | --- | --- |
| `external_extraction_tasks` | 新增 `source_manifest_json`，JSONB/SQLite JSON，非空默认 `{}` | 原文件固定范围：kind、filename、content_type、size_bytes、page_count；不复用旧页图 manifest |
| 同上 | 新增 `result_summary_json`，同类型同默认 | 最新验收覆盖状态、计数、工具来源、失败原因，不保存全文或租约 token |
| `document_versions` | 新增 `extraction_policy_json`，同类型同默认 | 永久标记本内容版本由宿主提取，丢失正文也不能回落服务器解析 |
| `ingest_batches.policy_json` | `extraction_profile`、`extra_request_executor` | 冻结本次编排方式 |
| `document_extraction_confirmations` | 任务、来源版本、结果指纹、状态、摘要计数 | 显式提取的预览/确认事实；不保存正文 |
| `document_pages.metadata_json` | 见下方固定 metadata | 保存区段顺序、来源及覆盖语义 |
| `document_elements.metadata_json` | 区段 ID、原 block ID、可靠位置 | 正式字段证据来源 |

新版本策略形状固定：

```json
{
  "mode": "WORKBUDDY_DOCUMENT",
  "contract_version": "workbuddy-document-v1",
  "origin_task_id": "任务 UUID",
  "origin_upload_version_id": "上传版本 UUID",
  "source_sha256": "原文件 SHA-256"
}
```

该策略只能由服务端写入，在启动任何正文读取之前固化；上传版本、同内容归档分析版本、工作副本版本必须传递。克隆允许 ID 改变，但哈希必须一致；内容改变后必须建立新版本及对应新提取任务，不照搬旧正文。默认 `{}` 的旧版本继续旧逻辑。

页面 metadata 固定含 `content_origin=WORKBUDDY`、`contract_version`、`origin_task_id`、`source_sha256`、`source_unit_id`、`unit_order`、`unit_kind`、`unit_status`、`coverage_status`、`provider_name`。克隆再增加原运行/页面/元素映射。Word 顺序按 `unit_order` 排，不依赖同一事务的 created_at。

`ExternalExtractionPage` 继续仅表示图片/PDF 的真实页；Word 不创建假第 1 页任务。新契约的 Word 区段直接进入 `DocumentPage/Element`。无需新增数据集、格式专用表或独立任务平台。

### 3.3 API 与 MCP 名称固定

API 基础路径 `/api/integrations/v1/document-extraction-tasks`，复用现有认证及统一错误响应。

| HTTP | 路径 | MCP / 用途 |
| --- | --- | --- |
| GET | `/{task_id}` | `document_extraction_get`，状态、覆盖摘要、可重试信息 |
| POST | `/{task_id}/claim` | `document_extraction_claim`，领取并由 MCP 下载受控原文件 |
| POST | `/{task_id}/renew` | `document_extraction_renew`，延长当前租约 |
| GET | `/{task_id}/source` | MCP 内部原文件下载，不开放任意 URL 参数 |
| POST | `/{task_id}/confirmation-previews` | `document_extraction_preview`，只登记完整结果指纹和计数，不接收正文 |
| POST | `/{task_id}/confirmation-previews/{confirmation_id}/confirm` | `document_extraction_confirm`，绑定用户确认与固定指纹 |
| POST | `/{task_id}/results` | `document_extraction_submit`，整文件结果回传 |

七条 API 路由放入独立 `external_extraction/document_router.py`，六个 MCP 工具放入
`document_extraction_tools.py`；不要让现有 v1 工具按参数猜测新旧契约。`CONFIRM_BEFORE_SAVE` 的 submit
必须重新计算完整载荷指纹并匹配最新 `CONFIRMED` 记录；`DIRECT_ARCHIVE` 不要求 preview/confirm。

claim 输入沿用严格 `worker_id`；renew 输入 `worker_id + lease_token`。download 要求当前用户、匹配 worker、有效租约，token 放 header，不进 URL/日志。claim 输出为：

```json
{
  "task_id": "UUID",
  "provider_contract_version": "workbuddy-document-v1",
  "source_version_id": "UUID",
  "source_sha256": "64 位小写十六进制",
  "lease_token": "随机 token，仅本次返回",
  "lease_expires_at": "UTC ISO8601",
  "source": {
    "filename": "申请材料.docx",
    "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "size_bytes": 12345,
    "source_kind": "WORD",
    "page_count": null,
    "resource_url": "/api/integrations/v1/document-extraction-tasks/UUID/source"
  },
  "limits": {"max_text_chars": 100000, "max_units": 500, "max_blocks": 2000, "max_payload_bytes": 4194304}
}
```

以上 JSON 是字段示意，测试 fixture 用真实合法 UUID/哈希。PDF `page_count` 由服务器只读容器元信息取得，不调用 `page.get_text` 或渲染。Word 和电子表格为 null；图片为 1。`.xls`/`.xlsx` 的 `source_kind` 为 `SPREADSHEET`，由 WorkBuddy 的真实电子表格能力读取，不进入图片 OCR。

MCP claim 必须实际下载并核对长度及 SHA-256 后，返回宿主可消费的 ResourceLink/缓存引用；下载失败不能返回“已准备好”。claim 的租约、版本、哈希、保存策略和资源信息必须作为 MCP 结构化结果交给 WorkBuddy。考虑到部分 WorkBuddy 版本只保留普通文本内容，MCP 还必须把经过同一输出模型校验的等价 JSON 放入第一条文本内容；该 JSON 是工具协议兼容表示，不是 Agent 自行重建。两种表示必须逐字段一致，且一句摘要永远不能代替完整结果。不能迫使宿主读取配置、原始本地附件或直连私有 API。缓存路径来自现有受控下载目录，仅作为受控工具结果供宿主读取，用户回复不得展示。服务器资源 URL 必须由客户端按固定 task_id 构造，不跟随外部重定向。

### 3.4 submit 严格输入

所有模型 `extra=forbid`、拒绝 NaN/Infinity；列表和文本有界。`task_id` 在 URL/MCP 参数中，body 不再重复。

| 字段 | 类型/约束 |
| --- | --- |
| `worker_id`、`lease_token` | 沿用当前租约 schema |
| `submission_key` | 1–200 字符；同一次发送/网络重试保持稳定 |
| `source_version_id` | UUID |
| `source_sha256` | 64 位小写十六进制 |
| `provider_name` | 1–120 字符，真实宿主工具名，不能写虚构识别服务 |
| `provider_version`、`provider_request_id` | 可空，最多 120/240 字符 |
| `coverage` | 下方对象 |
| `units` | 0–500 个区段；COMPLETE 时至少 1 个 |
| `error` | 可空 `{code: ≤120 字符, message: ≤1000 字符, retryable: bool}` |

`coverage`：`status=COMPLETE/PARTIAL/FAILED`、`unit_count`、`warnings`（最多 20 条，每条最多 500 字符）。unit_count 必须等于本次 units 数量，不能当作可靠物理页数。

每个 unit：

- `unit_id`：1–120 字符，在本次文件内唯一；`order` 从 0 开始连续。
- `kind`：`IMAGE_PAGE/PDF_PAGE/TEXT_SECTION/SHEET`。
- `page_number`：正整数或 null；PDF 必须覆盖容器的 1..N，图片必须为 1，Word 和电子表格固定 null。
- `sheet_name`：仅 `SHEET` 必填，必须是宿主真实读取的工作表名；同一提交内不能重复。
- `status`：`READ/BLANK/FAILED/UNSUPPORTED`。
- `text`：完整原文区段，允许空；READ 必须有非空正文，BLANK 不得带正文。
- `blocks`：总计不超过 2000 个，强类型结构，字段见下。
- `error`：FAILED/UNSUPPORTED 必须提供结构化原因；BLANK 必须在 `blank_reason` 说明实际读取结果，不能凭空推断。

block 固定字段：`block_id`（文件内唯一）、`kind=PARAGRAPH/TABLE_ROW/TABLE_CELL/TEXT`、`text`、`start`、`end`，可选 `table_id,row_index,column_index,bbox`。start/end 是 unit.text 的 Unicode 字符索引，左闭右开，服务端检查 `text == unit.text[start:end]`；不能精确定位时不提交 block。表格坐标只保存真实返回值，不根据模型补造；`SHEET` 的 `TABLE_CELL` 必须同时带真实 `row_index`、`column_index`。bbox 使用归一化 `[0,1]` 的 left/top/right/bottom，仅真实图像/PDF 页允许，缺失则 null。

全文 chars 按 units.text 总和计算，block 重复文本另计入最大 4 MiB 的整个请求体。应用路由读取 JSON 前限制请求字节，防止只在 Pydantic 后检查。文件本身大小沿用部署上传上限，MCP 下载上限必须与之兼容，不能误用导出 XLSX 的 32 MiB 上限。

COMPLETE 必须无失败/不支持 unit，PDF 页数一致，至少有可用正文。全空白文件按 `EMPTY_DOCUMENT` 返回，不能新建空记录骗成功。宿主完全无法读取时允许 FAILED + 空 units + error；PARTIAL 要提交已知页面结果/失败页，对 PDF 仍覆盖已知页号全集。

### 3.5 状态、事务与重试

| 情形 | task | ingest item / 后续动作 |
| --- | --- | --- |
| 创建新文档任务 | PENDING | WAITING_EXTERNAL_EXTRACTION，结束当前 worker 作业，不占租约轮询 |
| claim 成功 | CLAIMED | 仍等待宿主 |
| 提交合格 COMPLETE | COMPLETED | 同事务写正文/元素，继续现有近似查重或归档 |
| 宿主 PARTIAL/FAILED | PARTIAL/FAILED | 保留诊断，仍 WAITING_EXTERNAL_EXTRACTION；首轮不发布残缺工作副本 |
| 结构/范围/哈希不合法 | 不推进 | 返回 4xx，不能产生成功页面或归档任务 |
| 租约过期 | 旧 token 无效 | 可重新 claim，旧 worker 提交拒绝 |
| 用户取消导入 | 不再接受提交 | 按原取消状态投影，阻止晚到的归档及追加 |

新模式忽略旧 `INTEGRATION_ALLOW_PARTIAL_EXTRACTION=true` 的发布例外。不能影响旧 OCR v1 的部分发布行为。

PARTIAL/FAILED 的诊断正文用 `DocumentExtractionRun.status=PARTIAL/FAILED` 保存；不能调用把结果无条件改成 COMPLETED 的仓库方法。诊断不进入正文索引、分类和数据集。重试按整个文件生成新解析运行，旧诊断保留。

复用现有 task 的 `last_submission_key/digest`：只有最近已接收的请求可以原样重放；成功后不同 key 不再提交。摘要排除 lease_token/worker_id，只包含业务结果和来源。重放必须在当前用户与任务生命周期校验后进行，允许网络丢响应后在 token 已清空时取回相同结果，不再次推进工作流。较旧请求被新租约/新提交替代时返回明确冲突，不保证历史任意 key 的自动重放。

`submit` 的业务服务只 flush，不自行 commit；外层受控写入封装一个事务提交正文、任务、item、归档 job 与 ChangeSet。业务失败用保存点回滚，保留失败 ToolInvocation；`ok=false` 必须记 FAILED。新租约写入也记录 ToolInvocation，只有正文/业务状态变更需要相应 ChangeSet。可提取图片 Demo runtime 的公共审计帮助方法，不能整段复制成两套长期分叉实现。

成功接收的 submit 返回 HTTP 202，固定包含 `accepted`、`ok`、`reused`、`task`、`next_stage`、`filesystem_job_id`。`accepted=true` 只表示结果已持久化；COMPLETE 时 `ok=true`，PARTIAL/FAILED 时 `ok=false` 且 job_id=null、next_stage=WAITING_EXTERNAL_EXTRACTION。宿主失败结果的诊断写入应提交保留，同时 ToolInvocation 记录 FAILED，不能被“失败就全部回滚”的通用包装抹掉；只有结构错误或执行异常才回滚相应业务写入。

task 固定投影 `id,ingest_item_id,provider_contract_version,source_version_id,source_sha256,status,coverage,retryable,error,extraction_run_id`，不包含 token、内部路径或全文。ingest item 结果新增 `document_extraction_task_id` 和 `content_extraction_contract`，通过现有 batch_get/items 查询可发现；没有任务发现字段就不能要求宿主凭空得到 task_id。

## 4. 分步开发清单

### D01：宿主能力探测与固定样本

**依赖：D00。输出：执行记录中的能力矩阵及无隐私测试样本。**

- [ ] 在真实 WorkBuddy 列出能调用的图片识别、DOCX/DOC、PDF 读取工具，记录真实工具名、参数、输出形状、是否截断、支持的定位。
- [ ] 确认新 claim 提供的本地资源能被这些工具读取；不能假设上传消息中“看得到附件”就代表可重复读取全部文件。
- [ ] 确认 DOC 转换发生在宿主；没有该能力时标为未完成并确定宿主适配方案，不能切换到服务器转换。
- [ ] 创建无隐私样本：图片 2 条、Word 段落 1 条+表格 2 条、两页文字 PDF 各 1 条、扫描 PDF 2 条、按页混合 PDF 2 条；合计 11 条，金额固定并记录预期总和。DOC 使用 Word 样本的另存版本，在独立表验证，避免算入同一基准导致重复。
- [ ] 准备失败样本：缺页结果、页内未识别图片、损坏/加密文件、正文超限、同文字重复出现、恶意公式文本。

基准表固定两个必填字段：`name:string`（姓名）、`amount:decimal`（金额）。样本采用虚构姓名编号，期望值如下，合计 **11 行、1400.00**：

| 文件 | 姓名与金额 |
| --- | --- |
| 图片 | 样本甲 100.00；样本乙 200.00 |
| DOCX | 样本丙 50.00；样本丁 150.00；样本戊 300.00 |
| 两页文字 PDF | 样本己 80.00；样本庚 120.00 |
| 扫描 PDF | 样本辛 40.00；样本壬 60.00 |
| 按页混合 PDF | 样本癸 70.00；样本子 230.00 |

DOC 在独立表预期 3 行、500.00。不同源文件中的重复姓名、日期规范化等另建用例，避免改变主基准。

**完成条件：** 有真实宿主能力记录。后端 D02–D07 可用确定性假结果独立开发，但 D09 不能在宿主能力未验证时通过。

### D02：schema、配置与迁移

**文件：** `db/models.py`、`core/config.py`、`.env.example`、新迁移、`external_extraction/document_schemas.py`、`ingestion/schemas.py`。

- [ ] 按第 3 节实现字段及限制，配置开关默认 false。
- [ ] 添加 `extraction_profile` 到创建批次输入；`extra_request_executor` 由服务端派生，客户端不能任意改变。
- [ ] 新增 task source/result JSON、version policy JSON；JSONB 兼容现有 SQLite 测试写法，数据库默认值与 ORM 默认值一致。
- [ ] 更新契约文档注明新表字段、状态及 Word 空页码语义。
- [ ] 建立 `test_document_extraction_contracts.py`，覆盖完整/部分/失败结果、重复 unit/block、非法范围、超限、跨页 bbox、非有限数值。
- [ ] 在隔离数据库验证 upgrade、downgrade；用 PostgreSQL 隔离库实际应用一次，确认旧行默认值。仅生成 PostgreSQL SQL 不能代替真实迁移测试。

**完成条件：** schema 测试、迁移往返通过；旧表数据可读，未修改旧迁移。

### D03：把模式贯穿两种附件入口及幂等指纹

**文件：** MCP `server.py`、`attachment_transfer.py`、`workbuddy_submission.py`；API `ingestion/service.py`、`workflow.py`、`request_runner.py`。

- [ ] `workbuddy_attachment_ingest` 与 `workbuddy_submission_ingest` 均增加可选 profile；transfer.ingest/ingest_resolved、可信提交单入口和 batch API 完整透传。
- [ ] 保存原用户文字，不绕过可信提交单的原文一致性检查，不修改宿主稳定 submission_id。
- [ ] `DEFAULT` 的旧批次 fingerprint 和客户端 manifest digest **字节级兼容旧算法**：新字段为默认值时从摘要输入剔除；仅新模式显式纳入摘要。
- [ ] 同一 submission_id 重试同模式复用，修改模式仍使用原幂等键并返回 IDEMPOTENCY_CONFLICT，不能自动另建批次。
- [ ] 冻结策略包含新模式和宿主执行者，不能封存后切换。
- [ ] `IngestionWorkflow._schedule_ready_extra_request` 遇 `extra_request_executor=WORKBUDDY` 不创建服务器附加执行；`IngestRequestRunner` 再校验一次，阻止意外排队任务重复执行。
- [ ] item/batch 回执区分“导入完成”与“表格尚未追加”，不能因跳过附加请求而声称 Excel 已生成。
- [ ] Hook 仍只捕获/校验提交单；两份插件入口说明不得诱导先按 DEFAULT 导入后再切模式。

**测试：** 扩展 `test_attachment_transfer.py`、`test_workbuddy_submission.py`、`test_integration_ingest_api.py`，补旧 payload 摘要 fixture、可信文字保留和无重复 AgentRun 用例。

**完成条件：** 普通旧请求原样可重试；新任务只由宿主做字段编排。

### D04：新文档任务、租约与原文件下载

**文件：** 新 `external_extraction/document_service.py`、`document_router.py`、`lease_service.py`；原 `external_extraction/service.py` 仅必要公共抽取；`main.py`。

- [ ] 将通用 task 加锁、当前用户绑定、随机 token 哈希、续租、token 过期检查提取为公共帮助方法；旧 OCR v1 回归不变。
- [ ] `prepare_for_ingest_item` 新模式分派到文档服务，在已确认上传版本上写策略、创建唯一 task，源 manifest 不含客户端给出的任意路径。
- [ ] PDF 仅使用 `fitz.open`、`page_count`/加密状态等元数据；不使用旧 `_build_page_manifest`、`_merge_native_and_external_pages`。
- [ ] source GET 通过 task 固定版本定位 StorageService 资源，校验租约、大小与哈希；不接受任意文件路径，不把 token 放入 URL。
- [ ] 添加 contract mismatch 错误，旧页图接口不能领取新契约任务；新接口也不能操作 v1 任务。
- [ ] 任务状态接口返回完整、可解释的覆盖摘要；清理流程按契约分派，新文档源文件不被旧页图清理误删。

**测试：** 新 `test_document_extraction_api.py`：领取、过期续租、并发 worker、版本错配、跨用户暂存隔离、原文件下载、hash 不符、v1/v2 契约隔离、清理原件保护。

**完成条件：** 能领取并下载三种格式的准确字节；服务器正文解析调用次数为 0。

### D05：正文验收、审计与查重恢复

**文件：** `document_service.py`、新 `external_extraction/document_runtime.py`、`files/extraction_repository.py`、`file_lifecycle/service.py`、`ingestion/duplicate_service.py`。

- [ ] 实现第 3.4/3.5 节的完整校验、逐区段保存和覆盖摘要，provider 来源固定且真实工具名单独记录。
- [ ] 对无 block 的非空 unit 生成一个服务端全文证据元素，明确其定位仅为区段，不假称段落/单元格。
- [ ] 只在 COMPLETE 时推进原有近似查重/归档；近似比较使用已持久化正文，不重新读取文件。
- [ ] 在 `_check_upload_duplicates` 新模式分支先完成纯哈希精确检查：无精确候选则创建宿主任务；有精确候选先沿用已有重复决策，不调用正文近似解析。
- [ ] `CONTINUE_UPLOAD` 和同类“继续处理新上传”分支，在 `_enqueue_archive` 前执行 `ensure_document_content_ready`；未完成则创建/返回原任务，不能直接跳到归档。
- [ ] COMPLETE 后新近似查重的继续决策不再次创建任务；已完成任务检查与归档 job 幂等共同防止循环和重复。
- [ ] “使用已有文件”沿原工作副本映射：同哈希且有合格宿主正文才可进新填表；只有服务器来源或不同哈希时返回 `WORKBUDDY_CONTENT_REQUIRED`/`SOURCE_SNAPSHOT_CHANGED`，不把新上传正文挂到另一个版本。
- [ ] 取消、过期批次的晚到 submit 拒绝推进；PARTIAL/FAILED 可重新 claim 全文件重试，不能被旧 partial 发布策略放行。
- [ ] 审计只保存来源 ID、计数、状态和错误摘要，不把全文、token 或字段原文写入日志/graph_state。

**测试：** 新 `test_document_extraction_workflow.py`，覆盖精确/近似重复各决策、重复 submit、PARTIAL→COMPLETE、取消后回传、提交中异常回滚、失败审计保留。

**完成条件：** 单次成功回传只写一份成功运行、一次归档推进；失败无正式发布。

### D06：工作副本映射与禁止内部解析

**文件：** 新 `files/extraction_policy.py`；`file_lifecycle/service.py`；`files/extraction_repository.py`、`readable_source.py`；`agent/tool_registry.py`；`managed_files/source_analysis.py`；`file_rename/suggestion_service.py`、`uploaded_suggestion_service.py`。

- [ ] 公共 `resolve_extraction_policy(version)` 与 `get_required_host_extraction(version)`：受保护版本优先选同版本、同哈希、完整且来源明确的宿主运行；缺失立即返回受控错误。
- [ ] 在 `_extract_document_text_handler` 计算本地 parser hash、转换或解析之前检查策略；不要通过伪造本地 parser_config_hash 让缓存命中。
- [ ] `ReadableDocumentSourceResolver.resolve` 对受保护版本拒绝创建服务器可读派生件；这是防漏保护，不能当正常流程一直抛错使用。
- [ ] 工作副本克隆同时复制策略、页面、元素、覆盖信息，重建页面/元素 ID 及引用；所有克隆和复用分支保持哈希一致性。
- [ ] 源侧分析 `managed_source_extraction_is_current/fingerprint`、解析入口、命名建议的缓存选择复用宿主结果；无结果时等待/报错，不用文件名制造正文。
- [ ] 分类、摘要、索引消费固定宿主运行；已有独立命名/分类规则及确认语义不做无关更改。
- [ ] 检查 `DocumentIndexService.build_latest_for_user/build_latest` 等“选最新运行”入口；受保护版本明确传入合格基础运行 ID，不能索引后来生成的 dataset 证据片段运行。该固定选择必须延伸到页面读取、摘要和分类。
- [ ] 对新版本 `force_reprocess/force_reconvert` 不绕过策略，返回“需要 WorkBuddy 重新提取”；旧版本保持原行为。重提取入口本轮未做就不能返回假成功。

**测试：** 新 `test_workbuddy_content_origin.py`，对成功流程、OCR 失败、缓存缺失、重新处理、发布后分析、源侧扫描和命名建议设禁止调用桩。patch 调用方已导入的符号，如 `agent.tool_registry.extract_document_text`、`managed_files.source_analysis.extract_document_text`，不只 patch 定义模块；同时覆盖 DOC 转换、Docling 和底层 PDF/Word/OCR 入口。

**完成条件：** 从上传到导出的集成测试内所有服务器内容提取桩均未触发，且普通 DEFAULT 文件解析回归仍通过。

### D07：多格式数据集 source 与证据追加

**文件：** 新 `structured_datasets/source_adapter.py`；`schemas.py`、`service.py`、`router.py`；`structured_extraction/repository.py`；MCP `dataset_tools.py`。

- [ ] source 请求可固定 `document_version_id/extraction_run_id`，新增 `content_origin=WORKBUDDY` 过滤；新 Skill 必传。无新参数的旧图片请求保留原输出和行为。
- [ ] Word/PDF 只接受宿主来源，返回 `source_units`（正式 page UUID、kind、order、nullable page、全文）、coverage、固定版本/哈希；保留图片 `pages` 兼容输出。
- [ ] `DatasetAppend` 新增 `content_origin` 可选字段；新模式为 WORKBUDDY，服务端据版本策略再次强制检查，不能靠客户端省略绕过。
- [ ] `FieldCandidate` 新增 `evidence_refs`：每字段最多 4 个，每个含 `source_unit_id: UUID`、`quote: 1..8000 字符`，可选同时提供 start/end。原 raw_text 最大 4000，原 confidence 语义不变。
- [ ] 新引用必须在同一来源运行内；quote 精确定位，重复命中且无正确范围则 `EVIDENCE_AMBIGUOUS`；raw_text 只允许受 quote 支持，不能用短 quote 反向包含证明长原值。
- [ ] Word 页码 null；PDF 页码取服务端真实来源。旧请求 page_number=1 只在图片兼容路径使用。
- [ ] API/MCP 的 `FieldCandidate.page_number` 同步改为可空可省略；带 evidence_refs 时从证据确定位置，无 refs 的旧图片请求仍按默认 1 处理。兼容性校验先识别旧请求再规范化，不能把默认值变化写进旧载荷摘要。
- [ ] 在独立证据派生运行保存 quote、来源页 UUID、字符范围和对应原元素 ID，调用公共 finalizer；旧全文/元素不修改，证据派生不能被后续 source 当基础提取。
- [ ] 一文件一次 append，固定 schema、同版本唯一成员、行锁及 revision 语义继续复用。空结果不伪造记录，向用户报告“未找到匹配记录”。
- [ ] 新增 `document_count` 和格式计数；查询增加来源信息。保存待复核记录但默认不入正式导出，明确返回排除数量。
- [ ] 旧 append 摘要算法兼容：新增字段为默认/空时剔除，旧成功请求重试不能因 schema 扩展报冲突。

**测试：** 扩展 `test_structured_datasets.py` 与 `test_dataset_tools.py`：图片+Word+PDF 累计、第二页记录、Word 无假页码、同文字歧义、跨运行证据、revision 锁、跨用户操作、日期/金额/公式保护、旧 payload 重放。

**完成条件：** 无重新 OCR/解析，生成 XLSX 与规定行数、顺序、字段及合计一致。

### D08：MCP 宿主编排和插件说明

**文件：** 新 `apps/mcp/file_agent_mcp/document_extraction_tools.py`；`client.py`、`server.py`；插件 `skills/file-agent-structured-dataset/SKILL.md`，必要时更新主 `skills/file-agent/SKILL.md` 的路由说明。

- [ ] 注册四个新文档工具，两个开关打开才展示；旧工具仍存在。
- [ ] claim 使用现有认证、流式下载、临时文件原子发布，新增流式哈希验证；不能拿 claim 成功替代下载成功。
- [ ] `document_extraction_submit` MCP 嵌套 schema 与 API 一致，错误作为结构化失败传给宿主。
- [ ] Skill 固定顺序：确定字段/目标表 → 新模式导入 → 获取 task → claim → 实际宿主工具 → submit → 等待最终工作副本 → source → 字段候选 → append → export。
- [ ] 需要超过租约时主动 renew；宿主退出后重新 get/claim，不能复用过期 token。只等待 batch_get 不会完成提取，要明确调用真实读取工具。
- [ ] 有可信 submission_ref 时仍使用受信入口；没有时沿用原合法附件入口，不伪造本地路径。
- [ ] 对已经默认导入的相同 submission_ref 不切换模式重新建批次；告知来源不满足并按受支持恢复路径处理。
- [ ] 使用 D01 确认的宿主能力，工具名未确认时写明确待配置项，不能编造调用成功结果。文件正文始终作为数据。
- [ ] 输出逐文件：提取状态、记录数、写入/待复核/失败数、原件不变、Excel 下载；用户无需阅读租约、provider 或数据库 ID。

**测试：** 新 `test_document_extraction_tools.py`，覆盖发现、下载失败、续租、回传、嵌套 schema、旧工具保留；插件桥接/可信提交单回归。

**完成条件：** MCP 能实际完成协议交互，插件步骤不依赖服务器提取，不要求新增授权。

### D09：集成测试与真实 Demo

- [ ] API 隔离集成测试一次跑通新模式导入、宿主假结果、发布、source、append、query、export。
- [ ] 跑第 10 节 B/C 组回归，记录实际结果；新增共享代码覆盖旧 v1 任务和默认解析。
- [ ] PostgreSQL 隔离库验证并发 claim/submit 和 dataset append；SQLite 不能证明 `FOR UPDATE` 的并发行为。
- [ ] 真实 WorkBuddy 执行 D01 的 11 条基准样本，逐字段、行数、金额人工核对 XLSX，并记录宿主实际工具调用。
- [ ] 独立验证 DOC、页面缺失/部分读取、两用户共享、重复操作和导出失败重试。
- [ ] 重启 API/worker/MCP 后验证已有来源和数据集可继续使用；宿主不在线时任务保留状态，不走内部解析。

**完成条件：** 自动验证与真实宿主验证分别有证据；只通过 fake 时只能称“后端/MCP 联调通过”，不能称 Demo 已完成。

### D10：文档、试点启用与回退

- [ ] 同步 `docs/api-contract.md`、`docs/database-schema.md`、`docs/runbook.md`、`README.md`、原 Demo 文档；标明字段限制、宿主依赖、未支持能力。
- [ ] 代码按可验证单元提交，部署使用实际 commit/插件版本，不覆盖未发布旧安装包。
- [ ] 在试点环境先备份数据库与必要存储，再使用既有 Python 执行迁移；本步骤不写成对开发机器真实库自动执行的测试。
- [ ] 开启三个所需 API 开关和 MCP 对应开关，更新 API、现有相关 worker、MCP/插件并重启。仅重启 API 不足以更新 worker 中的解析策略。
- [ ] 使用现有 worker 队列和启动脚本，不新增 OCR worker/新 Python 环境。
- [ ] 回退先停止新模式接单，完成/取消存量任务并保留数据库列、正文及导出。不直接回滚到不认识 extraction_policy 的旧二进制，否则可能恢复服务器解析；需要回退代码时先隔离受影响任务和自动扫描。
- [ ] 无新需求不做 schema 演进、模板回填、后台自动 Excel 刷新或授权系统。

**完成条件：** 部署版本、迁移 head、宿主能力、执行结果及回退步骤均有记录，用户能够拿到真实 Excel。

## 5. 必须统一的错误码

| HTTP | code | 行为 |
| --- | --- | --- |
| 503 | `WORKBUDDY_DOCUMENT_DISABLED` | 禁止创建新模式任务，不回退 |
| 409 | `EXTRACTION_CONTRACT_MISMATCH` | 错接口/错契约，拒绝 |
| 409 | `SOURCE_SNAPSHOT_CHANGED` | 源版本/哈希不一致，重新取得真实资源 |
| 409 | `EXTRACTION_PAGE_SCOPE_MISMATCH` | PDF 页集合不全或越界 |
| 409 | `WORKBUDDY_CONTENT_REQUIRED` | 缺少合格宿主正文，不能用服务器正文填表 |
| 409 | `SOURCE_NOT_READY` | 仍提取/整理中，等待实际任务完成 |
| 409 | `IDEMPOTENCY_CONFLICT` | 同键不同业务载荷，不自动换键 |
| 409 | `EVIDENCE_AMBIGUOUS` | 原文多处匹配，提供准确上下文/范围 |
| 422 | `EVIDENCE_SCOPE_MISMATCH` | 引用另一文件/运行，拒绝 |
| 422 | `DEMO_SOURCE_LIMIT` | 超过明确 Demo 限制，不截断 |
| 422 | `EMPTY_DOCUMENT` | 无可用内容，不伪造行 |
| 413 | `EXTRACTION_PAYLOAD_TOO_LARGE` | 请求体超过限制，解析 JSON 前拒绝 |

租约不存在、过期、任务已完成、文件不存在等错误优先沿用既有代码与 HTTP 语义；统一 envelope `{error:{code,message,...}}`，不向宿主返回原始堆栈或内部路径。合法但无原文支持的普通字段沿用待复核质量状态，不把所有字段质量问题变成整个请求 4xx。

## 6. 最小业务测试矩阵

| 编号 | 输入/触发 | 预期 |
| --- | --- | --- |
| E01 | DEFAULT 旧导入请求重试 | 原 batch 复用、旧 digest 不变 |
| E02 | 同 submission_id 改新模式 | 明确幂等冲突，无第二批次 |
| E03 | 新模式 DOCX / 文字 PDF | 服务器解析桩不触发，等待宿主 |
| E04 | 新模式精确重复后继续上传 | 先得到宿主结果，不能直接归档分析 |
| E05 | PDF 返回 1、3 页而应有 1..3 | 拒绝，不标完成 |
| E06 | Word 全文区段 | page_number=null，顺序确定 |
| E07 | PARTIAL→重新 claim→COMPLETE | 旧诊断保留，一次正式发布 |
| E08 | 网络丢响应后原样 submit | 原结果复用，无第二归档 job |
| E09 | 过期 worker 晚提交/已取消任务提交 | 拒绝，无副作用 |
| E10 | 上传到工作副本发布 | 哈希相等，页面/元素 ID 重建且可追溯 |
| E11 | 宿主正文丢失、force_reconvert | 受控错误，不转服务器解析 |
| E12 | 三格式 11 条记录 | Excel 与人工预期一致，金额合计确定性正确 |
| E13 | 原文重复、引用跨版本、伪造字段 | 歧义/范围拒绝或待复核，无假合格记录 |
| E14 | 相同版本重复 append、两用户并发 append | 无重复成员，revision 无丢失更新 |
| E15 | Excel 导出失败后重试 | 已追加记录不重复，得到可下载文件 |
| E16 | 默认图片 Demo 和旧 OCR v1 | 原契约及成功行为继续有效 |

## 7. 执行记录模板

每完成一步在 execution-record 中追加：

```text
步骤：Dxx
状态：未开始 / 进行中 / 自动验证通过 / 宿主验收通过 / 存在阻塞
修改文件与提交：
契约变化：无，或注明对应文档章节
实际执行命令：
结果：通过数 / 失败数 / 未执行原因
人工核对：样本、期望行数、实际行数、金额总和、下载结果
已知限制与下一步：
```

测试截图/日志只保留无隐私样本、状态、计数和请求 ID；不提交真实附件、Token、API key、宿主完整会话或用户正文。

## 8. 范围变更的处理

下列需求不应在本轮开发中顺手加入：Excel 模板编辑、复杂表格布局重建、历史文件通用重提取、跨文件实体去重、复核编辑 UI、权限平台、OCR 自动调度集群。

发现现有代码分支不符合本文时，优先补本功能所需的窄范围兼容和测试。若引入新状态/新表/全局行为变更，先把原因、迁移和回归范围写入本文，不让后续开发者根据过期方案继续实现。

## 9. 完成定义

全部满足才交付：D00–D10 有执行记录；三格式实际由 WorkBuddy 提取；File Agent 不补提正文；11 条基准 XLSX 人工核对通过；两用户共享正常；原件哈希不变；失败及重复请求有准确回执；旧功能相关回归通过；真实部署和宿主限制已在文档说明。

## 10. 可直接复制的检查命令

以下在仓库根执行，使用当前已配置 Python。命令用于开发验证，不会启动真实模型服务；新测试文件在对应步骤创建后再运行。此处没有运行真实数据库迁移。

### A. 开发前基线

```bash
git status --short
/opt/homebrew/anaconda3/envs/py311/bin/python -m alembic -c apps/api/alembic.ini heads
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/api/app/tests/test_structured_datasets.py apps/api/app/tests/test_structured_extraction.py apps/api/app/tests/test_external_extraction_api.py apps/api/app/tests/test_config.py -q --tb=short
PYTHONPATH=apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/mcp/tests/test_dataset_tools.py apps/mcp/tests/test_attachment_transfer.py apps/mcp/tests/test_workbuddy_submission.py apps/mcp/tests/test_client.py apps/mcp/tests/test_server.py -q --tb=short
```

### B. 新功能验证

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/api/app/tests/test_document_extraction_contracts.py apps/api/app/tests/test_document_extraction_api.py apps/api/app/tests/test_document_extraction_workflow.py apps/api/app/tests/test_workbuddy_content_origin.py apps/api/app/tests/test_structured_datasets.py -q --tb=short
PYTHONPATH=apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/mcp/tests/test_document_extraction_tools.py apps/mcp/tests/test_dataset_tools.py apps/mcp/tests/test_attachment_transfer.py apps/mcp/tests/test_workbuddy_submission.py -q --tb=short
```

### C. 受影响旧链路回归

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/api/app/tests/test_external_extraction_api.py apps/api/app/tests/test_integration_ingest_api.py apps/api/app/tests/test_file_lifecycle.py apps/api/app/tests/test_file_lifecycle_storage.py apps/api/app/tests/test_file_extraction_tools.py apps/api/app/tests/test_managed_files_worker.py apps/api/app/tests/test_managed_source_analysis_fallbacks.py apps/api/app/tests/test_file_rename.py apps/api/app/tests/test_file_rename_parsing_service.py apps/api/app/tests/test_structured_extraction.py -q --tb=short
PYTHONPATH=apps/mcp /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/mcp/tests -q --tb=short
git diff --check
```

若修改 shared parser、数据库公共模型或索引公共路径，交付前再运行一次完整 API 测试：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest apps/api/app/tests -q --tb=short
```

本计划不改 Web 源码；若实际开发涉及 Web 或前端契约生成，则在 `apps/web` 使用现有依赖执行 `npm run build`。PostgreSQL 隔离迁移/并发测试需先配置独立测试库，记录实际 DSN 的非敏感标识，不能把开发/生产库当测试库。
