# 阶段六：自然语言纠正与共享文件整理开发计划

- 状态：阶段六代码实现完成；本机自动化验收通过，待目标 Windows 环境执行数据库迁移和页面烟测
- 前置阶段：阶段五真实 PostgreSQL 迁移、活动文件 `document-chunk-index-v2` 重建和前端验收通过
- 直接产品目标：用户只通过自然语言确认或纠正文件分类、修正文件名，并在明确确认后整理唯一共享工作目录
- 上位规范：`agent.md`
- 总体方案：`docs/automatic-organization-conversational-access-implementation-plan.md`

## 1. 阶段价值

阶段一至五已经完成或建立了以下闭环：

```text
上传
-> 原件归档
-> 共享工作副本
-> 解析、摘要、分类建议和索引
-> 文件检索
-> 原文证据回答
```

当前缺口是系统可以生成分类和文件名建议，但用户对建议的接受、拒绝、纠正，以及确认分类后的共享目录
整理仍没有形成统一产品语义。阶段六要补齐：

```text
系统生成建议
-> 用户用自然语言接受、拒绝或纠正
-> 后端固化可审计的逻辑分类
-> 文件仍保持原位置
-> 用户明确要求按分类整理
-> 系统生成共享目录 OperationPlan
-> 用户确认后才移动工作副本
```

普通用户仍不需要理解 taxonomy、Skill、Tool、WorkingCopy、ChangeSet 或数据库表。

### 1.1 当前实现基线

阶段六不是从零开发，必须在以下现有事实之上增量改造，禁止另建平行链路：

- 已有 `DocumentCategorySuggestion`、`DocumentCategoryFeedback`、分类反馈 API 和前端“正确/错误/更正”
  入口，但当前反馈服务主要记录建议反馈，尚未原子写入正式分类关系、ChangeSet 和图谱投影任务。
- 已有唯一共享系统工作区、`WorkingCopy`、`WorkingCopyRoot`、`UploadArchiveRecord`、回收站、
  `WorkingCopyPathRecord` 和工作副本 OperationPlan 执行器。
- 已有重命名、移入回收站、恢复和同名冲突的部分对话闭环；已有底层
  `MOVE_WORKING_COPIES`，但尚未完成“按已确认分类解析安全目标目录”的对话服务。
- 当前部分检索和工作副本操作仍通过 `Document.user_id` 限制可见范围。这与“所有用户共享活动工作副本”
  的阶段六目标不完全一致，必须通过统一访问策略改造，不能在各查询中零散删除用户条件。
- 已有 `GraphProjectionRun` 和 Neo4j 投影能力，但正式分类提交与图谱投影之间还缺少不会丢失的持久化
  异步出口。

阶段六开发时必须先形成“复用、改造、新增”清单。已有入口只能收敛到新的统一服务，不能让旧按钮、
自然语言纠正和管理端分别维护不同事务规则。

## 2. 核心概念和不可混淆的三层状态

### 2.1 分类建议

分类建议来自规则、正文证据、受控 LLM 或图谱候选，状态为 `SUGGESTED` 或 `NEEDS_REVIEW`。

- 可以用于展示、搜索候选和请用户确认。
- 不能冒充用户已确认分类。
- 不能自动移动文件。
- 不能仅凭父目录或文件名升级为正式分类。

### 2.2 已确认逻辑分类

用户明确接受或纠正后，系统应写入正式逻辑分类关系。

- 必须绑定稳定 `document_id`、当前 `document_version_id`、taxonomy ID 和版本。
- 必须记录确认用户、来源建议、证据、确认时间和历史替代关系。
- 接受、拒绝、纠正都必须保留追加式反馈审计。
- 已确认分类用于所有用户共享文件的客观分类展示和检索，但个人反馈、会话和确认人身份仍按用户隔离。
- 用户沉默、打开、下载或继续对话不能推断为接受。

### 2.3 共享工作副本的物理位置

所有用户共用唯一共享工作目录；用户上传文件和外部受管文件都只导入一份工作副本。

- 分类确认不会自动改变 `working_copies.relative_path`。
- 改名、移动和移入回收站会影响所有用户可访问的共享文件，必须生成 OperationPlan。
- OperationPlan 必须明确显示变更前后位置和“影响所有用户共用文件”的风险。
- 用户确认后只能修改工作副本；受管原件、上传归档原件和历史 DocumentVersion 不得改变。

### 2.4 规范文件身份与映射

阶段六必须区分“用户当时上传的文档记录”和“当前共享工作副本对应的文档记录”。正式分类与物理操作的
规范目标统一定义为：

```text
canonical_working_file
= WorkingCopy.id
+ WorkingCopy.document_id
+ WorkingCopy.current_version_id
```

其中：

- `WorkingCopy.id` 是共享物理文件及路径操作的稳定身份。
- `WorkingCopy.document_id` 是当前共享工作副本正文、摘要、索引和正式分类的文档身份。
- `WorkingCopy.current_version_id` 是本次确认所针对的内容版本；不得用文件名或 SHA-256 代替身份。
- `content_sha256` 只用于完整性校验、并发检查和重复内容提示。即使哈希相同，也不能替用户合并两个
  不同工作副本；同名不同内容更不能自动合并。

统一实现 `CanonicalWorkingFileResolver` 或等价服务，按以下顺序解析：

