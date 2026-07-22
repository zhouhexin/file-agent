# 文件智能重命名实施计划

> 历史文档：其中受管原始目录原地重命名和上传暂存重命名方案已由三层文件生命周期方案取代。当前只允许 `RENAME_WORKING_COPIES`，以 `docs/managed-original-working-copy-trash-implementation-plan.md` 为准。

## 1. 文档状态

- 状态：受管文件闭环和上传附件临时存储重命名已实现。
- 当前动作：后续补充附件分类、受管目录选择和正式归档。
- 目标能力：根据“年份 + 文号 + 正文标题”生成文件名建议，并在用户确认 OperationPlan 后对受管目录文件执行真实重命名。
- 详细开发规格：`docs/file-rename-development-spec.md`。

## 2. 最终技术决策

本功能结合 `tfeldmann/organize` 和 F2 的设计，但不把文件操作控制权交给外部工具：

- 借鉴 organize 的规则、过滤、模板、模拟和冲突处理模型。
- 不直接依赖 organize，不开放其 Shell、Python、删除和任意移动能力。
- 定义项目自有的安全 `RenamePolicy`，只允许受控模板和字段。
- 定义统一 `RenameExecutor` 接口。
- 第一阶段默认使用项目自有 `NativeRenameExecutor`。
- F2 放入第二次迭代，作为可选 `F2RenameExecutor`，用于较大批量的预演、冲突检查和执行。
- Planner、Tool Registry、OperationPlan、路径校验、数据库同步和 ChangeSet 仍由 File Agent 控制。

## 3. 功能范围

第一版包含：

- 对受管目录中的普通文件生成重命名建议。
- 从正文、解析结构和原文件名提取年份、文号、正文标题。
- 支持批量文件，每个文件独立返回 READY、NEEDS_REVIEW 或 FAILED。
- 生成真实、可查询的 OperationPlan。
- 用户确认后执行重命名。
- 更新 `managed_files` 索引。
- 写入 `ChangeSet` 和 `FILENAME_CHANGED`。
- 文件系统或数据库失败时执行补偿或记录部分失败。
- 前端展示逐文件 before/after、字段证据、风险和执行状态。

第一版不包含：

- 不把聊天上传附件直接分类或归档到受管目录；当前只允许确认后在 Document 私有临时目录改 basename。
- 不重命名目录。
- 不覆盖同名目标文件。
- 不覆盖冲突文件；自动建议冲突先保留原上传文件名并请求用户决定，只有用户明确选择同时保留后才允许在后续确认计划中分配版本后缀。
- 不执行移动、删除或任意路径写入。
- 不允许 LLM 直接生成文件路径、Shell 命令或 F2 参数。
- 不依赖 Neo4j、Docling 或自动 Skill 演化才能运行。

## 4. 默认命名策略

默认模板：

```text
{year}_{document_number}_{title}{extension}
```

示例：

```text
2026_校发〔2026〕12号_关于做好奖学金评审工作的通知.pdf
```

默认规则：

- 年份必须是四位数字。
- 文号保留完整机关代字、年份和序号。
- 标题移除重复年份、文号、扩展名和无意义装饰字符。
- 扩展名沿用源文件并规范为小写。
- 文件名不能包含路径分隔符和跨平台非法字符。
- 隐藏文件默认不参与重命名。
- 文件名最长默认为 240 bytes，为常见文件系统限制预留空间。
- 年份、文号和标题完整时使用 `{year}_{document_number}_{title}{extension}`。
- 普通材料确实没有文号、但年份和标题可靠时，降级使用 `{year}_{title}{extension}`。
- 没有年份或日期、但正文标题可靠时，降级使用 `{title}{extension}`；该标题必须来自正文或结构化文档，不能只取原文件名。
- 同一目录同一批次出现相同归一化标题时，扩展名不参与标题分组；如果组内每个文件都有完整日期，使用 `{YYYYMMDD}_{title}{extension}` 区分。
- 完整日期优先取正文或结构化落款日期，其次取安全快照保留的原始文件名；文件名存在多个日期时取最后出现的版本/提交日期。
- 同标题文件日期相同或日期不完整时，继续使用 `_第二版`、`_第三版` 规则，禁止覆盖。
- 正文标题缺失、字段冲突或证据不足时，状态进入 `NEEDS_REVIEW`。
- `READY` 文件独立进入可勾选 OperationPlan；`NEEDS_REVIEW` 文件单独展示并继续通过对话确认名称，不阻止其他文件执行。

## 5. 总体链路

