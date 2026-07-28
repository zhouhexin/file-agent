# 阶段七：共享工作副本内容更新与版本管理开发计划

- 状态：待用户评审；尚未开始代码实现
- 前置阶段：阶段六代码实现和本机自动化验收完成，目标 Windows 环境迁移与页面烟测通过后进入本阶段
- 直接产品目标：用户通过对话安全地更新共享文件内容、查看和比较历史版本，并在确认后恢复旧版本
- 上位规范：`agent.md`
- 关联方案：`docs/managed-original-working-copy-trash-implementation-plan.md`
- 前一阶段：`docs/stage-6-natural-language-correction-shared-file-organization-plan.md`

## 1. 阶段价值

阶段一至六已经建立：

```text
上传和原件归档
-> 唯一共享工作副本
-> 解析、摘要、分类和正文索引
-> 文件检索和证据回答
-> 自然语言纠正
-> 重命名、移动、删除和恢复
```

当前缺口是：共享工作副本内容一旦发生变化，系统还没有完整的版本提交、历史引用保护、重新解析和并发
控制闭环。用户无法可靠地完成：

```text
用刚上传的新版替换旧文件
把修订后的通知保存为新版本
查看这个文件的历史版本
比较当前版本和上一版
恢复到指定历史版本
撤销刚才的内容更新
```

阶段七必须把内容更新定义为追加式版本事件，而不是直接覆盖磁盘文件：

```text
用户选择更新来源和目标共享文件
-> 后端唯一解析身份
-> 生成内容更新 OperationPlan
-> 用户确认
-> 后台准备新版本及全部必需派生数据
-> 原子激活新版本
-> 保留旧版本和历史引用
-> 对所有用户展示新的当前版本
```

普通用户仍然不需要理解 WorkingCopy、DocumentVersion、Chunk、Tool、Skill、ChangeSet 或图数据库。

## 2. 当前实现基线与缺口

### 2.1 可以直接复用

- `WorkingCopy.current_version_id` 已经表达当前内容版本。
- `DocumentVersion` 已有 `version_number`、`parent_version_id`、`working_copy_id`、`sha256`、
  `operation_plan_id` 和 `created_by`。
- `DocumentExtractionRun`、`DocumentIndexRun`、`DocumentSummary`、`DocumentSearchProfile`、
  `DocumentCategorySuggestion` 和 Neo4j 投影已经按 `document_version_id` 保存派生事实。
- `AnswerReference` 已经绑定 `document_version_id` 和 `working_copy_id`，具备保护历史引用的基础。
- `OperationPlan`、`OperationConfirmation`、`ChangeSet`、`ChangeItem` 和文件 worker 已具备受控异步执行边界。
- 共享工作区、回收站、路径记录、原件保护和所有登录用户共享操作权限已经建立。

### 2.2 必须补齐

- 当前工作副本内容更新的正式操作类型和执行器。
- 不可变的工作副本历史版本内容存储，不能让多个版本继续指向同一个会被覆盖的可见路径。
- 新版本的准备、派生数据重建、激活、失败回滚和幂等重试状态。
- “刚上传的新文件”与“要被更新的共享文件”之间的明确身份绑定。
- 上传导入 worker 与“此附件只作为版本来源”之间的竞态处理，避免额外生成无意义共享副本。
- 版本历史查询、历史版本预览、确定性差异比较和恢复闭环。
- 当前版本变化后，分类、检索、回答引用和 Neo4j 投影的版本一致性。
- 多用户同时更新同一共享文件时的行锁、快照校验和过期计划处理。

## 3. 本阶段不可削弱的产品规则

### 3.1 内容变化才创建版本

- 替换正文、表格、PDF 或其他文件内容必须创建新的 `DocumentVersion`。
- 从历史版本恢复也必须创建新的 `DocumentVersion`。
- 重命名、移动、逻辑分类确认、删除到回收站和从回收站恢复不创建内容版本。
- 解析器升级、摘要重建和索引重建属于派生数据重处理，不创建内容版本。

### 3.2 版本只追加，不覆盖历史

- 已激活的历史版本不得修改内容、哈希、版本号和来源。
- 纠错必须创建新版本或新审计事件，不能回写旧版本。
- 版本号在一个 `Document` 内严格递增；不得复用已失败或已删除的版本号。
- 相同 SHA-256 也允许形成新的恢复版本，但必须明确记录来源和原因。

### 3.3 原件保护