1. 输入已经是 `WorkingCopy.document_id` 时，在共享系统工作区查找唯一 `ACTIVE` 工作副本。
2. 输入来自上传附件的 `Document` / `DocumentVersion` 时，通过
   `UploadArchiveRecord.upload_document_version_id -> managed_file_id -> WorkingCopy.managed_file_id`
   解析共享系统工作区中的当前主工作副本。
3. 分类建议绑定上传阶段 `document_id` 时，必须通过该建议的 `document_version_id` 执行相同追溯；
   不得把正式分类写回只代表上传暂存对象的 Document。
4. 解析到零个工作副本且导入任务仍在执行时，返回 `WORKING_COPY_NOT_READY`，保留待办上下文并允许
   worker 完成后重试；不得伪造成功。
5. 解析到零个且不存在进行中的导入任务时，提示用户重新附加或选择文件。
6. 解析到多个候选时必须展示文件选择卡；不得按文件名、当前版本或哈希自动挑选。
7. 回收站中的工作副本只允许返回“文件已删除，是否恢复”的选择，不得确认分类、读取正文或创建移动计划。

将上传阶段建议映射到共享工作副本时，还必须校验：

```text
suggestion.document_version 的内容哈希
== WorkingCopy.current_version_id 的内容哈希
== WorkingCopy.content_sha256
```

如果内容版本已经变化，应把旧建议标为过期并重新分类，不能将旧证据确认到新版本。正式分类必须写入
规范工作副本的 `document_id` 和 `current_version_id`，同时保留原始 `source_suggestion_id`、
上传来源文档 ID 和映射链路用于审计。

阶段六迁移必须为现有分类建议和反馈执行一次可重复的身份补建：能唯一映射且哈希一致的记录关联到规范
工作副本；无法唯一映射、已进回收站或版本不一致的记录进入 `NEEDS_REVIEW`，不得自动生成正式分类。

## 3. 产品用语调整

普通页面不再使用容易误解的“待处理（未分类）”。

建议统一为：

| 内部事实 | 普通用户文案 |
|---|---|
| 已有建议但未确认 | 分类待确认 |
| 没有足够正文证据 | 暂无可靠分类 |
| 分类置信度不足 | 分类依据不足，请确认 |
| 用户已经接受或纠正 | 已确认分类 |
| 已确认分类但尚未移动 | 已确认分类，文件位置未变 |
| 已生成移动计划 | 等待确认整理位置 |
| 已确认并完成移动 | 已按确认计划整理 |

文件卡必须说明未自动落位的原因，但不得展示内部阈值、分类器、Tool 或图谱分数。

## 4. 自然语言意图

阶段六至少识别以下意图：

| 用户表达示例 | 业务意图 | 是否需要 OperationPlan |
|---|---|---:|
| “这个分类是对的” | 接受当前分类建议 | 否 |
| “这个不是科研材料” | 拒绝指定分类建议 | 否 |
| “这个不是科研材料，是干部考察材料” | 拒绝原建议并纠正为指定分类 | 否 |
| “把刚才文件的分类改成人事师资/考核聘任” | 纠正逻辑分类 | 否 |
| “只修改分类，不要移动文件” | 固化分类并保持路径 | 否 |
| “按刚才确认的分类整理这个文件” | 创建移动计划 | 是 |
| “把这些已确认分类的文件整理到对应目录” | 创建批量移动计划 | 是 |
| “把这个文件改名为……” | 创建重命名计划 | 是 |
| “先给我改名建议，不要执行” | 只生成建议 | 否 |
| “是，覆盖原文件” | 确认当前唯一同名冲突并覆盖已有工作副本 | 当前回复即确认 |
| “同时保留” | 进入现有同名冲突保留闭环 | 是 |

“整理”存在分类、改名和移动三种含义。上下文不能唯一确定时必须展示选择卡，例如：

```text
您希望：
1. 只确认或修改分类，文件位置不变；
2. 按已确认分类移动共享工作副本；
3. 只生成文件名建议，不执行改名。
```

系统不得默认把“分类”解释为移动，也不得默认把“整理”解释为批量改名。

## 5. 文件范围解析

自然语言纠正必须先解析到稳定文件范围：

```text
L0 当前消息附件
-> L1 当前会话明确引用的文件
-> 精确完整文件名
-> 文件选择卡
```

规则：

1. “这个、刚才那个、第二个文件”必须由后端附件上下文解析。
2. 同名不同内容文件必须单选，不能按当前版本、哈希或路径替用户合并。
3. 回收站文件不能接受分类确认、改名或移动；必须先进入恢复确认闭环。
4. 多文件批量纠正必须逐文件展示结果；单项失败不能回滚其他已完成的逻辑反馈。
5. 物理移动计划创建时必须重新校验工作副本仍为 `ACTIVE` 且版本未变化。
6. “是、覆盖、同时保留”等短回复只能绑定当前会话最新且唯一的有效同名冲突记录；没有待决冲突或
   同时存在多个冲突时，不得猜测文件，必须展示文件选择卡。

文件范围唯一仍不代表分类建议唯一。自然语言确认还必须同时解析：

```text
working_copy_id
+ current_version_id
+ suggestion_id
+ category_id
```

当一个文件存在多个建议、一个分类名称对应多个 taxonomy 节点，或当前对话存在多个待确认文件时，后端
必须创建持久化的 `ClassificationClarification` 或等价选择状态。选择卡使用后端签发的选项 ID，至少保存
会话、用户、工作副本、当前版本、建议、分类、过期时间和状态。客户端和 LLM 只能选择选项，不能提交
任意 `working_copy_id`、`suggestion_id`、`category_id` 或分类路径。

