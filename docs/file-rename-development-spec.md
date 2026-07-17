# 文件智能重命名开发规格

## 1. 文档目的

本文是“年份 + 文号 + 正文标题”文件智能重命名功能的开发规格。受管文件闭环已实现；2026-07-14 起新增上传附件私有临时存储重命名阶段。

相关文档：

- `agent.md`
- `docs/file-rename-implementation-plan.md`
- `docs/conversational-file-agent-development-blueprint.md`
- `skills/operation-plan/SKILL.md`
- `skills/confirmed-file-action/SKILL.md`

## 2. 现状与缺口

当前项目已经具备：

- 受管目录配置、扫描、过滤和逻辑路径访问。
- 受管文件快照、SHA-256 版本识别和解析结果复用。
- `document_pages.text_content` 完整正文读取。
- Agent Planner、Tool Registry 和 Capability Catalog。
- OperationPlan 数据表、查询 API 和确认记录。
- ChangeSet 和 ChangeItem。

当前缺口：

- `operation-plan-create` Tool 仍返回内存占位 ID。
- `confirmed-file-action` Tool 仍返回占位执行结果。
- `OperationPlanRepository.confirm_plan()` 会在没有真实文件动作时直接标记 `EXECUTED`。
- 没有年份、文号和正文标题的统一提取服务。
- 没有安全文件名策略和重命名执行器抽象。
- 没有 `managed_files` 与真实重命名结果的同步。
- 没有真实 `FILENAME_CHANGED` ChangeItem 执行链路。

## 3. 领域边界

### 3.1 支持对象

受管目录普通文件执行原地重命名：

```text
同一个 managed root
+ 同一个父目录
+ 只改变 basename
+ 保留原扩展名
```

聊天上传附件：

- 使用后端确定的 `document_ids` 读取正文并生成建议。
- 生成 `RENAME_UPLOADED_FILES` OperationPlan，确认后只改变 Document 私有临时存储中的 basename。
- 共享 FileObject 使用写时复制，不能影响其他 Document 或受管快照。
- 当前不做分类、受管目录选择或正式归档。

### 3.2 权限边界

- 所有用户可以通过对话查询允许访问的受管目录。
- OperationPlan 只能由创建它的用户确认。
- 普通 `user` 可以创建并确认自己的重命名 OperationPlan。
- `ops/admin` 同样只能确认自己创建的计划；如后续需要代审批，应新增独立审批机制。
- 后端只接受逻辑 `root_key`、`managed_file_id` 和受控相对路径，不向 LLM 暴露服务器绝对路径。

### 3.3 持久化边界

- 建议、计划、执行结果属于 Persistent Stores。
- `AgentGraphState` 只保留 suggestion 摘要、operation_plan_id、changeset_id 和逐文件状态。
- 执行器、数据库 Session、F2 binary path 和临时文件属于 `AgentRuntimeContext` 或请求级服务，不进入 State。

## 4. 模块设计

新增目录：

```text
apps/api/app/modules/file_rename/
├─ __init__.py
├─ schemas.py
├─ metadata_extractor.py
├─ policy_loader.py
├─ filename_builder.py
├─ suggestion_service.py
├─ executor.py
├─ native_executor.py
└─ f2_executor.py
```

新增规则文件：

```text
rules/file_rename_policy.json
```

新增 Skill：

```text
skills/file-rename/SKILL.md
```

### 4.1 `metadata_extractor.py`

职责：

- 从 `document_pages` 按页读取完整正文。
- 从原文件名和正文中生成年份、文号和标题候选。
- 对每个字段记录值、来源、状态和 evidence item。
- 确定性规则优先，存在歧义时才调用 LLM 结构化提取服务。

禁止：

- 不构造目标路径。
- 不修改文件。
- 不直接读取未授权绝对路径。
- 不把全文写入 AgentGraphState 或日志。

### 4.2 `filename_builder.py`

职责：

- 根据已验证字段和 RenamePolicy 构造 basename。
- 保留并规范化扩展名。
- 清理非法字符、控制长度、规范空白和分隔符。
- 返回结构化校验结果，不执行文件动作。

### 4.3 `suggestion_service.py`

职责：

