# File Agent 整项目手工烟测手册

本文用于在阶段四开发完成后以及后续版本发布前，对 File Agent 当前已经实现的普通用户入口、上传自动整理、
文件生命周期、权限、审计和真实工作副本副作用进行手工验证。自动化测试通过不能替代本文的真实文件
系统烟测。

阶段二及之前阶段的普通用户验收以
`docs/frontend-conversation-smoke-test.md` 为唯一页面烟测入口。本文中保留的 curl、直接 API、SQL 和
文件系统命令只用于阶段三内部索引、运维审计或故障诊断，不能替代 `/chat` 页面操作，也不能作为普通用户
产品闭环的通过证据。

## 1. 测试范围和通过原则

本轮必须验证：

- 普通用户注册、登录、新手引导和 `/chat`。
- 上传、异步查重、不可变原件归档、隐藏导入、摘要、分类和首次命名建议。
- 低置信度、重复文件、同名冲突、加密文件和宏风险提示。
- TXT、MD、CSV、PDF、DOC、DOCX、XLS、XLSX 的代表性解析。
- 普通用户任务回执不暴露 Skill、Tool、AgentRun、服务器路径或密钥。
- 工作副本重命名、移入回收站和恢复必须经过 OperationPlan 确认。
- 所有用户仅有一个共享物理工作目录、用户间逻辑数据隔离，以及 ops/admin 审计接口权限。
- 原件内容和路径在全部测试过程中保持不变。

以下能力当前不作为通过条件：

- 阶段五的正式 Evidence Answer、`qa_answers` 和持久化引用。
- 分类接受、拒绝和纠正的普通用户页面；同名冲突自然语言决策已经纳入本阶段验收。
- 尚未提供的 `/admin/documents`、`/admin/feedback`、`/admin/settings/llm` 前端页面。
- 病毒扫描引擎。系统当前只能说明已完成基础格式、MIME、宏和加密风险检查。

任何测试只要出现原件被覆盖、未确认即产生物理副作用、跨用户访问成功或普通用户接口泄漏内部载荷，
都属于 P0 失败，必须停止后续发布。

## 2. 测试记录

执行前填写：

```text
测试日期：
测试人员：
Git commit：
操作系统：
Python：
PostgreSQL：
LibreOffice：
浏览器：
测试数据库：
测试存储根：
```

禁止使用生产数据库、正式受管目录或包含真实个人信息的文件执行烟测。

## 3. 自动检查前置条件

在仓库根目录执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -v

cd apps/web
npm run build
cd ../..

/opt/homebrew/anaconda3/envs/py311/bin/python \
  -m alembic -c apps/api/alembic.ini heads

/opt/homebrew/anaconda3/envs/py311/bin/python -m pip check
git diff --check
```

当前阶段期望：

```text
后端（macOS/Linux）：518 passed, 19 skipped
后端（Windows 有 symlink 权限）：518 passed, 19 skipped
后端（Windows 无 symlink 权限）：517 passed, 20 skipped，其中新增跳过项必须是 symlink 权限前置条件
前端：TypeScript 检查和 Vite build 成功
Alembic：单一 head 20260724_0003
Python：No broken requirements found
```

后续新增测试后，测试数量可以增加，但不能出现失败项或新增未说明的跳过项。

### 3.1 Windows 全量回归与生命周期路径检查

从仓库根目录使用当前已配置的 Python 环境执行：

```powershell
python -m pytest -v

python -m alembic -c apps/api/alembic.ini heads
python -m pip check
git diff --check

Set-Location apps/web
npm run build
Set-Location ../..
```

Windows 测试基础设施会自动使用当前 `%TEMP%` 下的短 pytest 根目录，避免 pytest 自动附加用户名、轮次和
完整测试函数名后制造非业务路径。业务长路径没有因此被跳过：下面的专项测试会独立重建曾经达到 267 字符
的工作副本路径，继续保护 StorageService 的路径长度边界。

全量失败时先执行以下定向测试：

```powershell
python -m pytest -v `
  apps/api/app/tests/test_file_lifecycle_storage.py `
  apps/api/app/tests/test_file_lifecycle.py::test_upload_is_archived_then_imported_by_separate_jobs