选择状态在文件版本变化、工作副本移入回收站、taxonomy 版本失效、用户完成选择或超时后立即失效。
“这个分类是对的”“改成第二个分类”等短回复只能消费当前会话最新且唯一的有效选择状态。

## 6. 分类确认和纠正执行链路

### 6.1 接受建议

```text
用户：“这个分类是对的”
-> 后端解析 suggestion_id 和文件范围
-> 校验建议属于当前文件和当前 taxonomy
-> 追加 ACCEPTED 反馈
-> 写入或更新正式逻辑分类关系
-> 写 ChangeSet / CATEGORY_ADDED
-> 异步投影 Neo4j CONFIRMED_AS
-> 返回“分类已确认，文件位置未改变”
```

### 6.2 拒绝建议

```text
用户：“这个不是科研材料”
-> 校验原建议
-> 追加 REJECTED 反馈
-> 不创建该分类的正式关系
-> 如果已有同来源正式关系，按受控服务结束该关系并写 CATEGORY_REMOVED
-> 返回仍可选择的其他建议
```

拒绝一个标签不能删除其他有效标签。

### 6.3 纠正建议

```text
用户：“这个不是科研材料，是干部考察材料”
-> 拒绝原建议
-> 后端在当前 ACTIVE taxonomy 中解析目标稳定 ID
-> 目标不存在或多义时展示 taxonomy 选择卡
-> 追加 CORRECTED 反馈
-> 写入目标正式逻辑分类
-> 写 ChangeSet
-> 异步更新 Neo4j 投影
```

LLM 可以生成目标分类候选，不能自由写入正式分类路径。目标不在 taxonomy 时只能记录待评审候选，不得
自动发布新分类节点。

### 6.4 多用户反馈冲突

共享工作副本不意味着一个用户可以静默抹掉其他用户已经确认的事实：

- 同一分类被多个用户接受时，正式关系保持一条，但确认来源必须逐用户可追溯。
- 用户拒绝分类时，只撤销或替代该用户自己的有效确认来源；不能删除其他用户的确认来源。
- 用户纠正分类时，结束该用户对原分类的确认来源，并为目标分类增加新的确认来源。
- 不同用户确认了不同分类时允许保留多标签；如果它们争夺唯一主目录，文件进入“整理目标待选择”，
  不得自动移动。
- ops/admin 后续可以处理长期冲突，但普通用户反馈本身不得改写 taxonomy。

## 7. 按确认分类整理共享目录

只有用户进一步明确要求移动，才能执行：

```text
用户：“按刚才确认的分类整理文件”
-> 读取正式逻辑分类
-> 解析一个明确主目录目标
-> 生成 MOVE_WORKING_COPIES OperationPlan
-> 展示 before / after、文件数量、冲突和共享影响
-> 等待用户确认
-> confirmed-file-action
-> 原子移动工作副本
-> 写 WorkingCopyPathRecord、ChangeSet、ChangeItem
-> 更新检索投影中的路径弱信号
```

### 7.1 分类到物理目录的确定性映射

物理目录不能从分类显示名称、LLM 文本或前端参数临时拼接。taxonomy v2 的可整理节点必须增加显式
`organization_path` 配置，例如：

```json
{
  "id": "school.hr.appointment-assessment",
  "name": "考核聘任",
  "organization_path": ["人事师资", "考核聘任"]
}
```

规则如下：

1. `category_id + taxonomy_key + taxonomy_version` 是逻辑分类身份；
   `organization_path` 只是该版本下的受控物理映射，不能反过来作为分类外键。
2. 没有 `organization_path` 的节点只能作为逻辑标签，不能生成移动计划。
3. taxonomy 在应用启动和 CI 中校验每个目录段：禁止空值、`.`、`..`、绝对路径、分隔符、控制字符、
   Windows 保留名称、结尾空格或点，并限制单段和完整路径长度。
4. 目录解析器只接受后端已经确认的 `category_id`，不接受 LLM 或客户端提交的目标路径。
5. 文件只能移动到原 `WorkingCopy.working_copy_root_id` 对应的
   `WorkingCopyRoot.relative_storage_path` 之下；阶段六不跨工作副本根移动，也不允许移动到
   `MANAGED_ROOT_*`、`MANAGED_ROOT_ARCHIVE_WRITE_PATH`、上传原件目录或回收站目录。
6. 目标相对路径由后端确定性生成：

```text
WorkingCopyRoot.relative_storage_path
/ organization_path
/ 当前文件名
```

最终路径必须再次通过 `FileLifecycleStorageService` 的根目录约束和路径穿越检查。

7. 一个文件只有一个明确的有效 `PRIMARY` 分类且该节点可整理时，才能直接生成目标候选。多个分类竞争
   主目录、只有 `SECONDARY/RELATED` 标签或多个用户确认不同主分类时，必须展示“整理目标选择卡”。
8. “其他”“暂无可靠分类”“分类待确认”和自由分类候选默认没有物理目录映射，不允许自动落入
   “其他”或“待整理”；只有 taxonomy 明确配置可整理节点后才能作为目标。
9. taxonomy 升级或显示名称改变不会自动搬动已有文件。只有用户重新发起整理，并在新计划中看到新旧
   路径后，才允许按新映射移动。

### 7.2 移动计划与执行快照

