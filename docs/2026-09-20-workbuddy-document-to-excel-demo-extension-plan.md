# WorkBuddy 图片、Word、PDF 内容抽取到 Excel：Demo 扩展方案

日期：2026-09-20
状态：方案，尚未实现。按用户最新要求修订：**文件内容提取必须在 WorkBuddy 中完成**。

执行开发时按[可执行开发计划 D00–D10](2026-09-20-workbuddy-document-to-excel-development-plan.md)推进。
该计划已冻结接口名称、迁移、重试状态、兼容规则、测试命令和验收样本；本文保留需求及总体设计。

## 1. 需求与职责

用户在 WorkBuddy 上传图片、Word（`.docx/.doc`）或 PDF，说明所需字段，WorkBuddy 读取文件内容、识别文字和表格、整理字段候选，File Agent 接收结果后保存、校验、追加到累计表格并生成 Excel。

用户所说的“OCR/提取文件内容”包含图片和扫描 PDF 的文字识别，也包含 Word、文字 PDF 的正文与表格读取。后三者不必强制转换为图片再 OCR，但执行位置都在 WorkBuddy 宿主及其可调用工具中。

| 工作 | 负责方 |
| --- | --- |
| 图片 OCR，扫描 PDF OCR | WorkBuddy |
| Word、文字 PDF 的正文与表格提取 | WorkBuddy |
| 必要的文件转换、页面渲染及读取方式选择 | WorkBuddy 的真实可用工具；能力不足明确报错 |
| 按用户字段生成记录候选及原文引用 | WorkBuddy |
| 保存上传原件、分配稳定版本、提供受控资源 | File Agent |
| 校验回传版本、哈希、结构、覆盖声明与字段引用 | File Agent |
| 保存正文、元素、字段结果和审计记录 | File Agent |
| 复用回传正文进行既有分类、整理和索引 | File Agent |
| 字段类型规范化、确定性合计、幂等追加、生成 XLSX | File Agent |

本次新增通道不调用 File Agent 的 Word/PDF 正文解析器或内部 OCR 来完成、补充或替代 WorkBuddy 的内容提取。已有其他通道的解析能力保留，原有上传、OCR、分类、检索和文件操作行为保持兼容。

其余需求继续生效：

- 一个文件可提取多行，多种格式可以汇总到同一张累计表。
- 所有登录用户共同操作共享工作副本和累计表，不新增 owner 限制、授权管理或额外授权页面。
- 先实现小 Demo，优先复用已经完成的功能。
- 原件不变，提取内容和 Excel 均可追溯到固定文件版本。

“填写 Excel”先沿用当前 Demo：新建累计表或追加到已有累计表，再导出 XLSX；任意 Excel 模板的指定 Sheet/单元格回填另作后续扩展。

## 2. 当前实现差距与复用判断

此前版本将 File Agent 的 Word/PDF 解析结果作为新功能的内容来源，职责不符合最新要求，本修订替代该设计。

| 当前实现 | 核对位置 | 修订方向 |
| --- | --- | --- |
| 外部 OCR 只接受图片/PDF，不接受 Word | `external_extraction/service.py:prepare_for_ingest_item` | 新增显式文档提取契约，覆盖图片、Word、PDF |
| PDF 有文字的页面不发给 WorkBuddy；回传时由服务器提取并合并原生文字 | `_build_page_manifest`、`_merge_native_and_external_pages` | 新契约交给 WorkBuddy 读取整个文件，不沿用该正文合并逻辑 |
| 原 claim 响应只提供图片页面资源，submit 要求固定整数页号 | `external_extraction/schemas.py` | 新契约提供固定版本的原文件资源，接收真实页或 Word 内容区段 |
| 工作副本克隆只识别 `workbuddy-external-ocr`，当前主要复制页面 | `file_lifecycle/service.py:_clone_upload_extraction_for_working_copy` | 兼容新 provider，并复制正文、元素及来源映射，阻止再次内部提取 |
| 数据集 source 只接受单张图片，且字段页码只能为 1 | `structured_datasets/service.py`、`schemas.py`、MCP `dataset_tools.py` | 支持宿主回传的多格式全文与证据引用 |
| 已有任务租约、版本与哈希校验、幂等回传 | `external_extraction` | 提取公共逻辑复用，不另建任务平台 |
| 已有正文、元素、结构化字段和累计表模型 | `DocumentExtractionRun/Page/Element`、`StructuredExtractionRun/Field`、`StructuredDataset*` | 继续使用，不为每种格式重复建表 |
| 已有字段规范化、查询、合计与安全 XLSX 输出 | `structured_extraction`、`structured_datasets` | 继续复用 |

