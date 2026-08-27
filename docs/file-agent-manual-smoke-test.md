# File Agent 整项目手工烟测手册

本文用于在阶段五开发完成后以及后续版本发布前，对 File Agent 当前已经实现的普通用户入口、上传自动整理、
文件生命周期、权限、审计和真实工作副本副作用进行手工验证。自动化测试通过不能替代本文的真实文件
系统烟测。

阶段二及之前阶段的普通用户验收以
`docs/frontend-conversation-smoke-test.md` 为唯一页面烟测入口。本文中保留的 curl、直接 API、SQL 和
文件系统命令只用于阶段三内部索引、运维审计或故障诊断，不能替代 `/chat` 页面操作，也不能作为普通用户
产品闭环的通过证据。

阶段五逐题验收以 `docs/stage-5-frontend-backend-acceptance-test-cases.md` 为准；该文档明确每个前端
问题是否需要只读核验 PostgreSQL、Neo4j、文件系统或日志。

## 1. 测试范围和通过原则

本轮必须验证：

- 普通用户注册、登录、新手引导和 `/chat`。
- 上传、异步查重、不可变原件归档、隐藏导入、摘要、分类和首次命名建议。
- 低置信度、重复文件、同名冲突、加密文件和宏风险提示。
- TXT、MD、CSV、PDF、DOC、DOCX、XLS、XLSX 的代表性解析。
- 普通用户任务回执不暴露 Skill、Tool、AgentRun、服务器路径或密钥。
- 工作副本重命名、移入回收站和恢复必须经过 OperationPlan 确认。
- 所有用户仅有一个共享物理工作目录；共享 `ACTIVE` 工作副本可访问，但个人会话、附件上下文、上传
  来源、反馈和操作确认记录保持隔离。
- 原件内容和路径在全部测试过程中保持不变。
- 普通文件问答和全文总结只使用活动当前版本，并显示可点击引用编号与紧凑文件框。
- 精确命中回收站文件时只显示恢复选择卡；同名不同内容文件必须先由用户单选。
- 表格金额、计数和汇总由确定性分析器计算，聊天页不展示内部行号或单元格定位。

以下能力不作为阶段五通过条件，但完成阶段六后必须按本文 `SMOKE-012` 验收：

- 自然语言接受、拒绝、纠正分类，正式共享分类以及确认后的共享目录整理。
- 尚未提供的 `/admin/documents`、`/admin/feedback`、`/admin/settings/llm` 前端页面。
- 病毒扫描引擎。系统当前只能说明已完成基础格式、MIME、宏和加密风险检查。

任何测试只要出现原件被覆盖、未确认即产生物理副作用、跨用户个人会话或上传来源访问成功、普通用户
接口泄漏内部载荷，都属于 P0 失败，必须停止后续发布。访问唯一共享工作目录中的 `ACTIVE` 工作副本
属于预期行为，不算越权。

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
后端（macOS/Linux）：607 passed, 19 skipped
后端（Windows 有 symlink 权限）：607 passed, 19 skipped
后端（Windows 无 symlink 权限）：606 passed, 20 skipped，其中新增跳过项必须是 symlink 权限前置条件
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
`xlrd` 或文件名推断替代真实 `.xls -> CONVERTED_XLSX -> openpyxl` 验证。

## 5. 启动顺序

### 5.1 Worker 与 scheduler 启动

Windows CMD 从仓库根目录执行以下脚本即可。它会分别打开扫描 worker、导入/生命周期 worker 和
scheduler 三个窗口；扫描每批发现文件后，导入 worker 可立即消费 IMPORT 任务，不必等待全量扫描。
脚本会在打开子窗口前同步并校验当前 Windows `.env` 中的受管目录，因此必须从 Windows 本机仓库
根目录执行，不能使用文档中的 macOS `/Users/...` 路径。`.env` 必须位于 Windows 仓库根目录，
例如 `E:\PycharmProject\file-agent\.env`；放在 Downloads 中的 `.env` 不会被自动读取。

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

预检成功时，当前窗口必须先显示类似：

~~~text
[File Agent Startup] 配置检查通过 managed_roots=1 root_keys=school_files
~~~

然后才会打开三个子窗口。预检失败时脚本返回非零退出码且不启动 worker，并明确区分：