新增对话动作 `MOVE_BY_CONFIRMED_CATEGORY`。该动作先通过目录解析器得到目标，再转换为底层
`MOVE_WORKING_COPIES` OperationPlan；不能把现有 `working-copy-action-plan-create` 当作已经完成
该能力。

OperationPlan 每个条目至少固化：

```text
working_copy_id
working_copy_root_id
document_id
document_version_id
content_sha256
before_relative_path
after_relative_path
category_id
taxonomy_key
taxonomy_version
document_category_id
```

确认执行时必须锁定工作副本和目标路径占用记录，并重新校验状态、当前版本、哈希、原路径、正式分类和
taxonomy 映射。任一事实变化则计划过期，停止执行并要求重新生成。

覆盖冲突不得直接对目标调用 `os.replace`。正确语义是：在同一受控操作中先把已有活动工作副本移入
可恢复回收站并完成路径审计，再移动当前工作副本；任一步失败都不能留下数据库与文件系统不一致。
“用户回复覆盖后直接执行”仅表示不再要求第二次用户确认，不表示跳过 OperationPlan、Confirmation、
ChangeSet 或文件锁。

约束：

1. 一个文件可以有多个逻辑分类，但一次物理移动只能选择一个主目标目录。
2. 多个分类都可作为主目录时必须让用户选择，不能按最高分自动决定。
3. 目标同名冲突不得自动覆盖或自动加版本后缀。系统必须先展示冲突文件名和选择：
   “是否覆盖已有文件”“同时保留”“取消”。
4. 用户对当前唯一有效冲突明确回复“是”“覆盖”或“覆盖原文件”时，该回复就是覆盖确认，后端必须
   直接执行覆盖，不再要求用户进行第二次重复确认。执行前仍须在同一受控事务中创建并确认
   `CONFLICT_REPLACE_EXISTING` OperationPlan，写入 OperationConfirmation、ChangeSet 和逐文件审计；
   这里的“直接覆盖”表示不增加第二轮用户确认，不表示可以绕过审计或原件保护。
5. 用户选择“同时保留”时沿用现有冲突选择闭环：先生成待确认计划，只有确认后才能分配稳定版本后缀；
   不得在用户选择前预先生成“第二版、第三版”等名称。
6. “覆盖已有文件”只作用于唯一共享目录中的现有活动工作副本。受管原件、上传归档原件和历史版本
   始终不可变；覆盖前必须保存可追溯的旧状态，禁止用文件系统 API 静默覆盖后丢失审计。
7. 除第 4 条中用户回复已经构成明确覆盖确认的情况外，确认前文件系统、`working_copies` 和路径记录
   均不得变化。
8. 确认后重命名或移动不得重建同内容 DocumentVersion、Chunk 或 Evidence。
9. 任何 Neo4j 失败都不能让 PostgreSQL 已确认分类或文件移动事务进入不一致状态；图谱投影可以重试。

## 8. 共享文件与用户边界

阶段六采用以下边界：

- `WorkingCopy`、正式逻辑分类和物理路径是共享业务事实。
- 用户 A 和用户 B 都可以通过各自对话访问共享 `ACTIVE` 文件。
- 用户 A 不能读取用户 B 的对话、个人附件上下文、个人反馈详情或操作确认记录。
- 共享文件修改必须记录发起人和确认人。
- 重复上传命中共享 `ACTIVE` 工作副本时不区分创建用户，所有登录用户都必须能选择“使用现有文件”；
  可以展示共享工作副本当前文件名和逻辑路径，但不得泄漏最初上传用户、上传暂存路径、来源会话或个人
  审计信息。
- 上传查重同时比较两类当前文件：共享目录中的 `ACTIVE` 工作副本，以及仍在受管文件索引中但尚未同步或
  物化成工作副本的当前文件。只排除 `TRASHED` 工作副本及其对应的已删除文件；回收站不参与 SHA-256、
  同名或近似内容匹配，只在用户明确按完整文件名请求查找或恢复时进入独立恢复选择流程。
- 用户上传归档原件和外部受管原件继续保持不可变。

如果后续需要限制某些共享文件的访问范围，应单独设计 ACL；不得通过复制多个用户工作副本实现隔离。

### 8.1 统一共享访问策略

新增 `SharedWorkingCopyAccessPolicy` 或等价应用层策略，所有普通用户文件入口必须调用该策略，不能继续
各自使用 `Document.user_id` 判断共享工作副本的归属。

当前阶段的授权规则固定为：

```text
authenticated user
+ shared SYSTEM_SHARED workspace
+ WorkingCopy.status == ACTIVE
= 可以检索、预览、下载和引用该共享工作副本
```

高风险操作还必须满足：

```text
当前用户发起
+ 当前会话内解析到唯一共享工作副本
+ 当前版本和路径快照未变化
+ OperationPlan 属于当前用户
+ OperationConfirmation 由当前用户提交
= 才能执行共享文件变更
```

必须统一改造并测试以下入口：

- 文件名检索、文件级检索投影、Chunk 正文检索、证据回答和 Neo4j 候选回查。
- 文件列表、预览、下载、正文读取、摘要和分类展示。
- 分类确认、重命名、移动、移入回收站和恢复。
- OperationPlan 创建、查询、确认和执行时的目标校验。
- 会话附件从上传 Document 映射到共享工作副本的过程。

共享访问不等于所有关联数据共享。以下对象继续按用户隔离：

