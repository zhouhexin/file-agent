# File Rename Skill

## 触发条件

用户要求为服务器受管目录中的文件生成重命名建议，或确认此前生成的重命名计划。

## 输入输出 Schema

输入只允许受管逻辑目录、相对目录前缀、扩展名和文件名过滤条件。输出包含年份、文号、正文标题、字段证据、建议文件名、状态和 OperationPlan ID。

## Tool 白名单

- `generate-rename-suggestions`
- `confirmed-file-action`

## Open Source Backing

规则化批处理思想参考 `tfeldmann/organize`；批量改名交互参考 F2。第一版不直接集成其执行器，使用项目自研 Native 执行器，避免绕过受管目录、OperationPlan 和 ChangeSet 边界。

## 处理步骤

1. 确定性解析受管目录范围和元数据过滤条件。
2. 创建或复用不可变快照，从 `document_pages` 读取完整正文。
3. 提取年份、文号和正文标题，并保存证据。
4. 使用 `_` 生成 `{year}_{document_number}_{title}`；普通材料缺文号时降级为 `{year}_{title}`。
5. 只把 `READY` 项加入 OperationPlan，`NEEDS_REVIEW` 和冲突项跳过。
6. 计划创建者确认后，由 Native 执行器逐文件改名并写 ChangeSet。

## 失败与降级

- 缺少年份或标题：`NEEDS_REVIEW`，不进入执行批次。
- 缺少文号：普通材料使用降级模板。
- 目标冲突、源文件变化、路径越界或目录未授权：拒绝执行当前项，其他项继续。
- 失败项必须写结构化错误和 ChangeItem，不得伪造成功。

## 验收标准

- 确认前源文件不变。
- 普通用户只能确认自己的计划。
- 执行后索引路径与文件系统一致。
- 每个成功项存在 `FILENAME_CHANGED`，每个失败项存在失败审计。

## 禁止事项

- 不允许重命名上传原件。
- 不允许 LLM 提供绝对路径或直接执行文件操作。
- 不允许覆盖已存在目标文件。
- 不允许执行 `NEEDS_REVIEW` 项。