```text
MANAGED_ROOT_NOT_FOUND
MANAGED_ROOT_NOT_DIRECTORY
MANAGED_ROOT_PERMISSION_DENIED
MANAGED_ROOT_UNAVAILABLE
```

`MANAGED_ROOT_SCAN_BATCH_SIZE`、`MANAGED_ROOT_SCAN_BATCH_MAX_SECONDS` 等全局参数不能出现在
`root_keys` 中。旧版本曾经误登记的同名伪目录会在预检阶段停用，其待执行扫描任务会被标记失败，
不会继续被扫描 worker 领取。使用共享开发数据库但每台电脑保留本地
`WORKING_COPY_STORAGE_ROOT` 时，数据库已有 WorkingCopy、当前机器物理文件缺失的情况会在下次扫描
重新入队；生命周期 worker 只从哈希一致的不可变原件修复同一副本，不新增重复 WorkingCopy，也不会
自动恢复已进入回收站的文件。

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
- `.xls` 使用独立输入、输出和 LibreOffice profile 转换，并发布为版本级持久化 `.xlsx` 派生件。
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
- 查重候选必须覆盖共享 `ACTIVE` 工作副本和尚未同步/物化为工作副本的当前受管文件。
- 如果相同 SHA-256 只存在于回收站，上传查重不得返回该候选或展示确认卡；本次上传直接按新文件建立
  工作副本，原回收站记录不能被自动恢复或合并。
- 用户完成选择后，确认卡应退出普通消息流；刷新页面也不得展示 `CANCEL_UPLOAD`、
  `USE_EXISTING_FILE`、`CONTINUE_UPLOAD`、“重复上传处理”或“已记录重复上传决策”等内部审计载荷。
- 该 UI 隐藏不能删除后端 Review、AgentRun、ToolInvocation、ChangeSet 或 ChangeItem 审计记录。

### SMOKE-007 加密文件和宏风险

步骤：

1. 上传 F11 和 F12。
2. 等待归档与风险检查完成。

通过标准：

- F11 原件被保护，但状态为 `NEEDS_REVIEW`，不创建可解析工作副本。
- 系统提示上传可读取版本，不尝试密码或破解。
- F12 显示宏风险，但系统不执行宏、脚本、链接或嵌入对象。
- 页面和日志不得出现“病毒扫描通过”或同义表述。
- 对能够被 PyMuPDF 修复读取、但 Docling 会报告页数不一致的 PDF，系统应自动改用本地逐页解析；
  页面不得暴露 `Inconsistent number of pages`、`Input document ... is not valid` 或服务器绝对路径。
- 对确实截断或结构无效的 PDF，应返回“文件结构无效或文件不完整”的逐文件失败说明，不得生成伪摘要。
- 若旧版本曾留下损坏的 `managed-snapshots` 快照，再次读取时应从受管原文件自动校验并原子修复，
  无需清库；受管原文件本身保持不变。

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
5. 使用另一个普通用户的 token 请求第 3 步面向上传 Document 的内部索引概览 URL。

通过标准：