- 受管原始目录中的文件不得被内容更新流程修改。
- 上传归档原件不得被覆盖、重命名、移动或删除。
- 新上传的版本来源同样先形成不可变归档事实。
- 工作副本内容更新不能反向同步到原件。
- `.doc`、`.xls` 等转换只产生临时或可追溯派生件，不能替换上传原件。

### 3.4 共享权限

- 所有登录用户都可以对 `ACTIVE` 共享工作副本发起内容更新、版本比较和历史查看。
- 内容更新和版本恢复影响所有用户，必须生成高风险 OperationPlan。
- 用户只能确认自己创建的 OperationPlan；其他用户可以针对同一共享文件重新发起计划。
- 私人上传暂存文件仍只属于上传者；其他用户不能把它作为版本来源。
- 用户会话、反馈、选择卡和计划详情继续按用户隔离。

### 3.5 当前版本与历史版本

- 普通列表、搜索、预览和新问题默认只使用 `WorkingCopy.current_version_id`。
- 历史版本只能通过明确的“查看历史”“比较版本”或历史引用入口读取。
- 历史版本不能重新进入普通全局检索结果。
- 回收站文件不能更新内容；必须先恢复。

### 3.6 正确性优先

- LLM 用量不作为阶段七首要限制，回答和版本差异准确性优先。
- 文件身份、版本号、哈希、页码、单元格差异和数字计算必须由确定性服务完成。
- LLM 可以把已经验证的差异组织成自然语言，不得判断哪个文件是目标版本，也不得直接操作文件。

## 4. 支持的用户场景

### 4.1 用上传附件更新共享文件

```text
用刚上传的新版申请表替换共享目录里的旧申请表。
把这个修订稿作为“2026年奖学金通知.docx”的新版本。
用附件更新刚才打开的文件。
```

必须唯一确定：

- 版本来源附件；
- 目标 `WorkingCopy`；
- 目标当前版本；
- 用户是否确认影响所有用户。

### 4.2 查看版本历史

```text
查看这个文件以前的版本。
这个文件修改过几次？
谁在什么时候更新了这份通知？
```

普通页面只展示业务信息：

- 版本序号；
- 更新时间；
- 操作人显示名；
- 文件大小；
- 更新说明；
- 当前版本标识；
- 是否完成正文和索引处理。

不得展示数据库 ID、绝对路径、完整哈希、Tool 或内部状态载荷。

### 4.3 比较版本

```text
比较当前版本和上一版。
新版申请金额和旧版有什么变化？
比较第一版和第三版的所有工作表。
```

比较必须优先使用确定性工具：

- TXT、MD、Word：标题、段落和表格差异；
- PDF：按页文本差异，必要时标记版面不可直接比较；
- Excel：工作表增删、表头变化、单元格变化和确定性金额差异；
- CSV：列、行和字段差异；
- 图片或无文本扫描件：仅在 OCR 结果可用时比较，并明确置信度。

LLM 只能总结差异工具返回的结构化结果。

### 4.4 恢复历史版本

```text
恢复到上一版。
把这个文件恢复到7月20日的版本。
撤销刚才的内容更新。
```

恢复不能直接把 `current_version_id` 指回旧记录。必须复制历史版本内容并创建新的追加版本：

```text
Version 1：初始版本
Version 2：用户更新
Version 3：从 Version 1 恢复产生的新版本
```

这样才能完整保留恢复人、恢复时间、恢复前版本和来源版本。

## 5. 规范身份解析

版本操作的目标身份固定为：

```text
canonical_version_target
= WorkingCopy.id
+ WorkingCopy.document_id
+ WorkingCopy.current_version_id
+ WorkingCopy.content_sha256
+ WorkingCopy.relative_path
```

其中：

- `working_copy_id` 是共享文件稳定身份。
- `document_id` 是版本、正文和派生数据所属文档身份。
- `current_version_id` 是计划生成时的当前版本快照。
- `content_sha256` 只用于完整性和并发校验，不能代替文件身份。
- `relative_path` 用于确认可见文件没有在计划期间被移动或重命名。

解析顺序：