- 用户上传暂存 Document、上传原文件名、上传会话和附件上下文。
- Conversation、Message、AgentRun、用户分类反馈明细、OperationConfirmation 和个人偏好。
- 重复上传候选对应的最初上传用户、上传暂存路径、来源会话和个人审计信息；共享工作副本当前文件名、
  逻辑路径和用于提交“使用现有文件”决策的受控标识不属于个人上传来源。

回收站文件不得进入普通检索、摘要、正文问答或宽泛相似搜索。只有通过完整文件名、带扩展名文件名或
后端已经解析的稳定文件引用精确命中时，才可以返回不含正文的删除状态卡并询问是否恢复；恢复完成前
不得读取其历史索引内容。

实现时禁止直接全局移除 `Document.user_id` 条件。上传来源访问仍按所有者校验；只有规范化为共享
`WorkingCopy` 后，才由本策略决定共享读取和受控操作权限。

## 9. PostgreSQL 数据边界

阶段六应补齐或复用以下事实：

### 9.1 正式分类关系

新增或补齐 `document_categories` 等价表，至少保存：

```text
id
working_copy_id
document_id
document_version_id
category_id
category_path_json
relation_role
status
taxonomy_key
taxonomy_version
classifier_version
source
source_suggestion_id
evidence_json
created_at
updated_at
ended_at
```

`document_id` 和 `document_version_id` 必须来自规范共享工作副本，不允许保存上传暂存 Document 的 ID。
`status` 至少包含 `CONFIRMED`、`ENDED`；阶段六的用户确认关系不使用 `SUGGESTED` 冒充正式分类。

数据库应建立部分唯一约束：

```text
UNIQUE (
  working_copy_id,
  document_version_id,
  category_id,
  relation_role
) WHERE status = 'CONFIRMED'

UNIQUE (
  working_copy_id,
  document_version_id
) WHERE status = 'CONFIRMED' AND relation_role = 'PRIMARY'
```

上述字段必须建立到 `working_copies`、`documents` 和 `document_versions` 的外键，并校验三者属于同一
规范工作副本。显示名称、分类路径和内容哈希都不能作为唯一键。

`relation_role` 的确定也必须来自用户可解释的决定：

- 文种维度使用 `DOCUMENT_TYPE`，不能成为物理整理目录。
- 普通业务标签确认可以是 `SECONDARY` 或 `RELATED`，不能仅因规则分数最高自动升级为 `PRIMARY`。
- 用户明确说“作为主分类”，或在整理目标选择卡中选择某分类时，才把该分类设为 `PRIMARY`；同一文件
  当前版本最多只能有一个有效 `PRIMARY`。
- 设置新 `PRIMARY` 时必须结束旧主分类角色或将其降为辅助角色，并写入同一分类决定事务及 ChangeSet。

多用户确认来源应使用 `document_category_confirmation_sources` 或等价追加式关联保存
以下字段：

```text
id
document_category_id
user_id
feedback_id
suggestion_id
status                 # ACTIVE / WITHDRAWN / SUPERSEDED
supersedes_source_id
created_at
ended_at
```

同一用户对同一正式关系只能有一个 `ACTIVE` 确认来源。不得在正式关系上只保留最后一个
`confirmed_by` 并覆盖其他用户来源；只有最后一个有效确认来源结束时，正式关系才能由
`CONFIRMED` 变为 `ENDED`。

### 9.2 反馈与历史

继续使用追加式 `document_category_feedback`：

- 同一用户的新反馈停用上一条当前反馈，但不得物理删除历史记录。
- 更正必须同时形成原分类负样本和目标分类正样本。
- 正式分类关系必须能追溯到来源建议和反馈。
- 反馈记录必须增加或等价保存规范 `working_copy_id`、规范 `document_id` 和
  `document_version_id`；原上传 Document 只能保存在来源映射审计中。
- 多用户确认共享建议时，不能继续只依赖“建议所属 AgentRun 的 user_id”判断访问。服务应先验证用户
  有权访问规范共享工作副本，再校验 suggestion 与该工作副本当前版本的映射；其他用户的会话和
  AgentRun 仍不可见。

### 9.3 统一正式分类事务

新增 `ClassificationDecisionService` 或等价事务服务。前端按钮、自然语言 Tool 和管理端人工纠正必须
调用同一服务，事务输入只接受后端已解析并签发的选择：

```text
actor_user_id
conversation_id
message_id / idempotency_key
working_copy_id
expected_document_version_id
suggestion_id
action                    # ACCEPT / REJECT / CORRECT
target_category_id        # 仅 CORRECT
relation_role
```

`actor_user_id` 必须来自 JWT/服务端认证上下文，不能接受客户端自报；其余 ID 必须由当前会话的有效
选择状态或后端附件解析结果产生。

单次事务执行顺序固定为：

1. 锁定 `WorkingCopy`、来源建议、当前用户的有效反馈和相关正式分类关系。
2. 通过 `CanonicalWorkingFileResolver` 校验工作副本为 `ACTIVE`、版本未变化、建议证据与当前内容哈希
   一致、分类存在于当前允许确认的 ACTIVE taxonomy。
3. 使用 `conversation_id + message_id + working_copy_id + suggestion_id + action + target_category_id`
   或等价稳定键检查幂等；重复请求返回原结果，不新增重复反馈、确认来源或 ChangeSet。