- F01/F06 的 `status=COMPLETED`，`chunk_count`、`evidence_count` 均大于 0，PDF Chunk 有真实页码。
- F05 的每个工作表都有独立定位；证据包含真实 `sheet_name` 和 `cell_range`，不能只用页码代替。
- 所有结果的 `embedding_status=DISABLED`；没有模型下载、GPU 进程或外部 embedding 请求。
- 重复处理复用同一版本索引，不增加同一 `document_version_id + extraction_run_id + config_hash` 的运行。
- 重命名或移动工作副本后索引仍复用；只有正文产生新 DocumentVersion 或解析配置变化才建立新索引。
- API 响应不包含 `text_content`、`search_text`、`search_vector`、`embedding`、绝对路径或全文。
- 上传 Document 的内部索引概览仍按上传用户隔离，其他用户请求返回 404；共享 `ACTIVE` 工作副本
  的检索和预览另按共享访问入口验证。
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
关于科研的文档。
查找与科研有关的文档。
关于科研的文件。
哪个文件提到了公示期限？
找包含资助金额的表格。
打开2026年的学生工作文件。
总结刚才上传的PDF。
总结一下述职报告-鲁晓锋-20200421.pdf。
述职报告-鲁晓锋-20200421.pdf 总结一下这个文档。
把统计表的每个工作表分别概括一下。
按单位汇总统计表中的人数或金额。
金海燕的资助总金额是多少？
给这些文件生成标准化文件名建议，但先不要改。
```

通过标准：

- 搜索只返回唯一共享系统工作区的活动工作副本；其他用户的个人会话、附件上下文、上传来源、反馈和
  确认记录永远不出现。
- 最终文件名、分类、元数据和摘要先参与低耗文档级召回；摘要遗漏但原文含“公示期限”的文件仍须
  经 Chunk 补召回命中，并显示真实页码。
- XLSX 命中“资助金额”时显示真实 Sheet 与单元格范围；不能通过文件名猜测位置。
- “刚才这些文件”只检索该轮后端确认的个人会话附件；全局文件请求可以在唯一共享工作区检索。
- “关于科研的文档”“查找与科研有关的文档”“关于科研的文件”必须归一为同一个
  `RELATED` 主题查询，并返回相同的有序文件集合；该要求在两阶段检索开启、显式关闭或异常降级时都成立。
- 结果卡默认显示前 10 个，点击“查看更多”每次追加最多 10 个；点击“查看文件”能通过鉴权下载或预览，
  不依赖相对路径。
- 页面和普通消息/API 响应不显示 Skill、Tool、Chunk、内部路径、搜索词项、SQL 分数或完整正文。
- Excel 数字汇总由确定性表格服务完成，不能让 LLM 心算。
- 当前消息只提交附件 ID 时，后端必须用授权文档记录补全真实文件名和类型；上述人员金额问题应进入
  表格分析而不是文件分类。明确的“人员 + 总金额”应在关闭 LLM 时仍能跨结构兼容 Sheet 筛选求和，
  并展示筛选字段、分 Sheet 明细及真实单元格依据。
- 对话中出现完整文件名时，前后两种“总结”语序都必须精确定位同一个活动工作副本；不能把“这个”
  或“总结一下”误识别成目录或文件名关键词。
- `LLM_ENABLED=false` 或文档阅读 LLM 暂不可用时，系统仍须基于持久化的完整
  `document_pages` 使用本地 CPU 抽取式摘要返回自然语言要点，不能只返回短预览或“暂无业务结果”；
  技术文档中的 `import`、安装命令、循环和纯代码行不得占据摘要主体。
- 本地模式应明确标识为“本地抽取式”，启用
  `LLM_ENABLED=true + CHAT_DOCUMENT_SUMMARY_PROVIDER=llm` 后才属于生成式归纳，不能把抽取句冒充
  模型生成的综合结论。
- 完整文件名确实不存在、尚未同步或正文解析失败时，页面必须展示明确原因，不能显示通用成功兜底。
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

1. 在已上传测试文件的会话中输入：`删除刚刚上传的文件。`
   也可以验证 `把刚才上传的附件删掉`、`这个文件我不要了`、`把它删了` 等不含“回收站”的口语表达。
   对会话中已出现的唯一文件，还必须验证 `删除2024科研成果资助汇总表`、
   `删除2024科研成果资助汇总表.xlsx` 和 `删除整个工作簿文件`。
2. 页面应展示“移入回收站计划”，先刷新页面确认计划仍在等待确认且文件未变化。
3. 点击“确认移入回收站”。
4. 刷新页面并找到最初的历史附件卡片，确认卡片仍保留，但状态变成
   “已删除（在回收站，可恢复）”；普通查看被禁用，并显示“恢复”入口。
5. 点击历史附件卡片中的“恢复”，确认系统只生成“恢复文件计划”，不会直接移动文件。
   也可以输入：`恢复刚才删除的文件。`
6. 页面应展示“恢复文件计划”，确认前文件仍未恢复。
7. 点击“确认恢复”，刷新后确认历史附件卡片恢复为可查看状态，再输入：`读取刚才恢复的文件。`
8. 再次移入回收站后，输入完整文件名，例如：`查找《待删除通知.txt》`。
9. 页面必须提示该文件已删除并展示恢复单选卡；只输入 `查找有关通知的文件` 时不得搜索回收站。
10. 准备两条同名、同版本的回收站记录后再次按完整文件名查找，确认页面逐条展示“文件 1、文件 2”，
   不预选、不合并；选择并恢复其中一条后，另一条继续留在回收站。

通过标准：

- 创建计划和确认之间不发生物理变化。
- 确认移入回收站后，页面提示文件已进入可恢复回收站。
- 删除表达不得进入 Excel 字段展示或只读表格分析；必须先生成 OperationPlan。
- 对已经在回收站中的同一文件再次删除时，应明确提示“已经在回收站”，不能笼统显示“当前不能删除”；
- 历史附件不会因删除而从聊天记录消失，但必须展示当前 WorkingCopy 状态；数据库为 ACTIVE 而物理文件
  缺失时应显示“文件状态异常”，不能显示为“已删除”或继续允许查看。
  文件仍在后台导入时则提示等待处理完成。
- 确认恢复后，工作副本重新可通过对话读取。
- 原路径冲突时恢复到稳定备用路径，不覆盖其他工作副本。
- 完整文件名精确命中是指消息中出现带扩展名的完整名称，例如
  `打开文件“2024科研成果资助汇总表.xlsx”`、`找到文件名为《面谈名单.xlsx》的文件`、
  `恢复 58号文附件.docx`；`找科研材料`、`查找金海燕相关文件` 等主题查询不属于精确命中。
- 即使候选文件名、版本号和内容完全一致，也必须由用户单选一个回收站条目；本次选择只恢复该条记录。
- 页面不提供永久删除入口；`TRASH_AUTO_PURGE_ENABLED=false`。
- 普通用户看不到物理路径、数据库 ID、Skill、Tool 或内部执行载荷。

### SMOKE-012 分类反馈

步骤：

1. 在 `/chat` 上传并完成一份有多个分类建议的文件，记录当前文件名和工作目录位置。
2. 在分类卡上接受一条建议，再用自然语言输入 `这个分类是对的`。
3. 对另一条建议输入 `这个不是科研材料`。
4. 输入 `这个不是科研材料，是干部考察材料`，目标分类必须从当前分类目录选择。
5. 当同一表达可以指向多个文件或多个建议时，在页面选择包含“文件名 + 分类标签”的单选卡。
6. 输入 `按刚才确认的分类整理这个文件`，核对页面只出现共享文件移动计划。
7. 确认计划前检查工作目录路径不变；在页面点击确认移动后再检查目标目录和文件卡。
8. 使用第二个普通用户打开同一活动共享文件，确认正式分类和移动后的文件位置一致。
9. 刷新页面并查询反馈汇总。

```bash
curl -sS http://127.0.0.1:8000/api/classification/feedback/summary \
  -H "Authorization: Bearer ${FILE_AGENT_SMOKE_TOKEN}" | jq