- 组合字段提取、命名策略和冲突预检。
- 支持批量文件并进行逐文件失败隔离。
- 输出 READY、NEEDS_REVIEW、CONFLICT 或 FAILED。
- READY 和 USER_NAMED 项才允许进入 OperationPlan。
- NEEDS_REVIEW 项保留在持久化重命名批次中，并阻止创建部分 OperationPlan；用户更正或排除后再一次性固化全部可执行项。

### 4.4 `executor.py`

定义统一执行器协议：

```python
class RenameExecutor(Protocol):
    def preview(self, request: RenameBatchRequest) -> RenameBatchResult: ...
    def execute(self, request: RenameBatchRequest) -> RenameBatchResult: ...
    def compensate(self, items: list[RenameExecutionItem]) -> RenameBatchResult: ...
```

### 4.5 `native_executor.py`

职责：

- 使用 Python 文件系统 API 执行同目录 basename 重命名。
- 执行前再次检查源文件、目标文件和路径边界。
- 不覆盖现有文件。
- 返回逐文件结构化结果。
- 支持按完成顺序逆序补偿。

### 4.6 `f2_executor.py`

职责：

- 将后端已经验证的 before/after 映射写入私有临时 CSV。
- 使用参数数组调用 F2，禁止 `shell=True`。
- 先 dry-run，再解析 JSON 输出。
- 验证 F2 输出数量、源文件和目标文件与 OperationPlan 完全一致。
- 用户确认后增加 `-x` 执行。
- 将外部进程状态映射为统一 `RenameBatchResult`。
- 删除临时 CSV，不记录其中的绝对路径。

F2 禁止参数：

```text
--allow-overwrites
--fix-conflicts
--hidden
--include-dir
--only-dir
--target-dir
```

## 5. 数据契约

### 5.1 字段候选

```json
{
  "value": "校发〔2026〕12号",
  "status": "RESOLVED",
  "source": "document_page",
  "confidence": 0.96,
  "evidence_items": [
    {
      "type": "text_quote",
      "page_number": 1,
      "sheet_name": null,
      "quote": "校发〔2026〕12号",
      "source": "document_pages"
    }
  ],
  "alternatives": []
}
```

字段状态：

```text
RESOLVED
AMBIGUOUS
MISSING
INVALID
```

决策不能只依赖 confidence 数值；`AMBIGUOUS`、`MISSING` 和 `INVALID` 一律不得自动执行。

### 5.2 重命名建议

```json
{
  "managed_file_id": "managed-file-id",
  "document_id": "snapshot-document-id",
  "root_key": "school_files",
  "relative_path": "党办/旧文件名.pdf",
  "proposed_relative_path": "党办/2026_校发〔2026〕12号_关于做好奖学金评审工作的通知.pdf",
  "year": {},
  "document_number": {},
  "title": {},
  "status": "READY",
  "warnings": [],
  "errors": []
}
```

建议状态：

```text
READY
NEEDS_REVIEW
CONFLICT
FAILED
```

### 5.3 OperationPlan item

现有 `OperationPlanItem` 继续保留 `document_id`，在 before/after 中扩展受管文件信息：

```json
{
  "document_id": "snapshot-document-id",
  "before": {
    "managed_file_id": "managed-file-id",
    "root_key": "school_files",
    "relative_path": "党办/旧文件名.pdf",
    "source_sha256": "sha256",
    "source_size_bytes": 12345,
    "source_modified_at": "2026-07-13T10:00:00+08:00"
  },
  "after": {
    "relative_path": "党办/2026_校发〔2026〕12号_标题.pdf"
  },
  "execution_status": "PLANNED",
  "rename_metadata": {
    "policy_version": "1.0",
    "year": {},
    "document_number": {},
    "title": {}
  }
}
```

如 Pydantic 当前禁止 `rename_metadata`，应显式扩展 schema，不能把字段塞进不受校验的任意 JSON。

### 5.4 执行结果

```json
{
  "status": "PARTIAL",
  "matched_count": 3,
  "completed_count": 2,
  "failed_count": 1,
  "items": [
    {
      "managed_file_id": "managed-file-id",
      "before_relative_path": "党办/旧文件名.pdf",
      "after_relative_path": "党办/新文件名.pdf",
      "status": "COMPLETED",
      "error_code": null,
      "error_message": null
    }
  ]
}
```

## 6. 字段提取规则

### 6.1 年份

优先级：