```

通过标准：3 项测试全部通过。该检查会重建曾经达到 267 字符的 pytest 工作副本路径，验证内部暂存路径
不再重复完整任务 UUID、文件 UUID 和原文件名，并验证原子复制的 `.part` 文件使用短排他名称。

测试套件默认忽略项目 `.env` 中的受管目录、Neo4j、Embedding、MCP、OCR 和外部 LLM 开关；需要验证这些
能力的用例必须显式注入 deterministic fake 或单独启用集成测试。这样 Windows 开发机不会在普通 pytest
期间递归扫描真实 Downloads、连接 Neo4j、下载 OCR 模型或调用外部服务。

`test_path_policy_rejects_symlink_escape` 需要操作系统允许创建符号链接。Windows 未启用开发者模式且当前
终端无管理员权限时，该项会以明确原因跳过；启用开发者模式后必须通过，不能把真实 PathPolicy 断言删除。

## 4. 隔离环境准备

### 4.1 当前开发测试数据库

烟测直接使用项目当前已经配置并正在使用的开发测试数据库，不创建新数据库、不切换 SQLite、不清表、
不执行 Alembic upgrade/downgrade，也不通过 SQL 修改测试结果。用户通过注册、上传、发消息和确认计划
产生的正常业务数据允许写入当前开发测试数据库。

为避免历史数据干扰，每次前端烟测使用唯一批次号，并把批次号写入虚构测试文件正文。测试结束后不直接
删除数据库记录；需要清理时必须以后续受控产品能力执行。

如果当前数据库 schema 与运行代码不兼容，应停止烟测并报告环境问题，而不是在烟测过程中修改数据库。

### 4.1.1 共享工作目录的干净开发重置

仅当需要从零验证“每个文件只导入一次”时，先停止 API、scheduler、watcher 和全部 worker，再在仓库根目录
执行 migration，随后运行以下受控命令：

```bash
PYTHONPATH=apps/api \
/opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.scripts.reset_development_shared_workspace \
  --confirm-reset-shared-workspace
```

该命令会清空：数据库业务表（保留 `alembic_version`）、`WORKING_COPY_STORAGE_ROOT`、
`TRASH_STORAGE_ROOT`、`FILE_STORAGE_ROOT` 下的 `uploads`/`quarantine`/`temp`，以及
`MANAGED_ROOT_ARCHIVE_WRITE_PATH` 中旧上传归档原件。它明确不会删除外部
`MANAGED_ROOT_*` 受管原始资料目录，例如 `MANAGED_ROOT_SCHOOL_FILES`。

命令会拒绝空归档路径、项目根、文件系统根、重复目标以及任何与外部受管原始资料目录重叠的路径；出现
拒绝时必须修正 `.env`，不能手动用递归删除命令绕过。完成后重新启动服务，系统会创建唯一的
`SYSTEM_SHARED` 工作区；首次扫描会把外部资料按批次导入 `shared/<root_key>`，不会再按用户复制。

### 4.2 LibreOffice

`.doc` 和 `.xls` 的完整烟测必须安装 LibreOffice。macOS 示例：

```bash
/Applications/LibreOffice.app/Contents/MacOS/soffice --version
```

如果目标环境没有 LibreOffice，必须把 `.doc`、`.xls` 用例记为“环境阻塞”，不能记为通过，也不能用
`xlrd` 或文件名推断替代真实 `.xls -> 临时 .xlsx -> openpyxl` 验证。

## 5. 启动顺序

### 5.1 Worker 与 scheduler 启动

Windows CMD 从仓库根目录执行以下脚本即可。它会分别打开扫描 worker、导入/生命周期 worker 和
scheduler 三个窗口；扫描每批发现文件后，导入 worker 可立即消费 IMPORT 任务，不必等待全量扫描。

~~~cmd
scripts\start-file-agent-workers.cmd
~~~

需要 watcher 时追加参数：

~~~cmd
scripts\start-file-agent-workers.cmd --with-watcher
~~~

如 Python 不在 PATH，先指定已配置解释器：

~~~cmd
set "FILE_AGENT_PYTHON=D:\anaconda\envs\myenv\python.exe"
scripts\start-file-agent-workers.cmd
~~~

以下是 macOS/Linux 的等价分终端启动方式。

### 5.1.1 扫描 worker

终端一：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent

PYTHONPATH=apps/api \
FILESYSTEM_WORKER_ID=reconcile-scan-worker \
FILESYSTEM_WORKER_QUEUES=RECONCILE,SCAN \
/opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.managed_files.worker
```