结论：这次需要补“宿主完整文档提取回传”这一环节，不能仅放开数据集 source 的扩展名。新增契约只实现 Demo 所需的原文件领取和正文回传，不要求实现早期完整方案中的复杂 OCR 调度、schema 演进或自动刷新。

## 3. 最小业务流程

```text
用户在 WorkBuddy 上传附件并描述目标字段
  -> 复用附件导入，保存原件及固定版本
  -> 该任务选择 WorkBuddy 完整文档提取模式
  -> WorkBuddy 领取任务，取得受控原文件资源并核对哈希
  -> WorkBuddy 调用宿主工具提取正文、表格及可用位置
  -> 回传完整提取内容、覆盖状态和来源版本
  -> File Agent 校验并保存正文和元素
  -> 复用回传正文完成现有查重、分类、归档、工作副本发布及索引
  -> WorkBuddy 取得最终工作副本和已落库的来源引用
  -> WorkBuddy 按累计表字段提交记录候选及证据
  -> File Agent 规范化、幂等追加并导出 Excel
  -> 返回真实 Excel 下载入口和逐文件回执
```

提取正文与追加表格分为两次提交，复用现有 ingest 与 dataset 边界。WorkBuddy 读取落库 source 是取得正式证据 ID、核对固定快照，不代表服务器重新提取内容。

用户只需提出一次任务。Skill 负责衔接领取、宿主提取、回传、等待整理和追加，不把同一抽取指令再通过 `IngestRequestRunner` 执行一遍。宿主没有读取能力、提取失败或任务尚未完成时，返回具体状态，不静默调用服务器解析兜底。

## 4. 新增通道设计

### 4.1 在导入时固定提取模式

为新 Demo 增加可选模式 `extraction_profile=WORKBUDDY_DOCUMENT`，由累计表格 Skill 在附件导入时设置。未传参数时保持原行为。模式连同清单和策略保存到 `IngestBatch.policy_json`，进入请求指纹；批次封存后不能更改。

该模式必须在第一次可能读取正文的查重/解析阶段之前生效，不能等服务器解析完成后才让 WorkBuddy 再读一次。文件类型、大小、哈希、容器页数等非正文元数据检查可留在后端；后端不调用 `get_text`、Word 段落遍历或内部 OCR 生成业务正文。

新通道未启用或客户端不支持时明确返回能力缺失，不降级成服务器提取。保留既有登录和任务租约；租约用于任务并发与提交绑定，不新增数据集所有者权限。

精确重复文件分支也必须遵循该模式：可以复用同一哈希且有完整 WorkBuddy 来源证明的已验收正文；只有服务器解析结果时不能冒充宿主提取。选择继续导入的分支仍需进入宿主提取，选择已有文件的分支在获得合格宿主来源前不能自动追加。原有重复文件处理行为不全局改写。

首轮完整闭环以本轮新上传附件为验收入口。历史工作副本已有完整 WorkBuddy 来源时可复用；只有服务器正文的历史文件暂不进入新模式的自动填表，后续可增加针对稳定工作副本的宿主重提取入口。现有图片 Demo 兼容路径继续保留，不能将历史能力限制误报为新功能完成。

### 4.2 最小版本化契约

保留原 `extraction_claim/renew/submit` 及 `workbuddy-ocr-v1` 行为。新增文档提取契约，例如 `workbuddy-document-v1`，通过独立的 `document_extraction_claim/submit` 工具暴露；内部复用租约、幂等和任务生命周期。续租可复用通用接口，实际路由和名称在实现时与 API 文档同步。

claim 返回：

- 固定 `task_id`、`source_version_id`、`source_sha256`。
- 文件类型、大小、受控下载资源和任务租约。
- 提取范围为完整文件、结果体积上限，以及已知的非正文范围信息，例如 PDF 页数。

MCP 沿用现有受控缓存下载方式。宿主也可复用同哈希的本轮附件缓存，但不能用附件显示名称推断是同一文件。普通用户界面不暴露服务器绝对路径。

submit 回传内容示意：

```json
{
  "task_id": "稳定任务 ID",
  "source_version_id": "源版本 UUID",
  "source_sha256": "源文件哈希",
  "submission_key": "稳定重试键",
  "provider_name": "实际宿主提取工具名",
  "coverage": {
    "status": "COMPLETE",
    "unit_count": 1,
    "warnings": []
  },
  "units": [
    {
      "unit_id": "宿主本次结果内唯一 ID",
      "kind": "TEXT_SECTION",
      "page_number": null,
      "order": 0,
      "text": "实际提取的完整正文区段",
      "blocks": []
    }
  ]
}
```

