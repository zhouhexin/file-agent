# Taxonomy 任意版本兼容与源分类自动刷新方案

## 1. 问题与目标

当前受管源文件只要 `ManagedFileRevision.status == "READY"`，扫描和源分析入口就会直接跳过。
`ANALYZE_MANAGED_FILE_REVISION` 的幂等键也只有 revision ID：

```text
managed-source-analysis:{revision_id}
```

因此 taxonomy 从任意旧版本升级到任意新版本后，源文件仍会复用旧的
`DocumentClassificationRun`。首次物化时，`CategoryOrganizationPathResolver` 又会严格要求分类
运行的 `taxonomy_key + taxonomy_version` 与当前 taxonomy 完全一致。最终表现为：分类 ID 和证据正确，
但路径解析返回 `TARGET_PATH_UNAVAILABLE`，文件进入 `.internal/neutral/`。

本方案目标不是兼容某个固定的 v3，而是建立通用规则：

```text
当前分类运行时身份
= taxonomy_key
+ taxonomy_version
+ classifier_version
```

只要其中任意一项变化，就只复用已持久化的 `document_pages`，重新生成分类建议；不重新读取、转换或
解析原始文件。旧分类运行继续保留用于审计，不覆盖、不删除。

## 2. 不应采用的修复

不要删除 `organization_path.py` 中的 taxonomy 版本一致性校验，也不要写死允许
`2026-07-v2 -> 2026-08-v3`。taxonomy 升级可能同时修改节点语义或 `organization_path`，直接允许旧结果
通过会把文件移动到已经改变含义的目录。

不要只修改本次数据或只给 `school.party.union` 增加特例。问题发生在源分析新鲜度判断，任何分类节点、
任何后续 taxonomy 版本都会复现。

## 3. 建议的通用运行时身份

### 3.1 新增身份与新鲜度模块

建议新增：

```text
apps/api/app/modules/classification/freshness.py
```

提供以下对象：

```python
@dataclass(frozen=True, slots=True)
class ClassificationRuntimeIdentity:
    taxonomy_key: str
    taxonomy_version: str
    classifier_version: str

    @property
    def fingerprint(self) -> str:
        # 对三个字段做稳定 SHA-256，不能依赖 Python hash()。
        ...


class ClassificationFreshness(str, Enum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    MISSING = "MISSING"
```

身份必须动态生成：

1. `load_default_taxonomy()` 提供 `taxonomy.key` 和 `taxonomy.version`。
2. `ClassificationRuntimeFactory(settings).create(...).classifier_version` 提供当前分类器版本。
3. 幂等键使用身份指纹，不能在代码中出现 `v3`、`v4` 等固定判断。

建议提供两个函数：

```python
def current_classification_identity(
    *, db: Session, settings: Settings, user_id: str
) -> ClassificationRuntimeIdentity:
    ...


def inspect_managed_source_classification(
    *, db: Session, revision: ManagedFileRevision,
    identity: ClassificationRuntimeIdentity
) -> ClassificationFreshness:
    ...
```

新鲜度查询应基于 `revision.analysis_document_id` 上最新成功的
`DocumentClassificationRun`：

- key、taxonomy version、classifier version 全部一致：`CURRENT`。
- 存在成功运行，但身份不同：`STALE`。
- 没有成功运行：`MISSING`。

受管源分析 Document 与 revision 是一对一关系，因此这一判断不需要把本地文件路径或正文写进任务负载。

### 3.2 空候选也必须有版本完成标记

当前 `persist_document_results_classifications()` 遇到空 `categories` 会直接跳过，导致空正文、
metadata-only 或无 taxonomy 候选的文件永远没有“已按当前版本判断过”的事实。

建议调整：

```text
apps/api/app/modules/classification/classifier_service.py
apps/api/app/modules/classification/service.py
apps/api/app/modules/managed_files/source_analysis.py
```

具体要求：

1. `DocumentClassificationService.classify()` 在顶层结果中始终返回
   `taxonomy_key`、`taxonomy_version`、`classifier_version`，不能只放在 category 内。
2. `persist_document_results_classifications()` 增加默认关闭的参数，例如
   `persist_empty_run: bool = False`。
3. 只有受管源分类入口传 `persist_empty_run=True`；普通读取/总结任务继续保持当前行为，不创建空运行。
4. 空候选运行创建 `DocumentClassificationRun(status="COMPLETED")`，但不创建
   `DocumentCategorySuggestion`。这表示“当前版本已经执行且无候选”，不表示分类成功落位。
5. metadata-only 分支也必须带当前运行时身份，并持久化当前版本完成标记，防止每次扫描都重新排队。

## 4. 拆分“完整源分析”和“分类刷新”

### 4.1 调整源分析服务

修改：

