# 受管目录全量工作副本同步测试方案

## 1. 测试目标

验证受管目录启动初始化调整后的完整行为：

```text
扫描受管目录
→ 为尚无活动副本的文件建立源侧修订
→ SOURCE_ANALYSIS 生成正文、摘要、分类、Chunk 和 Evidence
→ 修订 READY 后自动创建低优先级 MATERIALIZE_WORKING_COPY
→ 全部成功文件最终进入 shared/<root_key>/<源相对路径>
```

同时验证全量同步未完成期间，搜索和证据回答能够联合使用活动工作副本与未物化源侧索引；用户命中的文件复用同一物化任务并提升优先级，不阻塞回答、不重复复制、不修改受管原件。

## 2. 验收边界

- API 启动只创建持久化任务，不同步遍历或复制大目录。
- 同一文件必须先完成源侧分析，再开始工作副本物化。
- 不同文件允许由 `SOURCE_ANALYSIS` 和 `MATERIALIZE` worker 跨文件并行。
- 后台物化任务默认优先级为 `100`；用户命中后提升为 `20`。
- 全量同步和用户命中使用相同幂等键，不产生并发重复任务或重复 `WorkingCopy`。
- 同步期间的检索结果覆盖活动工作副本和已完成源侧分析的未物化文件，并按来源关系去重。
- 尚未完成源侧分析的文件不能生成正文结论；系统只能提升分析任务并进入处理中状态。
- 单文件失败不能阻塞其他文件继续分析和同步。
- 受管原件的路径、内容哈希、大小和修改时间不得改变。
- 工作副本只能写入 `WORKING_COPY_STORAGE_ROOT/shared/<root_key>/`。

## 3. 测试环境

使用独立测试受管目录和工作副本目录，不得直接用生产资料执行破坏性测试。

建议配置：

```env
MANAGED_ROOT_TEST_LIBRARY=/absolute/path/to/file-agent-test-managed-root
MANAGED_ROOT_TEST_LIBRARY_CLASSIFICATION_MODE=NONE
WORKING_COPY_STORAGE_ROOT=./storage/working-copies
MANAGED_ROOT_RECONCILE_ON_STARTUP=true
FILESYSTEM_ASYNC_JOBS_ENABLED=true
MANAGED_FILE_INITIALIZATION_MODE=source_index_first
MANAGED_SOURCE_ANALYSIS_ENABLED=true
MANAGED_SOURCE_SEARCH_ENABLED=true
MATERIALIZE_ALL_MANAGED_FILES=true
MATERIALIZE_WORKING_COPY_BACKGROUND_PRIORITY=100
MATERIALIZE_RELEVANT_FILES_AFTER_RESPONSE=true
MATERIALIZE_WORKING_COPY_PRIORITY=20
```

需要运行以下进程：

- API。
- scheduler。
- `RECONCILE,SCAN` worker。
- `SOURCE_ANALYSIS` worker。
- `MATERIALIZE,IMPORT` worker。
- `ANALYSIS` worker。

## 4. 测试数据

在独立受管目录中准备：

1. 一份 TXT，正文包含唯一检索词。
2. 一份原生文本 PDF。
3. 一份扫描 PDF 或图片，用于 OCR 路径。
4. 一份 DOCX，包含多个段落和标题。
5. 一份 XLSX，包含多个 Sheet、表头和唯一单元格内容。
6. 一份损坏、不支持或加密文件，用于失败隔离。
7. 两个不同目录下的同名文件，用于路径和去重验证。

测试前记录每个原件的相对路径、SHA-256、大小和修改时间。

## 5. 自动化回归

从仓库根目录执行：

```bash
cd apps/api
PYTHONPYCACHEPREFIX=/private/tmp/file-agent-pycache \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -q
```

重点测试文件：

```bash
cd apps/api
PYTHONPYCACHEPREFIX=/private/tmp/file-agent-pycache \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -q \
  app/tests/test_managed_files_worker.py \
  app/tests/test_managed_source_search.py \
  app/tests/test_retrieval_readiness.py \
  app/tests/test_evidence_answer.py \
  app/tests/test_file_lifecycle.py
```

自动化测试必须覆盖：

- 扫描阶段不在修订 `READY` 前复制同一文件。
- 源侧分析完成后自动创建后台物化任务。
- 重复触发只保留一个幂等任务。
- 用户命中将既有任务从优先级 `100` 提升到 `20`。
- 后台任务已处于 `RUNNING` 时，相关集合仍能在完成或失败后正确收敛。
- 物化复用源侧页面和索引，不重复解析原件。
- 工作副本成功创建且原件字节不变。
- 失败任务不被普通扫描无限重开。

## 6. 手工测试用例

### T01：启动不阻塞

1. 启动 API，但暂不启动 worker。
2. 调用健康检查和普通登录接口。
3. 验证 API 可用，数据库中出现 `RECONCILE_MANAGED_ROOT`，但尚未复制文件。

预期：API 启动不等待扫描、解析或复制。

### T02：扫描与单文件顺序