示意省略了必需的 worker/租约字段；正式 schema 必须包含并校验。`blocks` 在正式 schema 中使用有界的强类型结构，不沿用任意字典来接收可执行动作、路径或无限嵌套数据。

- 图片使用单页结果，PDF 按真实页面提交，每页明确成功、空白或失败，不漏掉文字页。
- Word 使用有序正文区段、段落或表格块；只有实际工具返回可靠物理页码时才填写页码，不用内部编号伪造 Word 第几页。
- 表格若有真实行列、合并单元格或坐标信息则保存；没有时保留完整文本及顺序，不补造坐标。
- DOC 若需转换，在 WorkBuddy 可调用工具中完成，并保留原 DOC 哈希及转换来源；没有转换能力则明确失败，不调用服务器 LibreOffice 兜底。
- PDF 页内文字和图片、Word 内嵌图片的覆盖由真实宿主能力决定。读取工具未覆盖时标记 `PARTIAL` 或能力限制，不能把仅文字层结果声称为完整识别。

### 4.3 验收、持久化及可信度边界

File Agent 校验租约、版本、源哈希、单位 ID 唯一性、顺序、体积、页范围和幂等摘要，再保存到既有 `DocumentExtractionRun/Page/Element`。provider 明确记录为宿主文档提取，例如 `workbuddy-external-document`，同时保存真实工具名、契约版本及覆盖说明。

校验回传结构和字段受正文支持，不等于独立证明 OCR 与原文件逐字一致。服务器不重新 OCR 或读取正文复核；Word 的完整性尤其依赖宿主提取工具的结果与覆盖声明。不能仅凭哈希正确就宣称识别内容完全正确。

`coverage=COMPLETE` 仅表示宿主报告支持范围内的读取完成，仍须通过缺页、已知错误和结果结构检查；`PARTIAL/FAILED` 保留诊断，但首轮不自动向正式累计表追加残缺文件。缺乏空白页依据时不能猜为空白。

复用现有 `ExternalExtractionTask` 保存任务状态、租约与提交指纹，使用明确的契约版本分派。`ExternalExtractionPage` 只表达有真实页号的结果；Word 区段进入 `DocumentPage/Element`，不能假装成 PDF 页面。执行计划确定增加独立迁移：task 增加原文件 manifest 和结果摘要 JSON，DocumentVersion 增加提取策略 JSON，以确保正文丢失、工作副本分析和重处理时也不能回落服务器提取；不修改已应用迁移。

重试首轮按整个文件提交；相同键同内容复用结果，不同内容冲突。无需在 Demo 中增加 Word 分段租约和跨会话自动续跑平台。

### 4.4 从上传版本衔接到工作副本

复用既有外部 OCR 发布衔接，扩展受控 provider 白名单并传递正文、元素及质量信息。源上传版本与目标工作副本必须有一致的内容哈希。

克隆时重新生成页面和元素 ID，保留来源映射；不能把上传版本的证据 ID 直接当作工作副本的证据 ID。dataset source 返回最终工作副本下的正式 ID，append 只接受这些 ID。

正式分析阶段必须复用已验收的宿主正文。检查解析结果选择、缓存匹配和失败重试逻辑，防止 provider 不匹配导致隐式重跑服务器 Word/PDF 解析。新来源仅参与既有分类、整理和索引等后续处理，不自动改变其他通道的优先级。

## 5. 累计表格与字段证据

沿用当前 7 个 `structured_dataset_*` 工具，不为三种格式分别创建数据集工具。

source 的职责调整为读取完整、已验收的 WorkBuddy 正文和正式引用。新模式只选择来源明确的宿主解析运行；不能随意取“最新 COMPLETED”而混入服务器生成的正文。保持 GET 只读，不触发解析或 OCR。

API 与 MCP 同步扩展字段候选：保留 `raw_text`、`confidence`，增加 `evidence_refs`，引用正式来源区段 ID 和原文 quote。后端在指定区段定位原文并保存字符范围；多处匹配时需要更完整上下文，不能任意选第一次。旧图片请求的 `page_number=1` 仍兼容。

字段校验继续复用 `finalize_candidates`，不触发图片 Provider。来源证据适配支持 Word 空页码及 PDF 多页，不强制使用只接受整数页码的图片 `LayoutPage`。证据必须属于固定版本和运行；无法定位、类型不符、必填缺失或关联不清的记录保留待复核，不编造字段，也不强填高置信度。

一份文件的全部记录一次 append；不能每页追加一次而触发现有同版本唯一成员约束。仍使用现有成员表、行锁、幂等键、revision、ChangeSet 和审计事务。文件之间独立成功或失败，已成功追加不因其他文件失败而回滚。