4. 追加反馈，停用当前用户被替代的上一条反馈，但不删除历史记录。
5. `ACCEPT`：创建或复用正式关系，并创建当前用户的有效确认来源。
6. `REJECT`：结束当前用户对原分类的确认来源；只有不存在其他有效确认来源时才结束正式关系。
7. `CORRECT`：在同一事务内结束当前用户对原分类的来源，同时为目标分类创建正式关系和确认来源；
   目标不存在或有歧义时整笔事务不写入，先返回 taxonomy 选择卡。
8. 创建一个真实 ChangeSet，并按实际差异写入 `CATEGORY_CONFIRMED`、`CATEGORY_ADDED`、
   `CATEGORY_REMOVED` 或 `CATEGORY_CORRECTED` ChangeItem。没有状态变化的幂等请求不得伪造变更。
9. 在同一 PostgreSQL 事务中创建持久化图谱投影待办；不得在事务提交前直接依赖 Neo4j 成功。
10. 提交后返回正式分类快照和“文件位置未改变”的普通用户回执。

任何一步失败必须回滚反馈、正式关系、确认来源、ChangeSet 和投影待办，不能出现“反馈成功但正式分类
失败”或“正式分类成功但没有可重试图谱事件”的部分状态。

分类关系是内容版本事实。工作副本产生新 `DocumentVersion` 时，旧版本正式关系保留为历史但不自动复制
到新版本；系统应为新版本重新生成建议或请求确认。

### 9.4 审计

逻辑分类确认也必须写真实 ChangeSet：

```text
CATEGORY_ADDED
CATEGORY_REMOVED
CATEGORY_CONFIRMED
CATEGORY_CORRECTED
```

物理移动继续使用：

```text
FILE_MOVED
working_copy_path_records
operation_plans
operation_confirmations
```

不得让运行日志替代上述业务事实。

## 10. Neo4j 投影边界

PostgreSQL 和 taxonomy 配置仍是 source of truth。Neo4j 只保存可重建投影：

- 接受或纠正后的有效正式分类可以投影为 `CONFIRMED_AS`。
- 未确认建议只能是 `SUGGESTED_AS`，不得参与可信传播。
- 拒绝或被替代关系必须从有效投影中移除或标记失效。
- 文件物理目录继续使用 `LOCATED_IN`；不得因为目录位置生成 `CONFIRMED_AS`。
- 投影任务必须写 `graph_projection_runs`。
- Neo4j 关闭或不可用时，分类确认、文件检索和 OperationPlan 必须继续工作。
- 不允许 LLM 生成自由 Cypher，不在图中保存正文、密钥或绝对路径。

正式分类事务必须同时写入 `PROJECT_CONFIRMED_CLASSIFICATION` 持久化待办，或使用等价的事务 Outbox。
待办至少包含 `document_category_id`、规范 `working_copy_id`、`document_version_id`、期望关系状态和
幂等键。worker 消费后再创建 `GraphProjectionRun` 并投影 Neo4j：

- 重复消费必须幂等，不产生重复 `CONFIRMED_AS`。
- Neo4j 不存在某种关系类型时不应让查询主链路失败；首次成功投影会自然创建关系类型。
- 失败时记录有限错误摘要和重试时间，不能回滚已经提交的 PostgreSQL 正式分类。
- 定期 reconciliation 按 PostgreSQL 当前有效关系重建投影，修复进程崩溃、手工清图或历史遗漏。
- 删除/结束投影前必须比较期望状态版本，旧任务不能覆盖更新后的正式关系。

## 11. 后端任务

按以下顺序实施，不能先接自然语言路由再补数据和权限边界：

1. 复核阶段五真实数据库迁移、活动工作副本索引和前端验收状态；建立阶段六“复用、改造、新增”清单。
2. 实现 `CanonicalWorkingFileResolver`，补齐上传 Document、`UploadArchiveRecord`、`ManagedFile`
   和当前共享 `WorkingCopy` 的规范身份映射及历史数据补建。
3. 实现 `SharedWorkingCopyAccessPolicy`，统一改造检索、预览、下载、证据回答、分类和 OperationPlan
   入口；保留上传来源、会话和用户反馈隔离。
4. 新增正式分类、确认来源、分类澄清状态和图谱 Outbox 所需迁移、外键、部分唯一索引及幂等约束。
5. 实现正式逻辑分类 Repository 和 `ClassificationDecisionService`，在一个事务内写反馈、正式关系、
   确认来源、ChangeSet/ChangeItem 和图谱待办。
6. 将现有分类按钮 API 收敛到统一事务服务；移除允许用动态建议绕过 ACTIVE taxonomy 的自由路径。
7. 实现文件与分类建议选择服务，使用后端签发选项，禁止客户端或 LLM 伪造
   `working_copy_id`、`suggestion_id`、`category_id` 和路径。
8. 定义分类确认、拒绝、纠正和整理的稳定意图与 Tool schema，再接入自然语言分类纠正 Planner 路由。
9. 为 taxonomy 增加显式 `organization_path`，实现启动校验和确定性
   `CategoryOrganizationPathResolver`。
10. 扩展对话工作副本计划服务，新增 `MOVE_BY_CONFIRMED_CATEGORY`，由后端解析目标后创建
    `MOVE_WORKING_COPIES` OperationPlan；继续由 `confirmed-file-action` 执行。
11. 补齐共享影响、版本变化、回收站、目标路径占用和同名冲突校验。
12. 为同名冲突建立会话级持久化选择状态：短回复“是”仅在唯一待决冲突下解释为覆盖；覆盖选择在
    单次后端事务中完成计划创建、确认和执行，不再返回第二张重复确认卡。