worker 启动时会输出“已启动，等待任务”，领取、完成或失败任务时会输出 job ID、
任务类型、队列和耗时；每个扫描批次会额外显示 `batch`、`files_discovered` 与
`import_jobs`。不会输出文件正文、绝对路径或密钥。空闲轮询不会刷屏，这不是卡住
或退出。

### 5.1.2 导入与上传生命周期 worker

终端二：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent

PYTHONPATH=apps/api \
FILESYSTEM_WORKER_ID=import-lifecycle-worker \
FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE,IMPORT,FILE_OPERATION \
/opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.managed_files.worker
```

API 启动时只向 `RECONCILE` 队列提交同步任务。扫描 worker 每达到文件数或时间
预算，就提交该批 `IMPORT` 任务；导入 worker 应立刻显示 `IMPORT_WORKING_COPIES`，
无需等待整棵目录扫描结束。

### 5.1.3 已有受管原始目录的同步前提

在启动 API 前，`.env` 必须定义一个普通受管目录，例如：

```dotenv
MANAGED_ROOT_SCHOOL_FILES=/absolute/path/to/school-files
MANAGED_ROOT_RECONCILE_ON_STARTUP=true
FILESYSTEM_ASYNC_JOBS_ENABLED=true
MANAGED_ROOT_SCAN_BATCH_SIZE=100
MANAGED_ROOT_SCAN_BATCH_MAX_SECONDS=5
```

`MANAGED_ROOT_ARCHIVE_WRITE_PATH` 仅是上传文件的受保护归档写入位置，系统刻意
不会把它当作可扫描的受管根；将原始文件手动放入该目录不会触发同步。普通受管根
中的文件在 API 启动后依次进入 `RECONCILE -> SCAN -> IMPORT`，再由 worker 为已有
用户工作区创建只可由 File Agent 操作的工作副本。

### 5.2 生命周期 scheduler

终端三：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent

PYTHONPATH=apps/api \
/opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.file_lifecycle.scheduler
```

受管目录近实时同步不是上传闭环的前置条件。如需同时验证受管目录 watcher，再启动：

```bash
PYTHONPATH=apps/api \
/opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.file_lifecycle.watcher
```

### 5.3 API

终端四：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent

PYTHONPATH=apps/api \
/opt/homebrew/anaconda3/envs/py311/bin/python \
  -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 5.4 前端

终端五：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent/apps/web
npm run dev
```

### 5.5 页面健康检查

浏览器访问 `http://127.0.0.1:5173/login`，完成登录并进入 `/chat`。页面能够加载、发送消息和选择附件
才属于普通用户健康检查通过。`GET /api/health` 只保留给运维诊断，不再作为阶段二及之前阶段的用户烟测
步骤。

## 6. 测试数据矩阵

所有文件使用虚构内容。上传前执行：

```bash
shasum -a 256 /path/to/file-agent-smoke-input/* \
  | tee /tmp/file-agent-smoke/input-sha256-before.txt
```

| 编号 | 文件 | 内容要求 | 主要验证点 |
|---|---|---|---|
| F01 | `通知.pdf` | 正文含明确年份、单位和完整标题 | 泛化文件名必须依据正文分类并生成命名建议 |
| F02 | `奖学金材料.docx` | 多段落并含明确业务主题 | Word 结构解析、分类和命名建议 |
| F03 | `旧版通知.doc` | 与 F02 不同内容 | LibreOffice 持久 `.docx` 派生件 |
| F04 | `统计表.xlsx` | 至少两个 Sheet，含日期、金额或人数列 | 全 Sheet 读取和确定性表格能力 |
| F05 | `旧版统计表.xls` | 至少两个 Sheet | 隔离转换、全部 Sheet、原件不变 |
| F06 | `说明.txt` | 明确标题、年份、单位 | 文本自动整理成功路径 |
| F07 | `扫描件.txt` | 内容短且缺少年份和标题 | 低置信度保留原上传文件名 |
| F08 | F06 的字节级副本 | SHA-256 与 F06 相同 | 重复上传确认 |
| F09/F10 | 内容不同但可生成相同目标名 | 相同年份和标题 | 同名冲突不自动加后缀、不覆盖 |
| F11 | 加密 PDF | 需要密码才能读取 | 原件归档后停止自动解析 |
| F12 | `.xlsm` | 含宏项目或宏标记 | 只提示风险，不执行宏 |
| F13 | 损坏 PDF | 允许扩展名但正文无效 | 单文件失败不影响同批其他文件 |