1. 当前消息附件直接对应共享 `WorkingCopy.document_id` 时，按共享工作区解析唯一活动副本。
2. 附件是上传来源时，通过 `UploadArchiveRecord` 和 `managed_file_id` 解析归档事实。
3. “刚才打开的文件”只使用当前会话后端记录的稳定文件上下文。
4. 完整文件名只在共享活动工作副本中精确匹配。
5. 同名文件有多个时展示文件选择卡。
6. 没有精确匹配时可以返回相似文件选择卡，但不能自动选择。
7. 回收站精确命中时提示先恢复。
8. 不允许按“最新上传”“哈希相同”“当前版本号最大”替用户选择业务目标。

## 6. 版本来源附件与上传导入竞态

用户在聊天框选择文件时，上传任务可能已经开始查重、归档和导入。阶段七必须避免用户说“把这个作为旧文件
的新版本”时，该附件同时被导入成另一个独立共享工作副本。

### 6.1 新增版本来源保留记录

建议新增 `working_copy_version_source_reservations`：

```text
id
user_id
conversation_id
source_document_id
source_document_version_id
target_working_copy_id
target_version_id
operation_plan_id
status
created_at
expires_at
resolved_at
```

状态建议：

```text
RESERVED
WAITING_CONFIRMATION
CONSUMED
RELEASED
SUPERSEDED
EXPIRED
```

约束：

- 同一个上传版本同时只能作为一个未完成版本更新的来源。
- 保留记录不等于用户已经确认更新。
- 用户取消更新后，来源附件恢复普通上传处理或由用户明确取消。
- worker 创建独立工作副本前必须检查有效保留记录。

### 6.2 竞态处理

如果版本更新意图到达时：

- 独立工作副本尚未创建：暂停该来源的普通导入，把它保留为版本来源。
- 独立工作副本正在创建：等待当前原子步骤结束，再重新判断。
- 独立工作副本已经创建：不能静默删除；展示选择卡：
  - 仅将上传内容作为目标文件的新版本，并把多余副本移入回收站；
  - 同时保留独立文件，并另外用于目标文件新版本；
  - 取消版本更新。

上述选择必须进入同一个 OperationPlan，不能在后台自动清理共享文件。

## 7. 数据模型

### 7.1 复用 `DocumentVersion`

新内容版本继续使用现有 `document_versions`：

```text
document_id            固定为共享 WorkingCopy.document_id
working_copy_id        固定为目标共享工作副本
version_number         在当前 Document 内递增
parent_version_id      指向执行前的当前版本
storage_tier           WORKING_VERSION
storage_path           指向不可变版本内容
filename               激活时的业务文件名
content_type
size_bytes
sha256
source_type            USER_REPLACEMENT / VERSION_RESTORE
operation_plan_id
created_by
created_at
```

恢复版本的 `parent_version_id` 仍指向恢复前当前版本；具体从哪个历史版本恢复由版本事件表记录。

### 7.2 新增版本事件表

建议新增 `working_copy_version_events`：

```text
id
working_copy_id
document_id
from_version_id
to_version_id
source_document_id
source_document_version_id
restored_from_version_id
operation_type
operation_plan_id
operation_confirmation_id
agent_run_id
tool_invocation_id
changeset_id
status
error_code
error_message
created_by
created_at
prepared_at
activated_at
failed_at
```

状态建议：

```text
PLANNED
PREPARING
DERIVATIVES_BUILDING
READY_TO_ACTIVATE
ACTIVATING
ACTIVE
STALE
FAILED
SUPERSEDED
```

数据库约束：

- `operation_plan_id` 唯一，保证重复确认不能创建多个版本。
- 一个工作副本同时只能存在一个 `PREPARING`、`DERIVATIVES_BUILDING`、
  `READY_TO_ACTIVATE` 或 `ACTIVATING` 事件。
- `to_version_id` 唯一。
- `from_version_id != to_version_id`。
- 事件进入终态后不可修改业务快照，只允许追加诊断事件。

### 7.3 派生数据版本隔离

以下事实必须绑定 `to_version_id`，准备新版本时不能覆盖旧版本派生数据：

- `DocumentExtractionRun`
- `DocumentPage`
- `DocumentElement`
- `DocumentSummary`
- `DocumentChunk`
- `EvidenceSpan`
- `DocumentIndexRun`
- `DocumentSearchProfile`
- `DocumentClassificationRun`
- `DocumentCategorySuggestion`
- `DocumentArtifact`
- Neo4j `DocumentVersion` 节点和投影任务

## 8. 物理存储规则

### 8.1 可见工作副本与不可变版本内容分离

建议在现有 `WORKING_COPY_STORAGE_ROOT` 内使用受控隐藏目录：