```

通过标准：

- 接受、拒绝和更正都形成追加式反馈记录，并通过同一个正式分类事务写入。
- 更正同时表达原分类负样本和目标分类正样本。
- 接受或更正只绑定当前共享工作副本和当前 DocumentVersion，不能绑定上传暂存 Document。
- 分类决定不会直接移动文件；只有用户明确要求整理时才创建 `MOVE_WORKING_COPIES` OperationPlan。
- 确认前工作副本路径、哈希和版本不变；确认后只移动共享工作副本，受管原件与上传归档原件不变。
- 页面不显示 Tool、taxonomy ID、Neo4j、工作副本 ID 或服务器路径。

只读核验 PostgreSQL。把 `<working_copy_id>`、`<operation_plan_id>` 替换为本次页面回执对应记录：

```sql
SELECT id, working_copy_id, document_version_id, category_id,
       relation_role, status, source
FROM document_categories
WHERE working_copy_id = '<working_copy_id>'
ORDER BY created_at;

SELECT document_category_id, user_id, feedback_id, status
FROM document_category_confirmation_sources
WHERE document_category_id IN (
  SELECT id FROM document_categories
  WHERE working_copy_id = '<working_copy_id>'
)
ORDER BY created_at;

SELECT id, document_category_id, expected_status, state_version,
       status, attempt_count, error_code
FROM classification_graph_outbox
WHERE working_copy_id = '<working_copy_id>'
ORDER BY created_at;

SELECT operation_type, status, confirmed_at, executed_at
FROM operation_plans
WHERE id = '<operation_plan_id>';