1. 启动 `RECONCILE,SCAN` worker。
2. 暂停 `SOURCE_ANALYSIS` 和 `MATERIALIZE` worker。
3. 验证扫描产生 `ANALYZE_MANAGED_FILE_REVISION`，此时不存在对应工作副本。
4. 启动 `SOURCE_ANALYSIS` worker。
5. 验证修订变为 `READY` 后才产生 `MATERIALIZE_WORKING_COPY`。

预期：同一文件严格先分析、后复制。

### T03：全量工作副本同步

1. 启动 `MATERIALIZE,IMPORT` worker。
2. 等待所有可成功分析文件的物化任务完成。
3. 检查物理路径与数据库记录。

预期：文件保存到 `shared/test_library/<源相对路径>`，每个源修订最多对应一个活动主工作副本。

### T04：同步期间联合检索

1. 暂停 `MATERIALIZE` worker，只运行源侧分析。
2. 使用 TXT、PDF 或 XLSX 正文中的唯一词搜索。
3. 同时准备一份已经物化且包含另一个匹配词的文件。

预期：同一次搜索可返回工作副本和未物化源文件；同一修订不会重复出现。源文件结果暂时不可打开，但具有正文命中和证据位置。

### T05：未物化文件总结

1. 对源侧分析已经 `READY`、但尚未物化的文件发起总结。
2. 记录回答返回时间和物化任务状态。

预期：回答基于源侧 `EvidenceSpan` 返回并包含引用，不等待物理复制；随后对应物化任务被提升。

### T06：交互优先级和并发竞态

1. 确认目标文件已有优先级 `100` 的后台物化任务。
2. 在任务为 `PENDING` 时搜索并选择该文件。
3. 再测试任务已经为 `RUNNING` 时发起相同操作。

预期：`PENDING` 任务提升为 `20`；`RUNNING` 任务继续执行而不创建第二个任务。相关集合最终变为 `MATERIALIZED`，失败时变为 `RETRY_WAIT` 或 `FAILED`。

### T07：多 worker 幂等性

1. 同时启动两个 `MATERIALIZE,IMPORT` worker。
2. 重复触发 scheduler、搜索和总结。

预期：同一幂等键只有一个任务；最终只有一个活动 `WorkingCopy` 和一个目标物理文件，不发生覆盖。

### T08：重启恢复

1. 在分析和复制过程中分别终止 worker。
2. 等租约过期或按正常部署方式重启 worker。

预期：任务恢复执行，尝试次数不超过三次；已完成任务幂等复用，不从头重复生成业务记录。

### T09：失败隔离

1. 保留一份损坏或加密文件。
2. 与正常文件一起执行全量初始化。

预期：失败文件记录结构化失败且不生成虚假工作副本；其他文件继续完成。普通重复扫描不绕过失败终态，管理员可显式重处理。

### T10：原件变化保护

1. 在源侧分析完成后修改测试原件，再执行物化。
2. 另测分析过程中修改原件。

预期：分析过程中变化的修订标为 `STALE`；已有工作副本与新原件哈希不一致时返回 `SOURCE_CHANGED`，不得自动覆盖工作副本或原件。

### T11：原件保护复核

完成全部操作后重新记录原件相对路径、SHA-256、大小和修改时间，并与测试前结果比较。

预期：除 T10 明确由测试人员主动修改的文件外，所有原件四项数据完全一致。

### T12：运维降级开关

设置：

```env
MATERIALIZE_ALL_MANAGED_FILES=false
```

重新启动相关进程并扫描新文件。

预期：源侧分析和检索继续工作，但不会自动创建全量后台物化任务；用户最终相关文件仍按 `MATERIALIZE_RELEVANT_FILES_AFTER_RESPONSE` 配置处理。

## 7. 数据库核对

按环境权限使用只读 SQL：

```sql
SELECT status, count(*)
FROM managed_file_revisions
WHERE is_current = true
GROUP BY status
ORDER BY status;

SELECT job_type, queue_name, status, priority, count(*)
FROM filesystem_jobs
WHERE job_type IN (
  'SCAN_MANAGED_ROOT',
  'ANALYZE_MANAGED_FILE_REVISION',
  'MATERIALIZE_WORKING_COPY'
)
GROUP BY job_type, queue_name, status, priority
ORDER BY job_type, priority, status;

SELECT deduplication_key, count(*)
FROM filesystem_jobs
WHERE job_type = 'MATERIALIZE_WORKING_COPY'
GROUP BY deduplication_key
HAVING count(*) > 1;

SELECT status, count(*)
FROM relevant_file_set_items
GROUP BY status
ORDER BY status;
```

幂等键重复查询应返回零行。测试结束后不应长期存在无对应运行任务的 `MATERIALIZING` 或 `RETRY_WAIT` 集合项。

## 8. 通过标准

- 自动化测试全部通过。
- 所有正常测试文件完成源侧分析和工作副本同步。
- 同步期间正文检索、证据回答和总结可使用未物化源侧索引。
- 用户命中的待同步文件优先处理且不产生重复任务。
- worker 并发、重启和单文件失败不破坏其他文件。
- 原件保护检查通过。
- 日志、任务响应和普通用户界面均不泄露受管目录绝对路径或正文全文。