```text
<shared-root>/
├─ 学院/行政管理/通知.docx              # 当前可见工作副本
└─ .internal/versions/
   └─ <working-copy-id>/
      ├─ <version-1-id>/content.docx
      ├─ <version-2-id>/content.docx
      └─ <version-3-id>/content.docx
```

规则：

- `DocumentVersion.storage_path` 指向不可变版本内容，不再让多个版本指向会被覆盖的可见路径。
- `WorkingCopy.relative_path` 只表示当前可见文件。
- `.internal` 永远不进入文件列表、检索、预览卡、MCP 文件系统 Tool 或普通下载目录。
- 历史版本下载只能通过后端鉴权接口读取版本内容。
- Windows 路径长度必须使用短 UUID 目录和现有长路径策略验证。

### 8.2 历史数据回填

迁移不能假设现有 `DocumentVersion.storage_path` 已经不可变。需要独立回填任务：

1. 锁定当前工作副本。
2. 校验可见文件 SHA-256 与当前版本一致。
3. 把当前内容原子复制到隐藏版本目录。
4. 校验目标哈希。
5. 更新当前版本的版本存储路径。
6. 写回填事件和审计。

回填失败时不得影响当前可见文件，不得阻断普通读取。

## 9. 两阶段版本提交

阶段七采用“准备后激活”，禁止确认后直接覆盖当前文件再慢慢重建索引。

### 9.1 Prepare

```text
确认 OperationPlan
-> 创建版本事件 PREPARING
-> 分配新 DocumentVersion
-> 复制来源内容到隐藏版本目录
-> 校验大小、MIME 和 SHA-256
-> 解析正文和表格
-> 生成摘要、Chunk、Evidence 和检索投影
-> 生成新版本分类建议
-> 状态 READY_TO_ACTIVATE
```

Prepare 期间：

- 旧版本仍是当前版本。
- 普通搜索和证据回答继续使用旧版本。
- 用户可以查看“新版正在准备”状态。
- 失败时当前文件完全不变。

### 9.2 Activate

激活必须在短事务和受控文件操作中完成：

1. 按稳定 ID 顺序锁定 `WorkingCopy`、版本事件和 OperationPlan。
2. 重新校验当前版本仍等于 `from_version_id`。
3. 重新校验当前路径、文件哈希和文件状态。
4. 校验新版本派生数据达到最低 READY 条件。
5. 把新版本内容复制到可见路径的同目录临时文件。
6. 校验临时文件哈希。
7. 使用原子替换切换可见文件。
8. 更新 `WorkingCopy.current_version_id`、`content_sha256`、`size_bytes` 和 `updated_at`。
9. 把版本事件标记为 `ACTIVE`。
10. 写 ChangeSet、ChangeItem 和图谱 Outbox。
11. 提交数据库事务。

如果文件系统提交成功但数据库提交失败，必须通过版本事件和磁盘提交标记执行 reconciliation，不能再次盲目
覆盖。具体补偿协议必须在实现前通过故障注入测试确定。

## 10. OperationPlan

新增或扩展操作类型：

```text
REPLACE_WORKING_COPY_CONTENT
RESTORE_WORKING_COPY_VERSION
```

计划必须由后端快照生成，至少保存：

```text
working_copy_id
from_version_id
from_sha256
from_relative_path
source_document_id / restored_from_version_id
source_version_id
source_sha256
source_size_bytes
source_content_type
shared_impact = true
managed_original_unchanged = true
historical_versions_preserved = true
```

普通确认卡只展示：

- 目标文件名；
- 当前版本时间和大小；
- 新版本来源文件名、时间和大小；
- 是否改变扩展名；
- 影响所有用户的提示；
- 旧版本会被保留；
- 原件不会改变；
- 确认后需要后台准备。

不能向前端返回绝对路径、完整哈希、数据库表名或执行器信息。

### 10.1 文件格式规则

第一批实现优先支持同扩展名内容更新：

```text
PDF -> PDF
DOCX -> DOCX
XLSX -> XLSX
TXT -> TXT
MD -> MD
CSV -> CSV
```

`.doc` 和 `.xls` 保留原格式作为版本内容，解析继续使用 LibreOffice 临时派生件。

跨扩展名更新必须单独提示：

- 新扩展名是否同时改变可见文件名；
- 浏览和解析能力是否支持；
- 是否存在同名目标冲突。

不得只替换内容却保留与真实格式不符的扩展名。

## 11. 并发、幂等和过期计划