```text
apps/api/app/modules/managed_files/source_analysis.py
```

在 `ManagedSourceAnalysisService` 中增加分类刷新方法，例如：

```python
def refresh_classification(
    self,
    *,
    revision_id: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    ...
```

该方法必须：

1. 校验 revision 当前有效且源文件元数据没有变化。
2. 读取 `analysis_document_id`、`analysis_document_version_id`。
3. 查询最新成功的 `DocumentExtractionRun`。
4. 调用 `ClassificationRuntimeFactory(...).create(...).classify()`。
5. 分类服务从已有 `document_pages` 读取完整正文；不得重新调用 `.doc` 转换、OCR 或文件解析器。
6. 持久化新的 `DocumentClassificationRun` 和建议，保留旧版本运行。
7. 返回动态身份和分类结果。
8. 不修改源文件，不创建新的 `DocumentVersion`，不重建 chunk/index。

现有 `analyze()` 的早退条件不能再只判断：

```python
revision.status == "READY" and revision.analysis_document_version_id
```

应改为：

```text
READY + 当前分类身份存在     -> 幂等返回 READY
READY + 分类身份过期/缺失    -> 返回 CLASSIFICATION_REFRESH_REQUIRED，或调用刷新方法
尚未完成正文解析             -> 执行现有完整源分析
```

推荐让 worker 显式处理刷新任务，避免 `analyze()` 一个方法同时承担两种耗时特征不同的工作。

## 5. 增加独立的分类刷新任务

修改：

```text
apps/api/app/modules/managed_files/worker.py
```

建议新增任务类型：

```text
REFRESH_MANAGED_SOURCE_CLASSIFICATION
queue_name = SOURCE_ANALYSIS
```

通用幂等键：

```text
managed-source-classification:{revision_id}:{identity_fingerprint}
```

其中 `identity_fingerprint` 动态来自 key、taxonomy version、classifier version。这样 taxonomy 或分类器
升级后自然生成新的任务身份，未来版本不需要修改代码。

分类刷新只复用数据库正文，成本明显低于完整源解析。队列优先级应略高于普通后台
`ANALYZE_MANAGED_FILE_REVISION`，避免旧解析积压使 taxonomy 升级后的存量文件长时间无法物化；
已存在的 PENDING 刷新任务也应通过队列服务提升优先级，但不能重置失败次数。

需要调整的具体函数：

### `_enqueue_source_analysis_jobs_for_revisions()`

不要再对所有 `revision.status == "READY"` 直接 `continue`。改为：

```text
revision 未 READY
  -> ANALYZE_MANAGED_FILE_REVISION

revision 已 READY，分类 CURRENT
  -> 不创建任务

revision 已 READY，分类 STALE/MISSING
  -> REFRESH_MANAGED_SOURCE_CLASSIFICATION
```

扫描完成后应对本次根下所有 current revision 执行这项新鲜度检查，而不只是新发现的 revision。否则
taxonomy 升级但文件本身未变化时，旧文件仍不会被发现。

### `_process_job()`

为 `REFRESH_MANAGED_SOURCE_CLASSIFICATION` 增加 handler：

1. 调用 `ManagedSourceAnalysisService.refresh_classification()`。
2. 成功后调用 `_enqueue_background_materialization_jobs_for_revisions()`。
3. 任务回执写入旧身份、新身份、分类运行 ID 和 `reused_extraction=true`。
4. 失败时按现有最多三次重试规则处理，不能把 revision 的正文解析状态改成 FAILED。

### `_enqueue_background_materialization_jobs_for_revisions()`

创建 `MATERIALIZE_WORKING_COPY` 前再次验证分类身份为 `CURRENT`。不满足时应先排分类刷新任务，不能让
旧版本分类进入物化队列。

## 6. 物化层必须保留防御校验

修改：

```text
apps/api/app/modules/file_lifecycle/service.py
```

在 `_materialize_working_copy()` 开始、复制物理文件之前，验证源分类身份：

- `CURRENT`：继续现有复用和落位。
- `STALE/MISSING`：不复制文件、不发布 neutral 文件，提交分类刷新任务并把本次物化记为
  `DEFERRED_CLASSIFICATION_REFRESH`。

刷新完成后用既有物化幂等键重新激活任务。不要通过抛普通异常反复重试，否则三次后会错误进入最终失败。

`CategoryOrganizationPathResolver` 的严格版本校验应保留。它是最后一道防线，不是本次问题的根源。

建议新增更精确的诊断原因：

```text
STALE_CLASSIFICATION_RUNTIME
```

只有真正无法解析当前 taxonomy 的 `organization_path` 时，才使用 `TARGET_PATH_UNAVAILABLE`。

## 7. 重置脚本与存量数据修复

修改：