13. 覆盖执行必须锁定冲突记录、源工作副本和目标工作副本；先把既有工作副本移入可恢复回收站，再移动
    当前工作副本，禁止使用文件系统静默覆盖。任一事实变化时停止执行并重新提示用户。
14. 实现 `PROJECT_CONFIRMED_CLASSIFICATION` 幂等消费、`GraphProjectionRun` 记录、退避重试和
    PostgreSQL 到 Neo4j 的 reconciliation。
15. 将仍返回虚假成功的通用 `change-report`、`operation-plan-create` 等占位入口接入真实服务，
    或从当前可调用目录中移除。

## 12. 前端任务

1. 将“未分类/待处理”语义调整为“分类待确认/暂无可靠分类”。
2. 文件卡展示已有建议、当前正式分类和未自动落位原因。
3. 保留“正确、错误、更正”按钮，并与自然语言入口复用同一后端服务。
4. 更正时使用 taxonomy 选择卡，不要求用户手工输入内部分类 ID。
5. 分类确认后明确显示“文件位置未改变”。
6. 用户要求整理目录时展示专用移动 OperationPlan 卡。
7. 计划卡按真实操作类型显示“确认移动、确认改名、确认移入回收站”，不能统一写成重命名。
8. 多文件任务逐文件展示成功、失败、待选择和未执行项。
9. 普通页面不展示 Tool、Skill、taxonomy 版本、Neo4j 或本地路径。
10. 同名冲突卡明确展示冲突文件名以及“覆盖已有文件、同时保留、取消”三个选择；用户选择覆盖后直接
    展示最终逐文件结果，不再插入第二张确认卡。
11. 多个同名冲突同时待处理时，“是”不能直接执行，必须先展示文件选择卡让用户确定一个冲突。
12. “这个分类是对的”等表达存在多个文件或多个建议时，展示同时包含文件名和分类标签的单选卡；前端
    只回传后端签发的 option ID。
13. 共享活动工作副本对所有登录用户使用同一种文件卡；不得展示上传用户、来源会话、归档路径或其他
    用户反馈详情。
14. 精确命中回收站文件时只展示“文件已删除，是否恢复”卡，不展示旧正文、摘要或分类操作按钮。

## 13. 自动化测试

至少覆盖：

1. 接受建议写反馈、正式分类和 ChangeSet。
2. 拒绝一个建议不影响其他分类。
3. 更正同时保存原分类负样本和目标正样本。
4. 目标分类不在 taxonomy 时不写正式关系。
5. 沉默、预览和下载不产生分类反馈。
6. 分类确认不改变工作副本路径或原件。
7. 用户明确要求移动后只创建等待确认计划。
8. 确认前无物理副作用。
9. 确认后移动共享工作副本并写完整路径审计。
10. 移动不重建同内容版本索引。
11. 同名目标冲突等待用户选择。
12. 同名不同内容文件先展示单选卡。
13. 回收站文件必须先恢复。
14. 用户不能读取其他用户会话和反馈。
15. 所有用户能按产品要求检索共享活动工作副本。
16. Neo4j 不可用时 PostgreSQL 分类确认仍成功，投影任务可重试。
17. 普通回执不包含内部 Tool、路径或图谱信息。
18. 重复提交相同反馈和重复确认 OperationPlan 保持幂等。
19. 用户 B 拒绝分类不能删除用户 A 仍有效的确认来源。
20. 多个确认分类竞争主目录时必须等待用户选择，不能自动移动。
21. 同名冲突出现时不自动覆盖、不自动生成版本后缀，并展示覆盖、同时保留和取消选择。
22. 当前唯一冲突下回复“是”直接覆盖已有活动工作副本，不出现第二次确认，同时完整保存
    OperationPlan、OperationConfirmation、ChangeSet 和文件版本/路径审计。
23. 没有待决冲突或存在多个待决冲突时回复“是”不得执行覆盖，必须提示用户选择具体文件。
24. 选择“同时保留”继续使用现有冲突计划，确认后才生成稳定版本后缀。
25. 覆盖现有工作副本不修改受管原件、上传归档原件或历史 DocumentVersion。
26. 上传 Document 上的建议能通过 `UploadArchiveRecord -> ManagedFile -> WorkingCopy` 唯一映射到
    规范共享文档并写正式分类；正式关系不绑定上传暂存 Document。
27. 上传建议与当前工作副本版本哈希不一致时拒绝确认并触发重新分类。
28. 同名、同哈希或同 DocumentVersion 的多个工作副本不被身份解析器自动合并。
29. 所有登录用户可以检索、预览和引用共享 `ACTIVE` 工作副本，但不能读取其他用户的上传来源、会话、
    AgentRun、反馈详情和 OperationConfirmation。
30. `TRASHED` 工作副本不进入普通文件名、正文和图谱搜索；精确文件名命中只返回恢复提示且不泄漏正文。
31. 一个文件有多个分类建议时，“这个分类是对的”必须等待建议选择，不能默认接受第一项或最高分。
32. 重复提交相同分类决定只产生一条有效反馈来源和一次实际 ChangeSet。
33. 用户 A、B 都确认同一分类后，A 撤回只能结束 A 的来源；正式分类和 B 的来源继续有效。
34. 新 DocumentVersion 不自动继承旧版本的正式分类。
35. taxonomy 未配置 `organization_path`、配置非法路径或目标越过当前 WorkingCopyRoot 时不能创建移动
    OperationPlan。