### 11.1 并发更新

如果用户甲和用户乙同时基于 Version 2 创建更新计划：

```text
计划 A：Version 2 -> Version 3
计划 B：Version 2 -> Version 4
```

只允许第一个完成激活。第二个激活时发现当前版本不再是 Version 2，必须进入 `STALE` 并提示：

```text
文件已被其他用户更新，请查看最新版本后重新确认。
```

不得自动把第二个更新接到新版本之后。

### 11.2 幂等

- 同一个确认请求重复提交只能返回同一版本事件。
- worker 重复领取不得创建第二个 `DocumentVersion`。
- Prepare 重试应复用相同版本 ID 和隐藏存储路径。
- Activate 重试必须先核对当前版本和目标文件哈希。
- 已激活事件不得再次执行文件替换。

### 11.3 计划过期

以下任一事实变化都使计划失效：

- 当前版本变化；
- 当前工作副本进入回收站；
- 当前路径变化；
- 当前内容哈希变化；
- 来源附件被取消；
- 来源版本不再可用；
- 版本来源保留记录过期；
- taxonomy 或解析配置变化不使内容计划失效，但必须使用当前配置重新构建派生数据。

## 12. 解析、分类和检索重建

### 12.1 最低激活条件

在新版本成为当前版本前，至少满足：

- 版本内容存在且哈希正确；
- 基础风险检查完成；
- 支持格式已经完成确定性正文解析；
- `DocumentExtractionRun` 完成或明确为不可解析；
- `DocumentIndexRun` 对可解析文档完成；
- `DocumentSearchProfile` 已建立；
- 解析失败没有被伪装成成功。

Neo4j、embedding 和 LLM 摘要不作为激活硬依赖。

### 12.2 分类

- 旧版本正式分类关系继续绑定旧版本，仅作为历史事实。
- 新版本必须重新生成分类建议。
- 新版本不能自动继承旧版本 `CONFIRMED` 分类。
- 页面可提示“上一版分类”为弱参考，但用户必须基于新版本证据重新确认。
- 分类确认不会自动移动文件；仍按阶段六独立生成整理 OperationPlan。
- 如果新版本尚未确认分类，文件保持原物理位置。

### 12.3 检索

- Prepare 期间全局搜索继续使用旧当前版本。
- Activate 后检索只使用新 `current_version_id`。
- 旧 `DocumentSearchProfile`、Chunk 和 Evidence 保留，但不参与普通当前文件召回。
- 新版本索引异常时不允许回退到旧版本证据冒充当前内容。
- 用户明确查询历史版本时，使用独立历史版本范围，不混入普通结果。

## 13. 历史引用保护

`AnswerReference.document_version_id` 是不可变历史事实。

当 Version 1 已产生回答，文件后来更新为 Version 2：

- 历史回答仍引用 Version 1 的 EvidenceSpan。
- 页面显示“引用来自历史版本”。
- 点击引用读取 Version 1 的不可变版本内容。
- 不得把引用自动映射到 Version 2 的相似段落。
- 新问题默认只检索 Version 2。
- 用户明确要求“按上一版回答”时才读取 Version 1，并在答案中标明版本。

如果历史版本内容缺失或哈希不一致：

- 禁止展示伪造预览；
- 标记版本存储异常；
- 保留回答和引用审计；
- 进入版本存储 reconciliation。

## 14. Neo4j 与 GraphRAG 边界

新版本激活后，通过持久化 Outbox 完成：

```text
旧 DocumentVersion.is_active = false
新 DocumentVersion.is_active = true
新版本分类建议或正式分类投影
版本来源关系投影
```

建议关系：

```text
(newVersion)-[:PREVIOUS_VERSION]->(oldVersion)
(restoredVersion)-[:RESTORED_FROM]->(historicalVersion)
```

约束：

- PostgreSQL 始终是版本权威事实源。
- Neo4j 不可用不能阻断版本激活。
- Neo4j 失败必须进入 Outbox 重试和 reconciliation。
- 图关系必须携带稳定版本 ID 和来源，不能只按文件名关联。
- GraphRAG 不作为阶段七主问答路径。
- Neo4j 只作为关系扩展和候选召回；最终回答仍回到指定版本原文 Evidence。

## 15. Tool、Planner 和 LangGraph

### 15.1 新增受控 Tool

建议新增：

