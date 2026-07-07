# Server Managed Files Async P0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现服务器受管目录 P0：管理员启用部署层预定义目录，系统异步扫描真实文件，只读查询文件元数据，并让 Agent 可以通过 Tool 回答“列出学工收件箱中的 PDF”等请求。

**Architecture:** Docker/部署层只读挂载目录，应用层只认 `root_key` 和 `relative_path`，禁止任意宿主机路径。FastAPI 负责管理员配置、扫描任务创建和只读查询；PostgreSQL 表保存 managed roots、managed files 和 filesystem jobs；worker 使用数据库队列拉取扫描任务。Agent 通过白名单 Tool 查询数据库，不直接访问文件系统。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy、Alembic、PostgreSQL、LangGraph Tool Registry、pytest、Docker Compose。

## Current Status

- [x] Task 1: Database Models And Migration
- [x] Task 2: PathPolicy And Managed Root Configuration
- [x] Task 3: Admin Managed Root API
- [x] Task 4: Filesystem Job Queue
- [x] Task 5: Read-Only Scanner
- [x] Task 6: Managed File Query API
- [x] Task 7: Agent Tools
- [x] Task 8: Deployment Files

---

## Inputs Reviewed

- `/Users/zhouhexin/Downloads/file_agent_server_managed_files_async_plan.docx`
- 当前项目已有边界：
  - `apps/api/app/modules/operations/` 已有 OperationPlan 最小闭环。
  - `apps/api/app/modules/changesets/` 已有 ChangeSet 审计。
  - `apps/api/app/modules/agent/tool_registry.py` 已有 Tool 白名单和 `job-status-read` 占位。
  - `apps/api/app/modules/auth/` 已有用户角色字段。
  - `apps/api/app/modules/agent/graph.py` 有 `async_job_wait` 占位节点。

## Scope

P0 只做只读文件发现、扫描、查询和 Agent 列表/搜索能力。

P0 不做：

- 移动文件
- 重命名文件
- 删除文件
- 覆盖文件
- 创建目录
- 跨目录复制
- 自动归档
- 文件内容解析/OCR/分类的批量执行

这些写操作必须等 OperationPlan、ChangeSet、异步执行、权限和回滚路径稳定后再进入 P1。

## Recommended Development Order

### Task 1: Database Models And Migration

**Files:**
- Modify: `apps/api/app/db/models.py`
- Create: `apps/api/alembic/versions/20260707_0001_create_managed_files_tables.py`
- Test: `apps/api/app/tests/test_managed_files.py`

**Tables:**

- `managed_roots`
  - `id`
  - `root_key`
  - `display_name`
  - `container_path`
  - `enabled`
  - `read_only`
  - `allowed_operations_json`
  - `created_by`
  - `created_at`
  - `updated_at`

- `managed_files`
  - `id`
  - `root_id`
  - `relative_path`
  - `filename`
  - `extension`
  - `size_bytes`
  - `modified_at`
  - `fingerprint`
  - `status`
  - `last_seen_scan_run_id`
  - `created_at`
  - `updated_at`

- `filesystem_jobs`
  - `id`
  - `job_type`
  - `root_id`
  - `status`
  - `progress_current`
  - `progress_total`
  - `payload_json`
  - `result_json`
  - `error_message`
  - `locked_by`
  - `locked_at`
  - `created_by`
  - `created_at`
  - `updated_at`

- `filesystem_job_events`
  - `id`
  - `job_id`
  - `level`
  - `message`
  - `details_json`
  - `created_at`

- `filesystem_scan_runs`
  - `id`
  - `root_id`
  - `job_id`
  - `status`
  - `files_discovered`
  - `files_updated`
  - `files_missing`
  - `errors`
  - `started_at`
  - `finished_at`

**Constraints:**

- `managed_roots.root_key` unique。
- `managed_files(root_id, relative_path)` unique。
- `filesystem_jobs.status` 使用受控值：`PENDING`、`RUNNING`、`COMPLETED`、`FAILED`、`CANCELLED`。

**Tests:**

- ORM metadata 能创建表。
- `managed_files(root_id, relative_path)` 重复写入失败。
- `managed_roots.root_key` 重复写入失败。

### Task 2: PathPolicy And Managed Root Configuration

