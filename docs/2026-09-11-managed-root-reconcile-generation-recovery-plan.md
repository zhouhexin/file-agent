# 受管目录协调代次与失败任务恢复修复方案

## 1. 背景与结论

2026-09-11 在清空工作副本和分类领域数据、但保留受管原件和可复用解析/索引结果后，服务重新启动。
启动协调任务 `RECONCILE_MANAGED_ROOT` 被标记为 `COMPLETED`，但没有产生新的有效扫描、分析、分类或工作副本任务。

排查确认：Worker 和 Scheduler 均正常运行；缺陷位于协调任务创建子扫描任务时的代次计算及结果校验，而非 Worker 轮询。

现场证据：

- 当前 `working_copies`、`document_categories`、`document_category_suggestions`、`document_classification_runs` 和 `document_organization_decisions` 均为 0。
- 当前队列没有 `PENDING` / `RUNNING` 任务。
- 2026-09-11 15:16 的协调任务引用了历史失败扫描 `06733311-8bd5-4607-8481-877043db98d6`；该扫描创建于 11:33，状态仍为 `FAILED`。
- 协调结果中 `scan_reused=false`，但 `scan_job_id` 实际指向上述终态失败任务。这是状态与结果不一致。

## 2. 根因

`FileLifecycleJobProcessor._reconcile_managed_root` 的实现只从**当前协调任务自身**的 `result_json.scan_generation` 计算下一代次。

新的 `RECONCILE_MANAGED_ROOT` 任务没有历史结果，因此每次都从代次 1（本现场为代次 2）重新开始。它随后按下列键创建扫描：

```text
managed-root-scan:<root_id>:<scan_generation>
```

`FilesystemJobQueue.create_job` 遇到同一个键的历史终态任务时，会原样返回该任务，除非调用方显式选择 `retry_failed` 或 `reuse_completed`。协调处理器没有检查返回任务是否确实为新建或活动任务，仍把父协调任务标记为完成。

现有单元测试只覆盖了“重复处理同一个父协调任务”的场景；该父任务自身已有上一轮结果，因此能得到代次 2。测试没有覆盖“创建一个新的父协调任务、根下已有终态扫描任务”的真实重启场景。

## 3. 修复目标与不变量

1. 同一个受管根在任意时刻至多有一个 `PENDING` 或 `RUNNING` 的 `SCAN_MANAGED_ROOT`。
2. 没有活动扫描时，每一个新的协调周期必须创建一个从全根历史中递增的全新代次。
3. `COMPLETED` / `FAILED` 扫描是不可变审计记录；自动协调不得重置或覆盖它们。
4. 父协调任务只有在成功关联到活动扫描，或已明确确认子扫描成功完成时，才可以标记为 `COMPLETED`。
5. 失败、去重冲突、结果状态不匹配必须使父任务显式失败，且日志、事件、API 状态可诊断。
6. 在“重置分类/工作副本，保留正文解析和索引”的受控重建后，系统必须能显式调度分类与工作副本重建，不能仅依赖文件内容变化。

## 4. 实现设计

### 4.1 从根级历史计算扫描代次

修改 `_reconcile_managed_root`：

1. 先按 `root_id + job_type=SCAN_MANAGED_ROOT + status in (PENDING, RUNNING)` 查询活动扫描并加锁/采用事务内一致读取。
2. 若存在活动扫描：只复用该任务，返回其 `payload_json.scan_generation`，并记录 `scan_reused=true`、`reuse_reason=ACTIVE_SCAN`。
3. 若不存在活动扫描：从该根全部历史扫描的 `payload_json.scan_generation` 中取得最大有效整数；缺失、非法或旧数据按 0 处理；新代次为 `max_generation + 1`。
4. 使用新代次生成去重键并创建任务。新键必须不等于任何历史任务键。
5. 不再从父协调任务的旧 `result_json` 推导全局代次。父任务结果仅用于记录本次关联结果。

建议抽取为独立私有函数，例如：

```python
def _next_scan_generation_for_root(self, root_id: str) -> int: ...
def _find_active_scan_for_root(self, root_id: str) -> FilesystemJob | None: ...
```