## 7. 具体烟测用例

本节原有用例包含页面验收和技术核验。阶段二及之前阶段执行时，应改用
`docs/frontend-conversation-smoke-test.md` 中的 `UI-SMOKE-*`；不得用本节的 curl 或 SQL 步骤替代页面
失败项。本节直接 API 步骤仅供阶段三内部事实检查和故障定位。

### SMOKE-001 注册、登录和普通用户界面

步骤：

1. 访问 `http://127.0.0.1:5173/login`。
2. 注册用户 `smoke_user_a`，烟测密码统一使用 `password123`。
3. 完成或跳过 `/getting-started`，进入 `/chat`。
4. 刷新页面，确认登录状态可以恢复。
5. 退出登录，再重新登录。

通过标准：

- 注册时自动创建 default workspace。
- 普通用户可以进入 `/chat`。
- 页面不展示 Skill、Tool、LangGraph、AgentRun、ToolInvocation、服务器绝对路径或模型 Prompt。

### SMOKE-002 上传文件并输入任务文字后的自动整理

当前阶段有附件时仍要求用户输入任务文字，不允许空文字直接提交。该限制用于避免系统猜测用户希望
分类、总结还是仅保存文件。

步骤：

1. 上传 F01、F02、F04、F06，并输入“读取并整理这些文件”。
2. 不要求用户选择 Skill、Tool、目录或解析器。
3. 等待 worker 完成查重、归档和导入。
4. 刷新聊天页，检查逐文件回执。

通过标准：

- 每个文件独立显示处理结果，不能只显示批量统计。
- 回执包含整理后的文件名、分类、年份、关键词、实体、警告和错误。
- 高置信度文件使用正文生成的整理名称。
- 文件扩展名保持真实扩展名，不出现字面量 `.ext`。
- 文种不作为额外字段重复追加到文件名；正文标题原有的“通知”“报告”等词正常保留。
- 回执明确说明受管原件保持不变。

### SMOKE-003 原件保护和 lineage

使用普通用户 token 查询工作副本：

```bash
export FILE_AGENT_SMOKE_TOKEN="$(
  curl -sS -X POST http://127.0.0.1:8000/api/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"username":"smoke_user_a","password":"password123"}' \
  | jq -r '.access_token'
)"

curl -sS http://127.0.0.1:8000/api/working-copies \
  -H "Authorization: Bearer ${FILE_AGENT_SMOKE_TOKEN}" | jq
```

选择一个 `working_copy_id` 后查询：

```bash
curl -sS http://127.0.0.1:8000/api/working-copies/<working_copy_id>/lineage \
  -H "Authorization: Bearer ${FILE_AGENT_SMOKE_TOKEN}" | jq

curl -sS http://127.0.0.1:8000/api/working-copies/<working_copy_id>/versions \
  -H "Authorization: Bearer ${FILE_AGENT_SMOKE_TOKEN}" | jq
```

通过标准：

- 工作副本可以追溯到 managed file 和 DocumentVersion。
- 普通接口不返回宿主机绝对路径。
- 受管原件 SHA-256 与上传前一致。
- 首次整理不会覆盖用户测试输入或受管原件。

### SMOKE-004 `.doc` 与 `.xls` 转换

步骤：

1. 上传 F03 和 F05。
2. 要求读取两个文件的完整内容。
3. 对 F05 分别询问两个 Sheet 中的内容。
4. 再次读取相同文件，观察复用行为。
5. 比较转换前后原件 SHA-256。

通过标准：

- `.doc` 生成可追溯的 `.docx` 派生件，重新读取可以复用。
- `.xls` 使用独立输入、输出和 LibreOffice profile 转为临时 `.xlsx`。
- `.xls` 的全部 Sheet 都可以读取，不能只读取第一个 Sheet。
- 临时 `.xlsx` 不登记为新的上传原件或 DocumentVersion。
- 原 `.doc`、`.xls` 字节不变。
- LibreOffice 缺失或输出无效时返回结构化失败，不能回落到 `xlrd` 或伪造正文成功。

### SMOKE-005 低置信度、同名冲突和批量隔离

步骤：

1. 同一批上传 F07、F09、F10、F13 和一个有效 TXT。
2. 等待全部任务完成。

通过标准：