SELECT operation_type, before_relative_path, after_relative_path,
       document_version_id, content_sha256, status
FROM working_copy_path_records
WHERE operation_plan_id = '<operation_plan_id>';
```

数据库通过标准：

- 当前版本同一 `PRIMARY` 只有一条 `CONFIRMED` 正式关系。
- 每个确认用户有独立 `ACTIVE` 来源；一个用户撤回不能删除其他用户来源。
- 分类事务同步产生 ChangeSet/ChangeItem 和 Outbox；重复提交不产生重复有效关系。
- 移动记录为 `MOVE`，before/after 可追溯，`document_version_id` 和 `content_sha256` 未变化。

启用 `NEO4J_SYNC_ENABLED=true` 和 `GRAPH_PROJECTION_WORKER_ENABLED=true` 时，再在 Neo4j Browser
只读检查：

```cypher
MATCH (version:DocumentVersion)-[relation:CONFIRMED_AS]->(category:Category)
WHERE version.document_version_id = '<document_version_id>'
RETURN version.filename,
       category.category_id,
       category.path,
       relation.source_type,
       relation.source_id;
```

Neo4j 通过标准：

- `source_type` 为 `formal_classification`，`source_id` 对应 PostgreSQL 正式关系。
- 重启或重复消费 Outbox 不产生重复有效关系。
- Neo4j 不可用时 PostgreSQL 分类仍成功，Outbox 保持 `RETRY`/`PENDING`；Neo4j 恢复后由 worker 补投影。

### SMOKE-013 共享活动文件、个人数据隔离和内部审计权限

步骤：

1. 注册第二个普通用户 `smoke_user_b`。
2. 使用用户 B 访问用户 A 的会话、个人上传 Document、反馈和 OperationPlan。
3. 使用用户 B 从 `/chat` 查找并打开已经导入唯一共享工作目录的 `ACTIVE` 工作副本。
4. 使用用户 A、B 分别访问内部审计接口。

通过标准：

- 用户 B 不能读取或确认用户 A 的个人会话、上传来源、反馈和 OperationPlan，返回 403 或 404。
- 用户 B 可以检索和预览共享 `ACTIVE` 工作副本，但不能看到用户 A 的上传归档路径、会话或附件上下文。
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

API、scheduler 和 worker 都从仓库根启动时，检查仓库根 `logs/` 下的当天 JSONL 日志：

```bash
tail -n 50 logs/file-agent-"$(date +%F)".log

rg -n '"event": "retrieval\.|"event": "working_copy\.search_repair' \
  logs/file-agent-"$(date +%F)".log

rg -n 'Bearer |LLM_API_KEY|password|text_content|病毒扫描通过' \
  logs/file-agent-"$(date +%F)".log