| Tool | 职责 | 副作用 | 是否需确认 |
|---|---|---:|---:|
| `working-copy-version-list` | 查询共享文件版本历史 | no | no |
| `working-copy-version-diff` | 确定性比较两个版本 | 可写差异派生件 | no |
| `working-copy-version-plan-create` | 创建内容替换或恢复计划 | yes | no |
| `working-copy-version-prepare` | 为已确认计划准备版本和派生数据 | yes | yes |
| `working-copy-version-activate` | 原子激活已准备版本 | yes | yes |
| `working-copy-version-reconcile` | 修复磁盘与数据库提交不一致 | yes | 管理员或系统任务 |

普通 Planner 不能直接调用 `prepare` 或 `activate`；它只能生成声明式计划，由确认后的异步执行链调用。

### 15.2 Planner 意图

建议新增：

```text
LIST_FILE_VERSIONS
COMPARE_FILE_VERSIONS
SUGGEST_FILE_VERSION_UPDATE
REPLACE_WORKING_COPY_CONTENT
RESTORE_WORKING_COPY_VERSION
```

Planner 只提取：

- 用户目标；
- 附件候选；
- 文件名或会话范围；
- 请求的版本时间或序号；
- 是否要求比较、更新或恢复。

真实版本 ID、目标工作副本、哈希和路径必须由后端服务解析。

### 15.3 AgentGraphState

State 只保存：

```text
working_copy_id
source_document_id
operation_plan_id
version_event_id
pending_job_ids
轻量状态和用户回复
```

不得保存文件正文、完整差异、数据库 Session、StorageService 或本地路径。

## 16. API 契约

主入口仍是：

```text
POST /api/conversations/{conversation_id}/messages
```

可以增加供前端卡片使用的受控接口：

```text
GET  /api/working-copies/{working_copy_id}/versions
GET  /api/working-copies/{working_copy_id}/versions/{version_id}
GET  /api/working-copies/{working_copy_id}/versions/{version_id}/preview
GET  /api/working-copies/{working_copy_id}/versions/{version_id}/download
POST /api/working-copies/{working_copy_id}/versions/compare
GET  /api/working-copy-version-events/{event_id}
```

所有接口必须：

- 要求登录；
- 验证共享工作区；
- 禁止读取私人上传暂存；
- 禁止返回物理路径；
- 历史版本预览明确标记非当前版本；
- 版本比较只接受后端验证属于同一工作副本的版本 ID。

## 17. 前端交互

### 17.1 内容更新确认卡

显示：

- 目标共享文件；
- 当前版本；
- 新来源附件；
- 内容大小和格式变化；
- “确认后影响所有用户”；
- “旧版本会保留，可恢复”；
- “原始归档文件不会改变”；
- 确认和取消按钮。

### 17.2 后台准备状态

确认后立即返回：

```text
新版已进入准备流程。当前文件在新版准备完成前仍保持不变。
```

状态卡只使用业务语言：

```text
正在准备新版
正在读取新版内容
正在建立检索索引
新版已启用
新版处理失败，当前文件未改变
```

不展示 worker、Job、Tool、Chunk、embedding 或内部错误堆栈。

### 17.3 版本历史卡

每项显示：

- 第几版；
- 更新时间；
- 更新人；
- 更新说明；
- 当前版本标签；
- 查看、比较、恢复入口。

历史版本卡不得与普通文件搜索结果混淆。

### 17.4 版本差异

- 默认先显示变化概览。
- Word/PDF 可以按章节或页展开。
- Excel 按工作表展示新增、删除、修改汇总。
- 数字差异必须显示确定性计算来源。
- 不展示数据库行号、内部 Chunk ID 或完整原文定位对象。

## 18. ChangeSet、审计和日志

建议新增 `change_type`：

```text
WORKING_COPY_VERSION_STAGED
WORKING_COPY_VERSION_ACTIVATED
WORKING_COPY_VERSION_RESTORED
WORKING_COPY_VERSION_PREPARE_FAILED
WORKING_COPY_VERSION_ACTIVATE_FAILED
VERSION_DERIVATIVES_REBUILT
VERSION_GRAPH_PROJECTION_ENQUEUED
```

每次更新至少记录：

- 操作人；
- 目标工作副本；
- 更新前后版本；
- 来源上传版本或恢复来源版本；
- 内容大小和哈希摘要；
- OperationPlan 和确认；
- 准备、激活和重建状态；
- 原件未改变；
- 对所有用户的共享影响。