```text
明确发布日期或落款年份
-> 规范文号中的年份
-> 正文标题中的明确年份
-> 原文件名中的明确年份
-> 文件元数据年份，仅作为低优先级候选
```

约束：

- 只接受合理范围内的四位年份。
- 多个互相冲突的业务年份进入 `AMBIGUOUS`。
- 文件系统创建时间不能自动当作文书年份。

### 6.2 文号

至少支持：

```text
校发〔2026〕12号
学工字〔2026〕8号
教办发[2026]15号
2026年第6号
```

约束：

- 保留机关代字和完整符号语义。
- 统一半角方括号等可规范符号为项目约定格式。
- 正文候选优先于原文件名候选。
- 多个不同文号且无法判断主文号时进入 `AMBIGUOUS`。

### 6.3 正文标题

优先级：

```text
Docling 或解析器提供的一级标题
-> 文号附近的首个有效标题
-> 首页前若干非空段落的标题候选
-> 原文件名移除年份、文号和扩展名后的候选
```

标题清理：

- 移除已提取的年份和文号重复片段。
- 移除“扫描件”“最终版”“副本”等可配置噪声词，但保留业务含义。
- 不删除“通知”“办法”“报告”等文种信息。
- 不允许 LLM 改写标题，只能从原文中抽取或选择候选。

## 7. RenamePolicy

规则文件示例：

```json
{
  "policy_key": "school_official_document",
  "version": "1.2",
  "separator": "_",
  "templates": [
    {
      "key": "official_document",
      "template": "{year}_{document_number}_{title}{extension}",
      "required_fields": ["year", "document_number", "title"]
    },
    {
      "key": "ordinary_material",
      "template": "{year}_{title}{extension}",
      "required_fields": ["year", "title"],
      "when": "document_number_missing"
    }
  ],
  "missing_field_strategy": "NEEDS_REVIEW",
  "conflict_strategy": "VERSION_SUFFIX",
  "duplicate_title_strategy": "FULL_DATE_THEN_VERSION",
  "include_hidden": false,
  "rename_directories": false,
  "preserve_extension": true,
  "lowercase_extension": true,
  "max_filename_bytes": 240,
  "noise_terms": ["扫描件", "副本"]
}
```

配置加载要求：

- 启动或首次使用时通过 Pydantic 校验。
- 配置无效时关闭真实执行并记录错误，不使用不完整规则继续运行。
- policy version 写入 OperationPlan，确认执行时使用计划中的版本核对。
- 后续修改规则不能悄悄改变已经等待确认的计划结果。
- `FULL_DATE_THEN_VERSION` 表示同目录同标题文件先以 `YYYYMMDD` 区分，日期仍相同时再追加中文版本号。

## 8. Planner 与 Tool 改造

新增意图：

```text
SUGGEST_RENAME
CONFIRMED_OPERATION
```

新增 Tool：

| Tool | 职责 | 副作用 | 确认 |
|---|---|---:|---:|
| `generate-rename-suggestions` | 基于解析结果生成建议 | 可读取持久化正文，不改文件 | no |
| `operation-plan-create` | 持久化重命名计划 | yes | no |
| `confirmed-file-action` | 执行已确认计划 | yes | yes |

Planner 示例步骤：

```text
managed-file-read-document
-> generate-rename-suggestions
-> operation-plan-create
```

规则：

- “帮我按年份、文号和标题重命名党办文件”必须进入 `SUGGEST_RENAME`。
- 文件范围由后端受管目录解析服务确定，不由 LLM 猜测。
- Planner 只声明 Tool plan，不调用 F2，不生成绝对路径。
- 存在 `NEEDS_REVIEW` 时，只对 READY 项创建计划，并在回执中逐项列出未纳入项及原因，不阻止其他 READY 项确认执行。

## 9. OperationPlan 状态机

建议状态：

```text
WAITING_CONFIRMATION
EXECUTING
EXECUTED
PARTIAL
FAILED
CANCELLED
STALE
```

确认过程：

1. 查询当前用户拥有且处于 `WAITING_CONFIRMATION` 的计划。
2. 写入 `operation_confirmations`。
3. 把计划推进到 `EXECUTING` 并提交，避免重复确认并发执行。
4. 重新校验每个文件的路径、指纹、SHA-256 和目标冲突。
5. 调用配置的 RenameExecutor。
6. 更新 `managed_files` 和 ChangeSet。
7. 根据结果更新为 `EXECUTED`、`PARTIAL`、`FAILED` 或 `STALE`。