**Files:**
- Create: `apps/api/app/modules/managed_files/path_policy.py`
- Create: `apps/api/app/modules/managed_files/repository.py`
- Create: `apps/api/app/modules/managed_files/service.py`
- Create: `apps/api/app/modules/managed_files/schemas.py`
- Test: `apps/api/app/tests/test_managed_files_path_policy.py`

**Rules:**

- 只允许管理员启用部署层预定义 `mount_key`。
- `mount_key` 由环境变量声明，例如 `MANAGED_ROOT_STUDENT_AFFAIRS=/managed/student-affairs`。
- API 不接受任意宿主机绝对路径。
- 查询和扫描只传 `root_key`、`relative_path`。
- 拒绝：
  - 绝对路径
  - `..`
  - 空字节
  - 符号链接
  - Windows junction / reparse point
  - 路径逃逸

**Tests:**

- `../secret.pdf` 被拒绝。
- `/etc/passwd` 被拒绝。
- `C:\Windows\system.ini` 被拒绝。
- 普通相对路径 `2026/inbox/a.pdf` 通过。
- symlink 指向 root 外部时被拒绝。

### Task 3: Admin Managed Root API

**Files:**
- Create: `apps/api/app/modules/managed_files/router.py`
- Modify: `apps/api/app/main.py`
- Test: `apps/api/app/tests/test_managed_files_api.py`

**APIs:**

- `POST /api/admin/managed-roots`
  - 输入：`root_key`、`display_name`
  - 行为：只能启用环境变量中存在的 `root_key`
  - 权限：`admin`

- `GET /api/admin/managed-roots`
  - 返回 root 列表、启用状态、只读状态、允许操作
  - 权限：`admin` 或 `ops`

**Acceptance:**

- 普通 `user` 调用返回 403。
- `admin` 可以启用 `MANAGED_ROOT_STUDENT_AFFAIRS`。
- 请求体中出现任意路径字段时返回 400。
- 响应不返回宿主机绝对路径，只返回 `root_key`、`display_name` 和状态。

### Task 4: Filesystem Job Queue

**Files:**
- Create: `apps/api/app/modules/managed_files/jobs.py`
- Create: `apps/api/app/modules/managed_files/worker.py`
- Test: `apps/api/app/tests/test_filesystem_jobs.py`

**Behavior:**