```

Windows PowerShell：

```powershell
$log = ".\logs\file-agent-$(Get-Date -Format yyyy-MM-dd).log"
Get-Content $log -Tail 100
Select-String -Path $log -Pattern '"event": "retrieval\.','"event": "working_copy\.search_repair'
```

Windows CMD 不依赖日期格式时可以检查当天所在的全部日志文件：

```cmd
findstr /I /C:"retrieval." /C:"working_copy.search_repair" logs\file-agent-*.log
```

检索故障按以下事件顺序定位：

```text
retrieval.route.selected
retrieval.query.parsed
retrieval.scope.resolved
retrieval.stage1.completed
retrieval.chunk_fallback.completed / failed
retrieval.stage2.completed / failed / skipped
retrieval.evidence.completed / failed / skipped
retrieval.search.completed
```

历史工作副本缺索引时按以下事件顺序定位：

```text
working_copy.search_repair.queued
working_copy.search_repair.started
working_copy.search_repair.extraction_reused / extraction_started / extraction_failed
working_copy.search_repair.index_started
document.index.completed / failed
working_copy.search_repair.profile_started / profile_failed
working_copy.search_repair.completed
```

通过标准：

- 每行是合法 JSON。
- API 日志包含 request_id；Agent、Tool 和文件事件尽量包含关联 ID 与耗时。
- 检索日志能看到每阶段的候选数、Chunk 命中数、证据数和最终结果数，但不能出现查询正文或文件正文。
- 任一 `retrieval.chunk_fallback.failed`、`retrieval.stage2.failed` 或
  `retrieval.evidence.failed` 只能使当次结果降级；消息接口仍应返回结构化结果，随后在同一会话发送
  普通消息也必须成功。控制台不得出现 `InFailedSqlTransaction` 或 ToolInvocation 写入失败。
- 两阶段主查询整体失败时允许回退文件名和摘要检索；两路都不可用时应显示“文件检索暂时不可用”，
  不能把 ASGI Traceback 展示给普通用户。
- 补建日志能区分缺少文件级 Profile、正文索引、解析结果或证据投影。
- 敏感信息检查命令不应发现 JWT、密码、API key、文件全文或虚假病毒扫描结论。
- 日志不能替代 AgentRun、ToolInvocation、ChangeSet 和 ChangeItem 审计事实。

### SMOKE-016 同义短语与检索歧义选择卡

准备三份已完成解析和索引的测试文件：

```text
A：正文连续包含“任职通知”
B：正文连续包含“任职通告”，但不包含“任职通知”
C：正文分别包含“任职”和“通知”，但没有任何一个完整短语
```

在聊天页依次输入：

```text
哪些文件提到了任职通知
查找与任职通知有关的文件
查找任职通知文件
```

通过标准：

- “提到了”使用正文连续短语证据；A 可以出现，C 不得因为两个拆分词分别命中而出现。
- 如果原短语无结果但“任职通告”等受控同义短语有结果，页面显示业务范围选择卡，不直接扩大结果。
- “有关”允许按完整同义短语扩展，A、B 可以出现，C 仍不能按 `任职 OR 通知` 进入结果。
- 未说明范围且精确与同义结果集合不同，页面显示“只查原短语、包含相近表达、宽泛主题、自定义短语”
  等单选项；系统不得预选宽泛选项。
- 选择“包含相近表达”后只新增一条用户消息和一条 Agent 回答；快速重复点击或重复提交不得产生
  第二份回答。
- 刷新浏览器后，已处理的卡片显示已选择状态，未处理且未过期的卡片仍可继续选择。
- 用另一个账号访问该选择卡 ID 必须失败，不能看到或执行原用户的检索。
- 选择卡和文件结果中不得展示分词、同义词配置路径、Tool、SQL、内部评分或宿主机路径。

### SMOKE-017 阶段五证据问答、总结、歧义和确定性表格

本节只保留整项目烟测入口摘要。完整测试文件、前端问题示例、数据库 SQL、Neo4j 检查边界和逐用例
记录模板见 `docs/stage-5-frontend-backend-acceptance-test-cases.md`。

准备一份包含明确姓名、日期和条款的 DOCX/PDF，一份含多个 Sheet 的 XLSX，以及两份同名但内容不同
的文件。等待 worker 完成正文索引后，只从 `/chat` 页面依次输入：

```text
这个文件要求什么时候提交材料？
完整总结这个文件
查找工作目录中提到金海燕的材料，并说明具体要求
汇总这个表格中金海燕的资助总金额
```

然后把被问答的文件移入回收站，再从历史附件或精确文件名提出同一问题。

通过标准：

- 普通问答和总结返回带 `[1]` 等引用编号的结论；点击编号或下方文件名打开受控文件预览。
- 页面只显示一个最终回答、必要限制和去重文件框，不显示 Tool、Chunk、Evidence ID、页码或单元格定位。
- 全文总结覆盖当前索引中的全部 Chunk；索引未完成时明确提示等待索引，不得把局部摘要冒充全文总结。
- 同名不同内容时显示持久化单选卡，未选择前不回答、不汇总、不计算；刷新后卡片仍可继续选择。
- 文件进入回收站后不再读取旧正文，只显示“文件已删除、是否恢复”的恢复选择卡。
- 表格总额由确定性 Tool 计算；多 Sheet 回答显示各 Sheet 小计和加法公式，不展示“第几行”或单元格定位。
- 重复发送完全相同问题时允许命中安全缓存；把文件替换、删除或更新版本后不得复用旧回答。
- 数据库 `qa_answers` 保存回答模式、请求/证据指纹、Provider、模型和调用统计；
  `answer_references` 绑定回答时的活动工作副本。表格计算血缘保存在 `retrieval_trace_json`。

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