```text
apps/api/app/scripts/reset_managed_root_working_copies.py
```

重置脚本不能无条件把全部旧 `MATERIALIZE_WORKING_COPY` 任务重新排队。对每个 revision：

1. 当前分类身份为 `CURRENT`：重新排物化任务。
2. 分类为 `STALE/MISSING`：先排 `REFRESH_MANAGED_SOURCE_CLASSIFICATION`。
3. 刷新成功后再由 worker 排物化任务。

建议再增加一个只入队、不直接处理正文的维护脚本：

```text
apps/api/app/scripts/refresh_managed_source_classifications.py
```

参数至少包括：

```text
--root-key <精确受管根>
--dry-run
--enqueue
```

脚本必须动态打印当前分类运行时身份和 STALE/MISSING 数量，不接受固定 `--target-version v3`。目标版本永远
来自当前部署加载的 taxonomy。

现有已经发布到 `.internal/neutral/` 的活动工作副本不能被后台静默移动：

- 开发测试根：停止 writers，刷新源分类后再次运行工作副本重置脚本。
- 生产或真实共享工作目录：基于当前分类生成 OperationPlan，经用户确认后移动。

## 8. 是否需要数据库迁移

推荐的最小实现不需要迁移：`DocumentClassificationRun` 已经保存 taxonomy key、taxonomy version 和
classifier version；受管源分析 Document 与 revision 一对一，可以按 `analysis_document_id` 判断新鲜度。

如果后续需要高频统计，可以再把以下字段冗余到 `ManagedFileAnalysisRun`，但不是本次修复的前置条件：

```text
classification_taxonomy_key
classification_taxonomy_version
classifier_version
classification_status
```

即使增加冗余字段，`DocumentClassificationRun` 仍是分类事实来源，冗余字段只能用于快速筛选和审计。

## 9. 必须补充的测试

### `apps/api/app/tests/test_managed_files_worker.py`

至少覆盖：

1. READY revision + 当前身份：不重复入队。
2. READY revision + 任意旧版本：创建分类刷新任务。
3. taxonomy 从模拟版本 A 改为 B 后自动生成不同幂等键，测试不能写死只支持 v3。
4. 分类刷新复用现有 extraction run，不调用文件解析器。
5. 刷新成功后才创建物化任务。
6. 空候选当前运行不会无限重复入队。

### `apps/api/app/tests/test_file_lifecycle.py`

至少覆盖：

1. 旧身份物化在文件复制前被延迟，不发布 neutral 文件。
2. 当前身份继续按 taxonomy `organization_path` 自动落位。
3. `CategoryOrganizationPathResolver` 仍拒绝旧版本直接解析。

### `apps/api/app/tests/test_file_data_reset_preserve_users.py` 或重置脚本专用测试

至少覆盖：

1. 重置后旧分类先刷新、后物化。
2. 当前分类直接重新物化。
3. 源 Document、DocumentPage、旧分类运行均被保留。

### `apps/api/app/tests/test_document_classifier.py`

至少覆盖：

1. 分类结果顶层始终返回运行时身份。
2. `persist_empty_run=True` 能保存当前版本空运行。
3. 普通读取任务默认仍不保存空分类运行。

建议使用参数化版本，例如 `version-a`、`version-b`、`future-version`，以证明逻辑与版本命名无关。

## 10. 推荐手工修改顺序

1. 新增 `classification/freshness.py` 和单元测试。
2. 让分类结果顶层返回完整运行时身份，并支持受控空运行持久化。
3. 从 `ManagedSourceAnalysisService` 拆出只读 classification refresh。
4. 增加刷新任务类型、动态幂等键和 worker handler。
5. 在物化入口增加刷新前置检查。
6. 调整重置脚本的重新排队逻辑。
7. 增加存量刷新维护脚本。
8. 先跑相关测试，再跑完整后端测试。
9. 对测试根执行 dry-run，确认旧版本数量。
10. 入队刷新；全部完成后再重置测试工作副本并重新物化。

## 11. 验收标准

- 代码中没有针对 `v2 -> v3` 或任何固定目标版本的兼容分支。
- taxonomy key、taxonomy version、classifier version 任一变化都会触发一次分类刷新。
- 文件正文解析、LibreOffice 转换、OCR、chunk 和 index 不因单纯 taxonomy 升级而重跑。
- 当前版本无候选结果不会形成无限任务循环。
- 旧分类运行保留可审计，新分类运行追加写入。
- 物化前不会再因可自动修复的版本过期而把文件发布到 neutral。
- 已经发布的活动文件不会被后台静默移动。
- `2006年度校级工会积极分子登记表戴.doc` 在刷新后可按当前 taxonomy 解析到
  `学校/党委相关/工会/`，但实现中没有该文件名或分类 ID 特例。
