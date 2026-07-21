# F2 批量重命名执行器实施计划

> 历史方案：F2/Native 受管原始目录原地重命名执行器不再接入 Agent Runtime，不得用于确认新的文件操作计划。

## 1. 文档状态

- 状态：执行中。
- 目标版本：F2 v2.2.2 候选版本，完成真实 CLI 契约验证后固定。
- 适用范围：受管目录中已经生成并由创建用户确认的 `RENAME_FILES` OperationPlan。
- 默认执行器：`native`。
- F2 作用：批量预演、冲突检测和执行已经确定的 `before -> after` 映射。

本计划是 F2 第二阶段开发的执行依据。开发必须按本文阶段顺序进行；阶段退出条件未满足时，不得提前把 F2 接入生产确认接口。

## 2. 不变边界

- Docling、现有解析器和 OCR 负责读取文件内容。
- `FilenameMetadataExtractor` 负责提取年份、文号和标题。
- `FilenameBuilder` 负责生成目标文件名。
- F2 不理解正文、不生成名称、不调整 OperationPlan。
- Planner、LLM 和用户不得提供 F2 参数、CSV、Shell 命令或绝对路径。
- 未确认 OperationPlan 时不得执行 `f2 -x`。
- F2 只能处理同一受管根目录内、同一目录 basename 变化的普通文件。
- 隐藏文件、目录、跨目录移动、扩展名变化和覆盖继续禁止。
- Native 执行器必须保留，并与 F2 使用同一请求和结果契约。
- F2 undo 不代替项目补偿、ChangeSet 和 ToolInvocation。

## 3. 目标调用链

```text
受管文件正文
-> Docling / 现有解析器
-> 年份、文号、标题和证据
-> FilenameBuilder
-> OperationPlan
-> 用户确认
-> ConfirmedRenameService 公共批次校验
-> RenameExecutorFactory
   |- NativeRenameExecutor
   `- F2RenameExecutor
       -> 私有 CSV
       -> F2 dry-run JSON
       -> 与 OperationPlan 完全比对
       -> F2 -x
       -> 文件系统结果复核