这样业务含义清晰，也便于单独测试。

### 4.2 收紧队列创建返回契约

`FilesystemJobQueue.create_job` 需要能够让调用者区分以下三种结果，不能只返回一个裸 `FilesystemJob`：

| 情形 | 允许行为 | 协调任务结果 |
|---|---|---|
| 新建键 | 创建 `PENDING` 子扫描 | 完成，`child_created=true` |
| 同键活动任务 | 复用活动任务 | 完成，`scan_reused=true` |
| 同键终态任务 | 对自动协调视为不变量破坏 | 父任务失败，不得伪装完成 |

实现可选方案：

- 为 `create_job` 增加结构化返回值（`job`、`created`、`reused`、`reuse_reason`）；或
- 保持接口兼容，并在协调处理器内预先验证新键不存在，再调用创建方法，随后断言返回的任务 ID、状态和 payload 代次均符合预期。

推荐第一种。它能同时消除其他调用方把终态去重任务误当成新任务的风险。

### 4.3 父子状态一致性保护

在标记 `RECONCILE_MANAGED_ROOT` 为 `COMPLETED` 前强制校验：

```text
child.root_id == parent.root_id
child.job_type == SCAN_MANAGED_ROOT
child.status in {PENDING, RUNNING}
child.payload.scan_generation == result.scan_generation
child.deduplication_key == managed-root-scan:<root_id>:<generation>
```

任一条件不满足时：

- 父任务标记 `FAILED`；
- 写入稳定错误码 `RECONCILE_CHILD_SCAN_INVALID`；
- `error_message` 包含父任务、子任务、子状态、预期代次、实际代次和去重键摘要；
- 创建一条 `filesystem_job_events` ERROR 事件；
- 不创建替代分类或工作副本任务，避免在未知状态下并发处理。

### 4.4 并发与唯一性

多个 Scheduler/API 启动钩子可能同时为同一根创建协调任务。实现必须保证：

- 在 PostgreSQL 下，对该根扫描任务的活动查询及新一代任务创建处于同一事务，并针对 `managed_roots` 行使用 `SELECT ... FOR UPDATE`（或等价的 advisory lock）。
- 锁内再次检查活动扫描，防止两个协调任务各自计算出同一代次。
- 保留现有 `deduplication_key` 唯一约束，作为最终防线；若触发唯一冲突，重新读取活动任务。若读到终态任务，则让父任务失败并记录不变量错误，而不是复用。

### 4.5 受控重建语义

“清空工作副本和分类领域数据、保留解析/索引”的运维操作不是普通目录变更。重置脚本在提交清理事务后必须：

1. 为每个受影响的受管根创建一个明确的 `REBUILD_MANAGED_CLASSIFICATION`（推荐新任务类型）或等价的 `REFRESH_MANAGED_SOURCE_CLASSIFICATION` 批量任务；任务 payload 必须包含 `reset_run_id`、根 ID、taxonomy 版本和重建范围。
2. 该任务以现有 `document_pages` / extraction run 作为优先输入，重建分类建议、正式 PRIMARY、组织决策、路径记录和工作副本；不可复用的信息才重新解析。
3. 若产品策略要求“仅新导入文件触发分类”，则重置脚本不得承诺旧受管文件自动重建，并必须在执行前明确提示。当前测试目的要求重建，因此采用前者。
4. 每个受影响根写入一个可审计 ChangeSet/重置操作记录及逐文件结果；不修改受管原件。

这使重置后的恢复不依赖扫描是否判定源文件有变化，也避免把运维意图隐式塞进普通扫描逻辑。

## 5. 数据修复与发布步骤

### 5.1 代码发布前

1. 先添加下述回归测试，使现有实现稳定失败。
2. 实现根级代次、返回契约和父子校验。
3. 运行定向测试与并发测试。
4. 将诊断字段加入 JSONL 日志和任务事件。

本修复仅改变任务调度代码和测试；正常情况下无需数据库迁移。若新增 `REBUILD_MANAGED_CLASSIFICATION` 仅复用现有 `filesystem_jobs`，同样无需迁移。