Excel 继续由 File Agent 使用现有安全 writer 生成，不由 WorkBuddy 直接写服务器文件。默认输出合格记录，回执显示候选数、写入数、待复核/排除数及原因；导出失败只重试 export，不重复追加。查询与合计由后端确定性计算。

保留当前 Demo 上限：20 个字段、每文件 100 条记录、每表 1000 条、单文件回传正文 100,000 字符；blocks 另设有界数量和字节限制。超限明确返回错误，不截断后声称完整。数据集新增 `document_count` 和格式计数，`image_count` 继续表示实际图片数。

## 6. 实施顺序与修改范围

1. **宿主能力与样本验证**：先确认真实 WorkBuddy 工具可以读取图片、DOCX/DOC、文字/扫描 PDF，取得真实文本及位置。不能仅编写 Skill 指令就宣称宿主具备这些能力。
2. **导入模式与回传接口**：扩展附件传输、批次 schema/策略、新文档 claim/submit；复用当前任务、受控资源、租约、幂等和正文仓库。
3. **后续工作流复用**：补充工作副本克隆、来源 ID 映射和解析缓存选择，保证整理/索引使用回传正文，缺少宿主结果时不触发内部提取。
4. **多格式累计表**：扩展 source、字段引用、证据派生和计数，继续复用追加、查询和导出。
5. **Skill 衔接与验收**：一次任务从上传推进到真实 XLSX，逐文件报告提取、整理、追加和导出状态。

预计涉及：

- `apps/mcp/file_agent_mcp/attachment_transfer.py`、`server.py`、`client.py`：可选提取模式、原文件资源及新回传工具。
- `apps/api/app/modules/ingestion/`：冻结策略、任务模式与原附加请求兼容。
- `apps/api/app/modules/external_extraction/` 及集成 API：版本化文档提取契约与公共租约逻辑。
- `apps/api/app/modules/file_lifecycle/service.py`、必要的解析结果复用分支：发布衔接、元素映射、防止重复内部解析。
- `apps/api/app/modules/structured_datasets/`、`structured_extraction/repository.py`、MCP `dataset_tools.py`：多格式来源、证据和累计表兼容。
- `integrations/workbuddy/file-agent-plugin/skills/file-agent-structured-dataset/SKILL.md`：真实宿主提取及回传步骤。
- 对应 API/MCP 测试，实现后同步 API 契约、数据库说明（若有变更）、runbook 和 Demo 文档。

不重建 OCR 平台，不引入新权限系统或新前端，不全局禁用旧插件入口。新增写入继续通过白名单 Tool、AgentRun、ToolInvocation 和 ChangeSet。

## 7. 验收要求

自动测试使用确定性 fixtures/fake；真实 WorkBuddy 能力单独烟测。

| 场景 | 验收标准 |
| --- | --- |
| 三类附件 | 图片、Word、PDF 内容均由宿主工具提取，具有真实 provider 来源 |
| 禁止服务器兜底 | 新模式下将服务器 Word/PDF 正文解析及 OCR 调用替换为抛错桩；成功链路不触发它们，宿主失败也不能触发 |
| 原生/扫描/混合 PDF | 所有页交由宿主处理，保留真实页码；没有服务器原生文字合并 |
| Word 和 DOC 转换 | 段落与简单表格可入表；页码不虚构；缺少宿主 DOC 能力明确失败 |
| 页内图片、内嵌图片 | 覆盖不足有明确状态，不将文字层读取等同于全文件读取 |
| 版本与发布 | 源哈希绑定正确，上传与工作副本 ID 映射正确，正文只提取一次 |
| 查重分支 | 新模式不会在精确/近似重复处理分支偷偷使用服务器正文代替宿主正文 |
| 字段与多行 | 不同格式累计到同一表格，行数、字段、数值及证据可核对 |
| 重试、部分失败 | 不重复计数，失败文件单独报告，导出失败可独立重试 |
| 共享访问 | 不同登录用户可继续操作共享累计表和合格工作副本，无 owner 授权步骤 |
| 旧功能回归 | 旧 OCR 契约、原图片 Demo、导入、分类、检索与受控文件操作继续工作 |
| 原件与真实交付 | 原件哈希不变，XLSX 可下载并与人工预期逐项一致 |

真实样本至少包含图片、带段落和简单表格的 Word、多页文字 PDF、扫描 PDF、文字页/扫描页混合 PDF。验收记录中明确区分“自动测试通过”“宿主实际提取成功”“Excel 人工核对通过”。

后续再扩展任意 Excel 模板回填、复杂表格、长文件分批处理、历史工作副本宿主重提取、待复核结果编辑与跨文件业务去重。

本次仅修订方案，没有修改应用代码、启用新通道或执行部署迁移。