36. 移动计划确认前后版本、哈希、原路径、分类或 taxonomy 映射发生变化时，执行必须停止并令计划过期。
37. 覆盖冲突先把旧工作副本移入可恢复回收站，再移动新工作副本；中途失败不会留下静默覆盖或孤立路径。
38. PostgreSQL 提交正式分类后 Neo4j 暂时不可用，Outbox 仍保留；恢复后重复消费只生成一条有效关系。

## 14. 前端烟测示例

在 `/chat` 依次验证：

```text
这个分类是对的。
这个不是科研材料。
这个不是科研材料，是干部考察材料。
把刚才文件的分类改成人事师资/考核聘任。
只修改分类，不要移动文件。
按刚才确认的分类整理这个文件。
把这三个已确认分类的文件整理到对应目录。
先给我文件名建议，不要改名。
把第二个文件改名为“2026年教师考核通知.docx”。
将这个文件改名为一个已经存在的文件名。
是，覆盖原文件。
同时保留。
```

每次物理移动或改名都必须有 OperationPlan 审计。普通改名和“同时保留”在页面确认前文件不变；同名
冲突卡已经完整展示覆盖对象时，用户回复“是，覆盖原文件”本身就是确认，系统直接执行并展示结果，不再
要求第二次确认。

## 15. 阶段退出条件

阶段六只有在以下全部满足时完成：

- 用户能从聊天自然语言接受、拒绝和纠正分类。
- 按钮反馈和自然语言反馈使用同一事务服务。
- 上传附件、分类建议和当前共享工作副本通过规范身份解析器唯一关联；正式分类只绑定当前共享工作副本
  及其当前版本。
- 正式分类关系与建议、反馈和证据可追溯。
- 反馈、正式关系、确认来源、ChangeSet 和图谱 Outbox 在同一 PostgreSQL 事务中保持一致且可幂等重试。
- 页面不再把“分类待确认”描述为完全未分类。
- 分类确认不会自动移动文件。
- 所有登录用户能通过统一共享访问策略读取 `ACTIVE` 工作副本，同时不能读取其他用户私有会话、上传来源
  和反馈详情。
- 用户明确要求整理后才创建共享目录移动计划。
- 物理目标只能由 ACTIVE taxonomy 中显式 `organization_path` 映射，并始终位于原
  `WorkingCopyRoot` 受控范围内。
- 确认前无物理变化，确认后有完整 OperationPlan、Confirmation、ChangeSet 和路径记录。
- 所有共享文件变更明确提示影响所有用户。
- 同名冲突默认停止；用户明确选择覆盖后不再重复确认，选择同时保留时沿用现有版本后缀确认闭环。
- 受管原件和上传归档原件始终不变。
- Neo4j 只是可重建投影，失败时主链路无损降级。
- 后端完整测试、前端 build 和 Windows 页面烟测通过。

### 15.1 2026-07-27 实施结果

当前代码已经完成：

- 上传附件和共享工作副本的 `CanonicalWorkingFileResolver` 身份映射与哈希/版本校验。
- 共享活动工作副本的列表、检索、证据回答、预览、下载、回收站提示和文件操作访问边界。
- 正式分类、逐用户确认来源、分类歧义状态和 Neo4j Outbox 数据表及 Alembic 迁移。
- 按钮反馈与自然语言接受、拒绝、纠正共用的原子分类决定事务。
- 多文件、多建议和目标分类多义时的后端签发选择项及前端选择卡。
- taxonomy 稳定 ID 选择器，前端不再提交自由文本分类路径。
- taxonomy `organization_path` 校验、主分类目标解析、`MOVE_BY_CONFIRMED_CATEGORY`
  OperationPlan、确认执行前分类/版本/哈希/路径快照复核。
- 目标同名冲突持久化、覆盖/同时保留/取消选项；唯一冲突的明确覆盖回复会在同一次
  后端调用中创建、确认并执行计划，旧工作副本先进入可恢复回收站。
- PostgreSQL 正式分类到 Neo4j 的幂等 Outbox、退避重试、投影运行审计和全量 reconciliation。
- 移动计划、分类选择、同名冲突和“分类已确认，文件位置未变”的普通页面展示。

本机验收结果：

```text
后端：669 passed, 19 skipped
前端：npm run build 通过
Alembic：20260728_0001 为唯一 head
git diff --check：通过
```

尚需在目标 Windows 开发环境执行：

1. `python -m alembic -c apps/api/alembic.ini upgrade head`。
2. 重启 API、worker 和前端，按第 14 节完成页面烟测。
3. 开启 Neo4j 时确认 `GRAPH_PROJECTION_WORKER_ENABLED=true` 与
   `NEO4J_SYNC_ENABLED=true`，观察分类 Outbox 被 worker 消费。

目标 Windows 页面烟测未执行前，只能认定“代码开发完成、本机自动化验收通过”，不能把跨平台部署验收
标记为完成。

## 16. 非目标

阶段六不实现：

- 自动移动全部文件。
- 按最高分分类自动选择物理目录。
- 自动发布新的 taxonomy。
- 未经用户明确选择的自动覆盖，或永久删除文件。
- 复杂 ACL 或为每个用户复制工作目录。
- GraphRAG 文件问答。
- 自动 Skill 发布。
- 因用户沉默而推断分类正确。