- F07 保留 `扫描件.txt`，并显示命名依据不足的待确认说明。
- F09/F10 发生目标名称冲突时，新文件保留上传名并进入待确认位置。
- 冲突发生后不自动生成 `_第二版`，不覆盖已有工作副本。
- 用户看到“同时保留、保留已有、替换现有工作副本、删除现有工作副本”等处理方式。
- F13 失败不影响同批有效 TXT 完成整理。
- 分别在独立冲突批次中回复“同时保留”“保留已有文件”“用新文件替换已有文件”和“删除已有文件”。
- 每种选择都必须先展示 OperationPlan；确认前文件不变，确认后才执行真实工作副本动作。
- “同时保留”确认后才分配 `_第二版` 等稳定后缀；替换或删除已有文件时，旧工作副本进入可恢复回收站。
- 受管原件在所有冲突选择前后都保持不变。

### SMOKE-005A 首次导入只给出命名建议

步骤：

1. 在 `/chat` 上传 F01 或 F06，并输入“读取并分类这个文件”，不要在消息中提出改名。
2. 等待文件处理回执完成，记录上传名和“建议名称”。
3. 在工作副本列表或文件系统中核对当前工作副本名称。
4. 再在同一对话中明确回复“改名”，确认系统先展示 OperationPlan。
5. 在计划确认前后分别核对工作副本名称。

通过标准：

- 首次导入后工作副本、DocumentVersion 和受管原件均保留上传时的文件名。
- 回执可以显示“建议名称”，但必须明确说明当前尚未改名。
- 用户未提出改名时，不创建自动重命名 OperationPlan，不产生 `FILENAME_CHANGED` ChangeItem。
- 用户明确提出“改名”后才生成 OperationPlan；确认前文件不变，确认后才执行真实工作副本重命名。

### SMOKE-006 重复上传决策

步骤：

1. F06 已完成导入后，再上传 F08。
2. 等待查重卡出现。
3. 分三轮分别验证“取消上传”“使用已有文件”“继续上传”。

通过标准：

- 查重卡展示脱敏候选，不泄漏其他用户身份或路径。
- “取消上传”不创建新活动工作副本。
- “使用已有文件”返回已有工作副本，不重复导入。
- “继续上传”才允许进入归档和导入任务。
- 相同 SHA-256 不能在没有用户决策时被系统静默合并。

### SMOKE-007 加密文件和宏风险

步骤：

1. 上传 F11 和 F12。
2. 等待归档与风险检查完成。

通过标准：

- F11 原件被保护，但状态为 `NEEDS_REVIEW`，不创建可解析工作副本。
- 系统提示上传可读取版本，不尝试密码或破解。
- F12 显示宏风险，但系统不执行宏、脚本、链接或嵌入对象。
- 页面和日志不得出现“病毒扫描通过”或同义表述。

### SMOKE-008 DocumentVersion 原文索引（CPU-only）

步骤：

1. 确认 `.env` 使用 `RETRIEVAL_MODE=lexical`、`CHINESE_TOKENIZER=jieba`、
   `EMBEDDING_ENABLED=false`、`EMBEDDING_PROVIDER=disabled`，并按 worker 容量配置
   `DOCUMENT_INDEX_MAX_CHARS`、`DOCUMENT_INDEX_MAX_CHUNKS`；服务器无需安装 GPU。
2. 上传 F01、F05 和 F06，完成查重决策并等待工作副本导入任务结束。
3. 从工作副本 lineage 取得各自 `document_id`，分别请求：

```bash
curl -sS http://127.0.0.1:8000/api/documents/<document_id>/chunks \
  -H "Authorization: Bearer ${FILE_AGENT_SMOKE_TOKEN}" | jq
```

4. 对同一文件再次触发读取/整理，重复查询 Chunk 概览。
5. 使用另一个普通用户的 token 请求第 3 步 URL。

通过标准：

- F01/F06 的 `status=COMPLETED`，`chunk_count`、`evidence_count` 均大于 0，PDF Chunk 有真实页码。
- F05 的每个工作表都有独立定位；证据包含真实 `sheet_name` 和 `cell_range`，不能只用页码代替。
- 所有结果的 `embedding_status=DISABLED`；没有模型下载、GPU 进程或外部 embedding 请求。
- 重复处理复用同一版本索引，不增加同一 `document_version_id + extraction_run_id + config_hash` 的运行。
- 重命名或移动工作副本后索引仍复用；只有正文产生新 DocumentVersion 或解析配置变化才建立新索引。
- API 响应不包含 `text_content`、`search_text`、`search_vector`、`embedding`、绝对路径或全文。
- 其他用户请求返回 404，不能探测文件是否存在。
- 原文件和工作副本 SHA-256 未因建索引发生变化。