日志不得记录文件正文、完整表格、JWT、API key、绝对物理路径或大段 LLM Prompt。

## 19. 故障恢复和 reconciliation

必须覆盖：

1. 隐藏版本文件存在但 `DocumentVersion` 不存在。
2. `DocumentVersion` 存在但版本文件缺失。
3. 新版本派生数据完成但事件仍停留在准备状态。
4. 可见文件已替换但 `WorkingCopy.current_version_id` 未更新。
5. 数据库已切换但可见文件哈希仍为旧内容。
6. 当前版本正确但 Neo4j 仍把旧版本标记为活动。
7. 版本来源保留记录过期但普通导入尚未恢复。

reconciliation 只能根据持久化版本事件、哈希和提交标记修复，不得按文件名猜测。无法唯一判断时进入人工处理，
不能自动覆盖当前文件。

## 20. 开发任务顺序

### 任务 0：阶段六验收门

1. 在目标 Windows 环境执行阶段六数据库迁移。
2. 完成共享分类、整理、冲突、删除和恢复页面烟测。
3. 确认当前分支和远端提交一致。
4. 备份开发数据库和共享工作目录。

### 任务 1：版本存储与迁移

1. 定义隐藏不可变版本目录。
2. 增加版本事件和来源保留表。
3. 增加数据库约束和索引。
4. 编写现有当前版本内容回填任务。
5. 增加路径逃逸、符号链接和 Windows 长路径测试。

### 任务 2：版本身份与查询

1. 实现共享版本目标解析器。
2. 实现版本历史查询。
3. 实现历史版本鉴权预览和下载。
4. 实现历史引用状态投影。

### 任务 3：版本来源保留

1. 在对话识别更新意图后保留来源附件。
2. 修改普通导入 worker，尊重有效保留记录。
3. 处理已生成独立共享副本的竞态选择卡。
4. 实现取消、过期和恢复普通导入。

### 任务 4：OperationPlan

1. 实现内容替换和恢复计划。
2. 固化工作副本、版本、路径、来源和哈希快照。
3. 增加共享影响确认卡。
4. 实现重复确认幂等。

### 任务 5：Prepare

1. 创建版本事件和新 DocumentVersion。
2. 写入隐藏不可变版本内容。
3. 执行风险检查和确定性解析。
4. 重建摘要、Chunk、Evidence、索引和分类建议。
5. 失败时保留当前版本。

### 任务 6：Activate

1. 锁定工作副本和版本事件。
2. 复核计划快照。
3. 原子切换可见内容。
4. 更新当前版本缓存。
5. 写 ChangeSet 和图谱 Outbox。
6. 实现中断和补偿测试。

### 任务 7：版本比较与恢复

1. 实现文本、Word、PDF 和表格确定性差异。
2. 实现差异普通用户投影。
3. 实现追加式恢复版本。
4. 保护旧 AnswerReference。

### 任务 8：前端

1. 内容更新确认卡。
2. 后台准备状态卡。
3. 版本历史卡和历史预览。
4. 版本比较卡。
5. 版本恢复确认。

### 任务 9：验收和文档

1. 完整后端测试。
2. 前端生产构建。
3. PostgreSQL 真实迁移。
4. Windows 页面烟测。
5. 更新 API、数据库、运行、手工烟测和阶段文档。

## 21. 测试要求

### 21.1 后端

至少覆盖：

1. 所有登录用户都能对活动共享工作副本创建内容更新计划。
2. 用户不能使用其他用户私人上传暂存作为版本来源。
3. 其他用户不能查看或确认不属于自己的 OperationPlan。
4. 未确认计划时当前内容和版本不变。
5. Prepare 失败时当前内容和版本不变。
6. Activate 成功后只增加一个 DocumentVersion。
7. 重复确认和 worker 重试不重复创建版本。
8. 新版本的父版本等于执行前当前版本。
9. 恢复历史版本创建追加版本，不回退版本号。
10. 重命名和移动不创建内容版本。
11. 回收站文件不能更新，恢复后可以重新发起。
12. 同名多个文件必须选择，不能自动挑选。
13. 两个用户并发更新时第二个计划变为 STALE。
14. 来源文件 SHA-256 变化时停止执行。
15. 当前路径变化时计划失效。
16. 新版本激活前普通搜索继续使用旧版本。
17. 激活后普通搜索只使用新版本。
18. 新索引失败时不使用旧证据冒充新版本。
19. 历史回答继续绑定旧版本 EvidenceSpan。
20. 历史版本不进入普通全局检索。
21. 新版本重新生成分类建议，旧正式分类不自动复制。
22. Neo4j 不可用不阻断版本激活。
23. 图谱 Outbox 重试最终只保留一个活动版本投影。
24. 文件系统和数据库各故障点均有补偿或人工处理状态。
25. 受管原件和上传归档原件 SHA-256 始终不变。