-> managed_files
-> ChangeSet / ChangeItem / ToolInvocation
```

## 4. 统一数据契约

新增 `RenameBatchItem`：

```text
managed_file_id
before_relative_path
after_relative_path
source_sha256
```

新增 `RenameBatchRequest`：

```text
root_path
operation_plan_id
items
timeout_seconds
```

扩展 `RenameExecutionItem`：

```text
managed_file_id
before_relative_path
after_relative_path
status
error_code
error_message
```

扩展 `RenameBatchResult`：

```text
executor
executor_version
preview_digest
status
matched_count
completed_count
failed_count
duration_ms
items
```

所有路径进入持久化结果和日志前必须转换为受管根目录相对路径。

## 5. 阶段计划

### 阶段 0：F2 CLI 契约基线

任务：

- 检查本机是否存在 F2。
- 验证候选版本的 `--version` 输出。
- 确认 CSV 列格式、UTF-8、中文文件名、逗号和引号转义。
- 确认 `--json` 在 dry-run 和 `-x` 下的结构。
- 确认冲突、源文件不存在和无匹配时的退出码与 stderr。
- 将真实输出脱敏后保存为测试 fixture；本机无 F2 时使用官方契约 fixture，并将真实集成测试标记为 skipped。

退出条件：

- 明确固定版本、命令参数、CSV 和 JSON schema。
- 无法验证的字段不得由业务代码猜测，必须由兼容解析器和严格校验隔离。

### 阶段 1：执行器协议和 Native 兼容重构

新增：

```text
apps/api/app/modules/file_rename/executor_protocol.py
apps/api/app/modules/file_rename/executor_factory.py
apps/api/app/modules/file_rename/batch_validator.py
```

任务：

- 定义 `RenameExecutor` Protocol。
- 把 Native 执行器适配为批次请求和批次结果。
- 抽取隐藏文件、路径边界、同目录、扩展名和目标冲突公共校验。
- 执行器工厂只接受 `native` 和 `f2` 枚举。
- 默认仍返回 Native 执行器。

退出条件：

- 原有 Native API、OperationPlan、ChangeSet 行为不变。
- Native 全部既有测试通过。

### 阶段 2：F2 dry-run 适配器

新增：

```text
apps/api/app/modules/file_rename/f2_executor.py
apps/api/app/modules/file_rename/f2_report_parser.py
```

任务：

- 后端使用标准库 `csv` 生成私有临时 CSV。
- 使用参数数组和 `shell=False` 调用 F2。
- 设置 `cwd` 为受管根目录，设置受控 `HOME` / `XDG_DATA_HOME` / `NO_COLOR`。
- 限制 timeout、stdout 和 stderr 摘要长度。
- 解析 JSON 并转换为统一 `RenameBatchResult`。
- 对输出数量、源路径、目标路径和状态进行严格比对。
- 计算稳定 `preview_digest`。
- dry-run 不得修改文件。

禁止参数：

```text
--allow-overwrites
--fix-conflicts
--fix-conflicts-pattern
--hidden
--include-dir
--only-dir
--target-dir
--clean
--recursive
--undo
```

退出条件：

- dry-run 输出与 OperationPlan 不一致时返回 `F2_PREVIEW_MISMATCH`。
- F2 缺失、版本错误、超时和非 JSON 输出均返回结构化错误。

### 阶段 3：F2 批量执行和补偿

任务：

- 确认后重新校验路径、SHA-256 和目标冲突。
- 再次执行 dry-run 并比较 preview digest。
- 只有一致时增加 `-x`。
- 执行后逐项检查源路径消失、目标路径存在、大小和 SHA-256 不变。
- 数据库更新失败时按完成顺序逆序补偿 `after -> before`。
- 不调用 F2 全局 undo。
- 单项失败返回 `PARTIAL`，不得丢失已完成项目的审计结果。

退出条件：

- 文件系统、`managed_files`、ChangeSet 和 OperationPlan 状态一致。
- 补偿失败可审计，不伪造原文件已恢复。

### 阶段 4：确认服务和审计接入

修改：

```text
apps/api/app/modules/file_rename/execution_service.py
apps/api/app/modules/operations/service.py
```

任务：

- 确认服务按受管根目录构造批次。
- 锁定相关 `managed_files`，避免重叠计划并发执行。
- 执行器由请求级工厂构造，不进入 AgentGraphState。
- OperationPlan 保存执行器名称、版本和 preview digest。
- ToolInvocation 保存执行器摘要，不保存绝对路径、CSV 和完整 stderr。
- 成功项写 `FILENAME_CHANGED`，失败项写 `FILE_OPERATION_FAILED`。

退出条件：

- 只有计划创建用户可以确认。
- 重复确认返回 409。
- Native 和 F2 输出同一 API 契约。

### 阶段 5：配置、部署和健康检查

配置：

```text
FILE_RENAME_EXECUTOR=native
FILE_RENAME_MAX_BATCH_SIZE=20
FILE_RENAME_EXECUTION_TIMEOUT_SECONDS=60
F2_BINARY_PATH=f2
F2_EXPECTED_VERSION=2.2.2
F2_FALLBACK_TO_NATIVE=false
F2_STDOUT_MAX_BYTES=1048576
```

任务：

- 配置为 `native` 时，F2 缺失不影响启动。
- 配置为 `f2` 时，版本或二进制校验失败必须关闭执行，不静默回退。
- 增加执行器健康检查服务和结构化日志。
- 固定 F2 版本、许可证、下载地址和 SHA-256 manifest。
- 离线部署包携带目标平台二进制，不在运行时联网下载。
- 更新 `agent.md`、Runbook、部署文档和 Skill 文档。

退出条件：

- Native 默认配置可直接升级。
- F2 可以按受管目录灰度启用并快速回退 Native。

### 阶段 6：测试和灰度

单元测试：

- CSV 转义和临时目录清理。
- F2 版本、超时、退出码、stderr 和 JSON 校验。
- 输出数量和 before/after 不一致。
- 隐藏文件、路径越界、目录、扩展名变化和目标冲突。
- Native/F2 契约一致。

服务测试：

- 未确认前不执行 `-x`。
- 源文件或 SHA-256 变化时计划失效。
- 成功、失败、PARTIAL 和补偿。
- 数据库行锁和重叠计划。
- ChangeSet、ToolInvocation、历史消息和前端回执。

集成测试：

- 默认使用 fake subprocess。
- 只有 `RUN_F2_INTEGRATION_TESTS=true` 时调用真实 F2。
- macOS 本地和 Linux 部署环境分别验证固定二进制。

退出条件：

- 后端全量测试通过。
- 真实 F2 dry-run 和 execute smoke test 通过。
- 测试受管目录灰度通过后才能把生产配置切换为 `f2`。

## 6. 错误码

```text
F2_NOT_AVAILABLE
F2_VERSION_MISMATCH
F2_TIMEOUT
F2_EXECUTION_FAILED
F2_INVALID_JSON
F2_PREVIEW_MISMATCH
F2_UNEXPECTED_FILE
F2_RESULT_INCOMPLETE
F2_POSTCHECK_FAILED
F2_COMPENSATION_FAILED
```

## 7. 验收标准

- F2 不参与文件理解和目标名称生成。
- OperationPlan 未确认时文件不变化。
- dry-run 与 OperationPlan 必须逐项一致。
- F2 不覆盖文件、不自动增加冲突序号、不处理隐藏文件和目录。
- 执行前后均验证 SHA-256。
- F2 故障不影响 Native、文件读取、Docling、OCR 和分类。
- 每个文件都有结构化执行结果和 ChangeItem。
- 任何数据库或文件系统不一致都有明确失败和补偿记录。
- F2 版本和二进制进入离线部署审计，不运行时下载。

## 8. 当前执行记录

- [x] 计划文档已建立。
- [x] 阶段 0：CLI 契约基线。官方参数边界已确认；本机未安装 F2，真实版本输出、CSV/JSON 样本和集成 smoke test 按计划保留为部署环境验收项，业务代码不得把未验证字段视为稳定契约。
- [x] 阶段 1：执行器协议和 Native 兼容重构。
- [x] 阶段 2：F2 dry-run 适配器。
- [x] 阶段 3：批量执行和补偿。
- [x] 阶段 4：确认服务和审计接入。
- [x] 阶段 5：配置、部署和健康检查代码与文档。F2 二进制及其平台 SHA-256 清单由离线发布包提供，本仓库不运行时下载二进制。
- [ ] 阶段 6：自动化测试已完成；本机缺少 F2，真实 F2 smoke test 和服务器灰度待部署环境执行。

本轮自动化验证结果：

```text
后端：266 passed, 1 skipped
跳过：test_real_f2_dry_run_and_execute_smoke（本机未安装 F2，需显式 RUN_F2_INTEGRATION_TESTS=true）
前端：npm run build 通过
静态检查：Python compileall 与 git diff --check 通过
```