失败标准：

- embedding 关闭导致 Chunk/Evidence 失败。
- 页码、Sheet 或单元格范围由文件名/文本猜测，或为空时伪造坐标。
- 重复运行生成重复 Chunk，或者移动/改名导致历史引用失效。

### SMOKE-009 对话文件搜索、原文定位和表格计算

在 `/chat` 依次输入：

```text
找我刚才上传的奖学金材料。
找我去年的奖学金材料。
哪个文件提到了公示期限？
找包含资助金额的表格。
打开2026年的学生工作文件。
总结刚才上传的PDF。
把统计表的每个工作表分别概括一下。
按单位汇总统计表中的人数或金额。
给这些文件生成标准化文件名建议，但先不要改。
```

通过标准：

- 搜索只返回当前用户工作区的活动工作副本；另一个测试用户的同主题文件永远不出现。
- 最终文件名、分类、元数据和摘要先参与低耗文档级召回；摘要遗漏但原文含“公示期限”的文件仍须
  经 Chunk 补召回命中，并显示真实页码。
- XLSX 命中“资助金额”时显示真实 Sheet 与单元格范围；不能通过文件名猜测位置。
- “刚才这些文件”只检索该轮后端确认的附件；“找我去年的奖学金材料”可以在当前用户工作区全局检索。
- 结果卡默认显示前 10 个，点击“查看更多”每次追加最多 10 个；点击“查看文件”能通过鉴权下载或预览，
  不依赖相对路径。
- 页面和普通消息/API 响应不显示 Skill、Tool、Chunk、内部路径、搜索词项、SQL 分数或完整正文。
- Excel 数字汇总由确定性表格服务完成，不能让 LLM 心算。
- 重命名请求只生成 OperationPlan，确认前文件不变。
- 阶段四只展示搜索定位和受限短预览，不要求返回阶段五的正式 Evidence Answer。

### SMOKE-010 工作副本重命名确认

步骤：

1. 在聊天页请求重命名建议。
2. 记录计划中的 before/after 和 OperationPlan ID。
3. 确认前查询工作副本、版本和文件系统路径。
4. 点击确认。
5. 再次查询工作副本、版本和路径记录。

查询路径记录：

```bash
curl -sS http://127.0.0.1:8000/api/working-copies/<working_copy_id>/path-records \
  -H "Authorization: Bearer ${FILE_AGENT_SMOKE_TOKEN}" | jq
```

通过标准：

- 确认前 OperationPlan 为 `PLANNED` 或 `WAITING_CONFIRMATION`，文件未变化。
- 确认后才执行真实重命名。
- `working_copy_id` 不变，DocumentVersion 数量不增加。
- 新增不可变路径记录和 `FILENAME_CHANGED` ChangeItem。
- 受管原件文件名、路径和 SHA-256 始终不变。

### SMOKE-011 回收站和恢复

本用例必须从 `/chat` 页面完成，不使用工作副本 ID、回收站 ID、curl、Swagger 或 SQL：

1. 在已上传测试文件的会话中输入：`把刚才上传的文件移入回收站。`
2. 页面应展示“移入回收站计划”，先刷新页面确认计划仍在等待确认且文件未变化。
3. 点击“确认移入回收站”。
4. 输入：`恢复刚才删除的文件。`
5. 页面应展示“恢复文件计划”，确认前文件仍未恢复。
6. 点击“确认恢复”，再输入：`读取刚才恢复的文件。`

通过标准：

- 创建计划和确认之间不发生物理变化。
- 确认移入回收站后，页面提示文件已进入可恢复回收站。
- 确认恢复后，工作副本重新可通过对话读取。
- 原路径冲突时恢复到稳定备用路径，不覆盖其他工作副本。
- 页面不提供永久删除入口；`TRASH_AUTO_PURGE_ENABLED=false`。
- 普通用户看不到物理路径、数据库 ID、Skill、Tool 或内部执行载荷。

### SMOKE-012 分类反馈

步骤：