- 创建扫描任务写入 `filesystem_jobs`，状态为 `PENDING`。
- worker 通过 PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` 领取任务。
- SQLite 测试环境使用普通查询 fallback。
- 任务事件写入 `filesystem_job_events`。
- 任务进度写 `progress_current`、`progress_total`。

**APIs:**

- `POST /api/admin/managed-roots/{root_id}/scan`
- `GET /api/admin/filesystem-jobs/{job_id}`

**Acceptance:**

- 重复创建扫描任务不会阻塞。
- job 状态从 `PENDING` -> `RUNNING` -> `COMPLETED`。
- 扫描失败时写 `FAILED` 和错误事件。
- 查询不存在 job 返回 404。

### Task 5: Read-Only Scanner

**Files:**
- Create: `apps/api/app/modules/managed_files/scanner.py`
- Test: `apps/api/app/tests/test_managed_file_scanner.py`

**Scanner Rules:**

- 只读遍历授权目录。
- 不打开文件正文，只读取元数据。
- 写入或更新 `managed_files`。
- 本轮未发现的历史文件标记 `MISSING`，不删除记录。
- fingerprint 使用轻量组合：`size_bytes + modified_at + relative_path`；后续可升级为内容 hash。
- 扫描过程中遇到单文件错误不终止整个扫描，写 job event。

**Acceptance:**

- PDF、DOCX、XLSX、CSV 文件进入 `managed_files`。
- 重复扫描不重复入库。
- 文件改名表现为旧记录 `MISSING` + 新记录 `ACTIVE`。
- symlink 被跳过并记录 warning。

### Task 6: Managed File Query API

**Files:**
- Modify: `apps/api/app/modules/managed_files/router.py`
- Modify: `apps/api/app/modules/managed_files/service.py`
- Test: `apps/api/app/tests/test_managed_files_api.py`

**API:**

- `GET /api/managed-files`

**Query Params:**

- `root_key`
- `extension`
- `filename_contains`
- `relative_path_prefix`
- `status`
- `limit`
- `offset`

**Acceptance:**

- 可以查询“学工收件箱中的 PDF”。
- 返回字段只包含：
  - `root_key`
  - `display_name`
  - `relative_path`
  - `filename`
  - `extension`
  - `size_bytes`
  - `modified_at`
  - `status`
- 不返回 container path 或宿主机绝对路径。

### Task 7: Agent Tools

**Files:**
- Modify: `apps/api/app/modules/agent/tool_schemas.py`
- Modify: `apps/api/app/modules/agent/tool_registry.py`
- Modify: `apps/api/app/modules/agent/capabilities/catalog.json`
- Modify: `apps/api/app/modules/agent/planner.py`
- Test: `apps/api/app/tests/test_agent_runtime.py`

**Tools:**

- `managed-root-list`
  - 列出当前用户可见的逻辑目录。

- `managed-file-list`
  - 按 `root_key`、扩展名、路径前缀列出文件。

- `managed-file-search`
  - 按文件名关键词搜索。

- `managed-root-scan`
  - 创建扫描 job，不同步扫描目录。

**Planner Examples:**

- “列出学工收件箱中的 PDF”
  - `intent=LIST_MANAGED_FILES`
  - Tool：`managed-file-list`

- “扫描学工收件箱”
  - `intent=SCAN_MANAGED_ROOT`
  - Tool：`managed-root-scan`

**Acceptance:**

- Agent 不接触绝对路径。
- Tool 输入 schema 禁止 `path`、`file_path`、`absolute_path`。
- Tool 输出不泄露 container path。

### Task 8: Deployment Files

**Files:**
- Modify: `deploy/` 下现有部署文件
- Modify: `docker-compose.yml` 或项目当前 compose 文件
- Modify: `README.md` 或 `docs/runbook.md`

**Changes:**

- 增加 `file-worker` 服务。
- 增加只读 bind mount 示例：
  - host: `/srv/file-agent/student-affairs-inbox`
  - container: `/managed/student-affairs`
  - mode: `ro`
- 增加环境变量：
  - `MANAGED_ROOT_STUDENT_AFFAIRS=/managed/student-affairs`
  - `MANAGED_ROOT_STUDENT_AFFAIRS_NAME=学工收件箱`

**Acceptance:**

- 文档说明不能通过网页或聊天输入宿主机路径。
- 文档说明 P0 只读，不执行文件变更。

## Integration With Spreadsheet Workbench

Anthropic xlsx skill 适合作为表格能力标杆，但不应直接下载、复制或改写进仓库；该 skill 的 GitHub 页面标注为 proprietary/source-available，适合作为能力方向参考，不适合作为项目内代码来源。

可借鉴方向应放到 P1 表格工作台：

- `analyze-spreadsheet` 保留为只读分析子能力。
- `edit-spreadsheet` 只能生成 OperationPlan，确认前不得写文件。
- `recalculate-spreadsheet` 通过 LibreOffice worker 隔离重算。
- `validate-spreadsheet` 扫描公式错误、引用错误、空值/类型异常和模板保真问题。
- `spreadsheet-workbench/SKILL.md` 负责编排意图和 Tool，不直接执行文件操作。

和本 P0 的关系：

- P0 先解决服务器真实目录的只读发现、扫描和查询。
- P1 再允许用户从 managed files 中选择 Excel，进入 spreadsheet workbench。
- P1 的任何编辑、生成或格式转换都必须产出派生件或 OperationPlan，不覆盖原文件。

## Verification Commands

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest app/tests/test_managed_files_path_policy.py
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest app/tests/test_managed_files_api.py
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest app/tests/test_filesystem_jobs.py
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest app/tests/test_managed_file_scanner.py
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest app/tests/test_agent_runtime.py
```

## Done Criteria

- 管理员可以启用一个部署层预定义目录。
- 用户不能提交任意路径。
- 扫描任务异步执行并可查询进度。
- 文件清单可按扩展名和文件名查询。
- Agent 可以回答“列出学工收件箱中的 PDF”。
- 全链路不泄露宿主机绝对路径。
- P0 不修改任何原始文件或目录结构。