### 5.2 修复后对当前环境恢复

不得删除历史失败扫描记录。修复发布后：

1. 确认所有 Worker/Scheduler 使用同一新版本。
2. 为 `test_library` 创建新的 `RECONCILE_MANAGED_ROOT`（或直接创建受控重建任务）。
3. 验证新子扫描的 `scan_generation` 大于该根历史最大值，且不是 `06733311-...`。
4. 跟踪扫描、源侧分析、分类、工作副本物化的逐级任务数和失败数。
5. 验证工作副本、分类运行、分类建议、正式 PRIMARY 和组织决策重新出现；抽样核对正文证据、PRIMARY 与物理“其他”落位规则。

## 6. 必须新增的测试

### 单元测试

1. **新父任务 + 历史完成扫描**：新父协调任务必须创建最大代次 + 1 的新 `PENDING` 扫描。
2. **新父任务 + 历史失败扫描**：同上，历史 `FAILED` 的状态、错误和尝试次数保持不变。
3. **新父任务 + 历史终态键碰撞**：模拟异常键碰撞；父任务必须 `FAILED(RECONCILE_CHILD_SCAN_INVALID)`，绝不能 `COMPLETED`。
4. **同根活动扫描**：两个父协调任务只关联同一活动子扫描，不产生第二条活动扫描。
5. **坏/缺失 generation**：历史 payload 缺失、字符串、负数时安全忽略，仍生成正确的新代次。
6. **并发协调**：两个事务/两个 session 并发执行，最终只有一条活动扫描，且两父任务的结果可解释。
7. **队列返回契约**：对新建、活动复用、终态碰撞分别返回正确来源和原因。

### 集成测试

1. 启动 API、Scheduler 和 Scan Worker；预置失败扫描后重启服务，验证自动协调创建更高代次扫描。
2. 扫描完成后，验证分类/工作副本重建任务实际可领取并完成，不能只检查父协调任务状态。
3. 执行“保留解析索引的选择性重置”，验证显式重建任务为全部受影响文件生成分类和工作副本，且解析页/索引复用率符合预期。
4. 反复重启三次，验证历史扫描数递增、没有终态任务被重置、同一时刻无重复活动扫描。

### 验收查询

按根查询应同时展示代次、状态和父子关联：

```sql
SELECT id, job_type, status, created_at,
       payload_json->>'scan_generation' AS scan_generation,
       payload_json->>'reconcile_job_id' AS parent_reconcile_job_id,
       deduplication_key, error_message
FROM filesystem_jobs
WHERE root_id = :root_id
  AND job_type IN ('RECONCILE_MANAGED_ROOT', 'SCAN_MANAGED_ROOT')
ORDER BY created_at;
```

通过标准：最新协调任务关联的扫描必须是 `PENDING`、`RUNNING` 或已由本轮成功完成的任务；不得引用历史 `FAILED` 任务并把自身标记为 `COMPLETED`。

## 7. 可观测性与告警

每次协调完成事件至少记录：`root_id`、`reconcile_job_id`、`scan_job_id`、`scan_generation`、`child_created`、`scan_reused`、`reuse_reason`、`child_status`。

增加以下告警/健康检查：

- `RECONCILE_MANAGED_ROOT=COMPLETED` 但子扫描为 `FAILED`；
- 有已启用受管根、源修订存在、分类/工作副本均为空，且超过配置的恢复窗口；
- 同一根存在超过一条活动扫描；
- 协调任务连续多次没有创建或复用有效子扫描。

运维页面应把“Worker 在线、队列空闲”和“目标重建完成”分开显示，避免将进程健康误认为业务完成。

## 8. 非目标

- 不删除或篡改历史 `FAILED` / `COMPLETED` 任务审计记录。
- 不自动修改受管原件。
- 不将分类证据不足的文件改为“待分类”或“待复核”；仍按既定策略落入 `system.other`（“其他”）。
- 不以无边界的全库重扫替代按根受控协调或重建。