```text
用户消息
-> 后端解析明确上传附件 document_ids 或受管文件范围
-> 上传附件直接生成/复用 document_pages；受管文件创建或复用安全快照和 document_pages
-> generate-rename-suggestions 提取年份、文号、正文标题
-> RenamePolicy 规范化并校验目标名称
-> RenameExecutor.preview 或安全本地预演
-> operation-plan-create 持久化 before/after 和证据
-> 前端展示待确认计划
-> 用户确认
-> confirmed-file-action 重新校验源文件和计划
-> NativeRenameExecutor 或 F2RenameExecutor 执行
-> 更新 managed_files
-> 写入 ChangeSet / FILENAME_CHANGED
-> 返回逐文件执行回执
```

## 6. 开发阶段

### 阶段 1：建议生成与安全规则

- 新增 `file_rename` 模块和 Pydantic schema。
- 新增安全 `RenamePolicy` 配置。
- 实现年份、文号、正文标题提取。
- 实现文件名规范化、长度校验和冲突预检。
- 增加 `generate-rename-suggestions` Tool。
- 批量返回逐文件建议和证据。

验收结果：只生成建议，不修改任何文件。

### 阶段 2：真实 OperationPlan

- 将 `operation-plan-create` 从占位 Tool 改为真实持久化调用。
- 扩展 OperationPlan item，保存 managed file、快照 Document、源指纹、before/after 和证据。
- Agent 响应返回真实 `operation_plan_id`。
- 前端增加重命名计划卡片。

验收结果：计划状态为 `WAITING_CONFIRMATION`，文件仍未变化。

### 阶段 3：Native 执行闭环

- 实现 `NativeRenameExecutor`。
- 重构确认服务，先记录确认，再进入 `EXECUTING`，完成后更新最终状态。
- 确认执行前重新校验路径、文件指纹、目标冲突和计划归属。
- 更新 `managed_files`。
- 创建真实 ChangeSet 和逐文件 `FILENAME_CHANGED`。
- 增加失败补偿和 `PARTIAL` 状态。

验收结果：用户确认后受管文件完成真实重命名，可通过原计划和 ChangeSet 追溯。

### 第二次迭代：F2 可选适配器

- 实现 `F2RenameExecutor`。
- 后端生成临时 CSV，不接受用户或 LLM 提供 CSV。
- 先执行 F2 dry-run 和 JSON 输出校验。
- dry-run 结果必须与 OperationPlan 完全一致。
- 确认后使用 `-x` 执行并解析逐文件结果。
- 禁止覆盖、自动冲突改名、隐藏文件和目录重命名选项。
- F2 不可用时按配置失败或回退 Native 实现。

验收结果：同一组契约测试同时通过 Native 和 F2 两个执行器。

### 阶段 5：前端与反馈

- 展示旧名称、新名称、三个字段和对应证据。
- 支持批量计划逐文件查看状态。
- `NEEDS_REVIEW` 项不允许直接确认执行。
- `NEEDS_REVIEW` 项不进入当前 OperationPlan，也不阻止 READY 项；前端默认勾选 READY 项，用户可取消勾选排除文件。
- 展示成功、失败、跳过和未修改原件说明。
- 为后续用户修正年份、文号和标题保留反馈入口。

## 7. 上线策略

- 默认 `FILE_RENAME_EXECUTOR=native`。
- 第一版只实现和启用 Native 执行器。
- F2 通过显式配置开启，不自动探测并切换生产执行器。
- 首次上线只允许测试受管根目录或灰度目录。
- 保留功能开关，可关闭真实执行但继续生成建议。
- 先验证小批量，再逐步提高单次文件上限。
- F2 版本必须固定并纳入离线部署包，不使用运行时在线下载。

## 8. 最终验收标准

- 年份、文号、标题均有来源和证据。
- LLM 无法直接调用文件系统或构造执行命令。
- 未确认时文件绝不发生变化。
- 确认时发现源文件变化或目标冲突会拒绝执行。
- 不允许越过受管根目录边界。
- 隐藏文件和目录不会被直接重命名；上传附件只能通过确认后的私有临时存储执行器改 basename。
- 每个成功文件都有 `FILENAME_CHANGED` ChangeItem。
- 批量任务中单文件失败不会丢失其他文件的执行结果。
- Native 和 F2 执行器输出相同的结构化结果契约。
- 普通 `user` 可以创建并确认自己的重命名 OperationPlan，但不能确认其他用户的计划。
- API、Agent Runtime 和前端历史记录能够恢复并展示 OperationPlan 与 ChangeSet。