1. 在分类卡上接受一条建议。
2. 拒绝另一条建议。
3. 把第三条建议更正为完整分类路径。
4. 刷新页面并查询反馈汇总。

```bash
curl -sS http://127.0.0.1:8000/api/classification/feedback/summary \
  -H "Authorization: Bearer ${FILE_AGENT_SMOKE_TOKEN}" | jq
```

通过标准：

- 接受、拒绝和更正都形成追加式反馈记录。
- 更正同时表达原分类负样本和目标分类正样本。
- 反馈不会直接修改 ACTIVE taxonomy 或正式 `document_categories`。

### SMOKE-013 用户隔离和内部审计权限

步骤：

1. 注册第二个普通用户 `smoke_user_b`。
2. 使用用户 B 访问用户 A 的会话、文件、工作副本和 OperationPlan。
3. 使用用户 A、B 分别访问内部审计接口。

通过标准：

- 用户 B 不能读取、下载、修改或确认用户 A 的对象，返回 403 或 404。
- 普通用户访问 `/api/agent/tools` 返回 403。
- 普通用户访问 `/api/agent-runs/{agent_run_id}` 返回 403。
- 普通用户访问 `/api/changesets/{changeset_id}` 返回 403。
- 普通消息接口只返回 `task_result`，不返回 AgentRun、ToolInvocation、Planner 或原始 Tool 输出。

### SMOKE-014 ops/admin 审计接口

当前没有 admin 前端页面。先通过 `/login` 注册专用用户 `smoke_admin`，烟测密码使用
`password123`。随后仅在隔离测试数据库中把该用户提升为 admin：

```bash
docker exec file-agent-postgres \
  psql -U file_agent -d file_agent \
  -c "UPDATE users SET role='admin' WHERE username='smoke_admin';"
```

提升后必须重新登录，使新 JWT 包含 admin 角色。验证：

```text
GET /api/agent/tools
GET /api/agent-runs/{agent_run_id}
GET /api/agent-runs/{agent_run_id}/tool-invocations
GET /api/changesets/{changeset_id}
```

通过标准：

- admin/ops 可以读取内部审计数据。
- 审计中 Tool 业务失败对应 `ToolInvocation.status=FAILED`。
- 未确认或未执行的物理计划不能显示为 `EXECUTED`。
- 普通用户仍然不能访问这些接口。

### SMOKE-015 日志和敏感信息

检查当天 JSONL 日志：

```bash
tail -n 50 logs/file-agent-"$(date +%F)".log

rg -n 'Bearer |LLM_API_KEY|password|text_content|病毒扫描通过' \
  logs/file-agent-"$(date +%F)".log
```

通过标准：

- 每行是合法 JSON。
- API 日志包含 request_id；Agent、Tool 和文件事件尽量包含关联 ID 与耗时。
- 第二条命令不应发现 JWT、密码、API key、文件全文或虚假病毒扫描结论。
- 日志不能替代 AgentRun、ToolInvocation、ChangeSet 和 ChangeItem 审计事实。

## 8. 最终原件复核

全部用例结束后再次执行：

```bash
shasum -a 256 /path/to/file-agent-smoke-input/* \
  | tee /tmp/file-agent-smoke/input-sha256-after.txt

diff -u \
  /tmp/file-agent-smoke/input-sha256-before.txt \
  /tmp/file-agent-smoke/input-sha256-after.txt
```

通过标准：`diff` 无输出。随后按 lineage 中的受管原件相对路径，对受管原件再次计算 SHA-256，结果也
必须与对应 DocumentVersion 一致。

## 9. 结果记录模板

每个失败项单独记录：

```text
用例编号：
结果：PASS / FAIL / BLOCKED
Git commit：
用户：
conversation_id：
document_id：
document_version_id：
working_copy_id：
agent_run_id：
operation_plan_id：
changeset_id：
request_id：
输入文件 SHA-256：
预期结果：
实际结果：
是否影响原件：
日志事件：
截图或复现步骤：
```

## 10. 停止和清理

先停止前端、API、scheduler、watcher 和 worker，再清理隔离测试目录。不要把删除目标写成 `$HOME`、
`~`、仓库根或未展开变量。确认目标确实为 `/tmp/file-agent-smoke` 后再删除。

本地 PostgreSQL 容器可以停止：

```bash
docker compose stop postgres
```

如需清空烟测数据库，应使用独立测试数据库的显式数据库命令；不要删除生产数据库卷。