`STALE` 表示计划生成后源文件已经变化，用户需要重新生成建议和计划。

## 10. 文件系统与数据库一致性

每个计划项按以下顺序执行：

```text
校验源文件
-> 校验目标名称和目标不存在
-> 执行文件系统 rename
-> 更新 managed_files.relative_path / filename / fingerprint
-> 写入 ChangeItem
```

补偿原则：

- 文件系统成功、数据库失败：立即尝试恢复原文件名。
- 补偿成功：项目标记 FAILED，并记录数据库错误。
- 补偿失败：计划标记 NEEDS_REVIEW 或 FAILED，记录高优先级审计事件。
- 批量任务按文件隔离；最终状态可以是 PARTIAL。
- 不依赖 F2 全局 undo 代替项目补偿和 ChangeSet。

并发控制：

- 确认时以 OperationPlan 状态更新防止重复执行。
- 执行前重新计算文件指纹或 SHA-256。
- 同一 managed file 同时只允许一个写操作计划执行。
- 后续有并发需求时使用 PostgreSQL advisory lock 或等价锁，不把锁对象放入 Graph State。

## 11. F2 调用规范（第二次迭代）

第一版只实现 Native 执行器。第二次迭代接入 F2 时，F2 仅通过 `F2RenameExecutor` 调用。

预演命令语义：

```text
f2 --csv <generated-plan.csv> --json
```

执行命令语义：

```text
f2 --csv <generated-plan.csv> --json -x
```

实现要求：

- 使用 `subprocess.run([...], shell=False)`。
- 设置明确 timeout。
- 使用受控环境变量和工作目录。
- CSV 由标准 `csv` 模块写入私有临时目录。
- 输出 JSON 必须经过 schema 校验。
- stderr 只记录摘要，不记录绝对路径和文件正文。
- dry-run 返回的目标名必须与 OperationPlan 完全一致。
- F2 自动增加冲突序号时视为预演失败。
- F2 缺失、版本不匹配或输出无法解析时返回结构化错误。
- 部署固定已测试版本并记录许可证，不在服务启动时联网下载。

## 12. ChangeSet 设计

成功项：

```text
change_type = FILENAME_CHANGED
before_value_json = {root_key, relative_path, filename, sha256}
after_value_json = {root_key, relative_path, filename, sha256}
source = confirmed-file-action
execution_status = COMPLETED
```

失败项：

```text
change_type = FILE_OPERATION_FAILED
before_value_json = {root_key, relative_path}
after_value_json = {proposed_relative_path, error_code}
source = confirmed-file-action
execution_status = FAILED
```

如果当前 ChangeItem 枚举尚未包含 `FILE_OPERATION_FAILED`，开发时应增加明确类型，不能伪装成 `FILENAME_CHANGED`。

ChangeSet 状态：

```text
全部成功 -> COMPLETED
部分成功 -> PARTIAL
全部失败 -> FAILED
```

## 13. API 影响

复用接口：

```text
GET  /api/operations/plans/{plan_id}
POST /api/operations/plans/{plan_id}/confirm
GET  /api/changesets/{changeset_id}
```

确认响应应返回真实 ChangeSet：

```json
{
  "id": "operation-plan-id",
  "status": "EXECUTED",
  "changeset_id": "changeset-id"
}
```

如果确认执行耗时超过同步请求合理范围，后续可转成异步 Job；第一阶段小批量可以同步执行，但必须设置批量上限和超时。

## 14. 前端规格

新增或扩展 OperationPlan 卡片，展示：

- 本次计划处理文件数量。
- 原文件名和建议文件名。
- 年份、文号、标题及其证据来源。
- READY、NEEDS_REVIEW、CONFLICT 状态。
- 原始文件尚未修改提示。
- 确认按钮和取消入口。
- 执行后的逐文件成功、失败和 ChangeSet 结果。

限制：

- `NEEDS_REVIEW` 和 `CONFLICT` 项不能显示为可执行成功项。
- 前端不能自行拼接目标路径。
- 前端不能把最终文本回复解析成计划数据，必须使用结构化 OperationPlan。

## 15. 配置与部署

建议新增配置项：