### 21.2 前端对话示例

```text
用刚上传的文件更新“2026年奖学金通知.docx”。
把这个附件作为刚才那个申请表的新版本。
查看“2026年奖学金通知.docx”的历史版本。
比较当前版本和上一版。
比较这两个版本中所有工作表的资助金额。
恢复到7月20日的版本。
撤销刚才的内容更新。
```

验收观察：

- 多文件歧义时出现文件选择卡。
- 内容更新前出现共享影响确认卡。
- 未确认前文件内容没有变化。
- 确认后先显示“正在准备新版”，当前文件仍可正常读取。
- 准备成功后当前版本切换。
- 历史版本可查看但明确标记为历史。
- 恢复操作创建更高版本号。
- 页面不出现 Skill、Tool、WorkingCopy、DocumentVersion、数据库 ID 或物理路径。

### 21.3 数据库核对

每个成功版本更新至少核对：

```text
document_versions              新增一条版本
working_copy_version_events    一条 ACTIVE 事件
working_copies                 current_version_id 指向新版本
operation_plans                EXECUTED 或异步完成终态
operation_confirmations        一条当前用户确认
change_sets / change_items     完整版本变化审计
document_extraction_runs       绑定新版本
document_index_runs            绑定新版本
document_search_profiles       绑定新版本
answer_references              历史记录仍绑定原版本
```

不得只检查前端提示而忽略数据库和物理文件状态。

### 21.4 Neo4j 核对

启用 Neo4j 时核对：

- 新旧 `DocumentVersion` 节点都存在。
- 只有新版本为活动版本。
- `PREVIOUS_VERSION` 或 `RESTORED_FROM` 方向正确。
- 正式分类关系不会从旧版本误复制到新版本。
- 图谱失败时 PostgreSQL 版本事实仍然正确。

### 21.5 Windows

必须在 Windows 开发环境验证：

- `start-file-agent-workers.cmd` 能消费版本任务。
- 中文、空格、长文件名和受限字符处理正确。
- 同目录临时文件和原子替换可用。
- LibreOffice `.doc` / `.xls` 派生解析可用。
- 文件被预览程序占用时返回明确可重试错误，不破坏当前版本。
- API、worker 和前端重启后版本事件能够继续处理。

## 22. 完成标准

阶段七只有在以下全部满足时才能标记完成：

- 用户可以通过对话唯一选择目标共享文件和版本来源。
- 所有登录用户都能在确认后更新共享工作副本。
- 私人上传暂存、私人会话和计划详情仍保持隔离。
- 内容更新和恢复都创建追加式 DocumentVersion。
- 旧版本内容可验证、可查看、可比较。
- 历史回答引用不会漂移到新版本。
- 新版本激活前当前文件不变。
- 新版本激活后搜索、预览、摘要和证据都基于新版本。
- 旧正式分类不会未经确认自动继承到新版本。
- 并发、重试和进程中断不会产生重复或半激活版本。
- 受管原件和上传归档原件不变。
- PostgreSQL、物理文件和 Neo4j 投影可以 reconciliation。
- 普通页面不暴露内部架构信息。
- 后端测试全部通过，前端构建成功。
- 目标 Windows PostgreSQL 迁移和前端页面烟测通过。

## 23. 非目标与后续阶段

阶段七不实现：

- OnlyOffice 或浏览器内完整 Office 编辑器；
- 多人实时协同编辑和单元格级锁；
- 自动永久清理历史版本；
- 未确认的自动内容覆盖；
- 把受管原件变化自动覆盖到工作副本；
- GraphRAG 作为主问答路径；
- Graphiti 用户时间记忆；
- 自动 Skill 发布；
- MinIO/S3 存储迁移；
- Redis/Celery 全面替换当前任务队列。

阶段七完成后再评估阶段八。阶段八优先方向为实时目录监听、定时同步、worker 健康和任务队列可靠性；
GraphRAG、Graphiti、对象存储和 Skill 自动演化必须分别立项，不能与版本管理混成一次高风险改造。