```text
FILE_RENAME_ENABLED=false
FILE_RENAME_EXECUTION_ENABLED=false
FILE_RENAME_EXECUTOR=native
FILE_RENAME_POLICY_PATH=./rules/file_rename_policy.json
FILE_RENAME_MAX_BATCH_SIZE=20
FILE_RENAME_EXECUTION_TIMEOUT_SECONDS=60
F2_BINARY_PATH=f2
F2_EXPECTED_VERSION=<固定测试版本>
```

第一版仅使用 `FILE_RENAME_EXECUTOR=native`；F2 配置项为第二次迭代预留，不作为第一版部署前置条件。

部署要求：

- 开发和测试默认关闭真实执行。
- 服务器先启用建议生成，再启用 Native 执行。
- F2 二进制及许可证文件进入离线 ZIP 部署包。
- 启动检查只报告 F2 是否可用，不自动安装或联网下载。
- F2 不可用且配置为 native 时，不应影响服务启动。

## 16. 测试计划

### 16.1 单元测试

- 四位年份和冲突年份识别。
- 多种中文文号格式识别。
- 正文标题、原文件名 fallback 和噪声词清理。
- 非法字符、Unicode、超长文件名和扩展名保留。
- 缺失字段进入 NEEDS_REVIEW。
- RenamePolicy schema 校验。

### 16.2 服务测试

- 批量建议逐文件隔离。
- 受管文件快照和 document_pages 复用。
- 旧版 `.xls` 采用 `xlrd>=2.0.1 -> LibreOffice 可选转换 -> 结构化文件名回退`；当前部署不强制安装 LibreOffice，后续出现 xlrd 兼容性缺口时再启用。
- READY 项创建真实 OperationPlan。
- 计划 before/after、SHA-256 和证据完整。
- 隐藏文件和目录被拒绝。

### 16.3 执行器契约测试

- 第一版验证 Native preview 和 execute 契约。
- 第二次迭代验证 Native 和 F2 preview 输出同一结构。
- 第二次迭代验证 Native 和 F2 execute 输出同一结构。
- 目标冲突不覆盖文件。
- 自动建议冲突由后端在 OperationPlan 创建前分配中文版本后缀；F2 不得自行修改目标名。
- 源文件缺失、变化和路径越界被拒绝。
- 部分失败返回 PARTIAL。
- 数据库失败触发文件名补偿。

F2 测试使用 fake subprocess；只有显式 integration 标记才调用本机 F2 二进制。

### 16.4 API 与 Agent 测试

- 重命名请求生成 `SUGGEST_RENAME` 计划。
- 未确认前文件不变化。
- 非拥有者不能确认计划。
- 重复确认返回 409。
- 确认后返回真实 changeset_id。
- ToolInvocation 正确记录 operation_plan_id 和 changeset_id。
- 对话历史恢复后仍可展示计划和执行结果。

### 16.5 端到端测试

```text
输入：按年份、文号和正文标题重命名党办目录下这批文件
断言：生成逐文件建议和 WAITING_CONFIRMATION 计划
断言：文件系统尚未变化
操作：确认计划
断言：文件真实重命名
断言：managed_files 已更新
断言：ChangeSet 包含逐文件 FILENAME_CHANGED
断言：聊天回执展示成功和失败明细
```

## 17. 开发完成定义

满足以下条件才算完成：

1. 所有字段都能追溯到正文、文件名或明确元数据。
2. 未确认计划不会修改文件。
3. 用户确认后执行真实文件重命名，而不是只修改计划状态。
4. 路径越界、覆盖、隐藏文件和目录重命名均被拒绝。
5. `managed_files`、OperationPlan、ToolInvocation 和 ChangeSet 保持一致。
6. 批量部分失败有逐文件状态和 PARTIAL 回执。
7. 第一版只要求 Native 通过完整执行器契约测试；F2 契约测试属于第二次迭代。
8. 前端只消费结构化计划和 ChangeSet。
9. API 全量测试和前端构建通过。

## 18. 已确认的产品决策

1. 默认分隔符使用 `_`。
2. 缺少文号的普通材料允许使用 `{year}_{title}` 降级模板。
3. `NEEDS_REVIEW` 项阻止批次创建部分 OperationPlan，直到用户补齐名称或明确排除。
4. 第一版只实现 Native 执行器，F2 放到第二次迭代。
5. 普通 `user` 可以创建并确认自己的 OperationPlan，但不能确认其他用户的计划。
