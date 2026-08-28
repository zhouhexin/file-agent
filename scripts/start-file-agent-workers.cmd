@echo off
rem File Agent Windows CMD worker 启动器。
rem 使用独立窗口并行执行扫描、源侧分析和按需物化，启动前必须完成当前机器目录配置预检。

setlocal EnableExtensions
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
pushd "%PROJECT_ROOT%"
if errorlevel 1 (
    echo [File Agent] Project root is unavailable. Move the repository to an accessible Windows directory.
    exit /b 1
)

rem 可以在执行脚本前把 FILE_AGENT_PYTHON 设置为当前环境解释器的绝对路径。
if not defined FILE_AGENT_PYTHON set "FILE_AGENT_PYTHON=python"
"%FILE_AGENT_PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo [File Agent] Python unavailable. Set FILE_AGENT_PYTHON to the configured interpreter path.
    popd
    exit /b 1
)

rem 后端包位于 apps/api，不强制用户切换或新建 Python 环境。
set "PYTHONPATH=%PROJECT_ROOT%\apps\api"

echo [File Agent] Synchronizing and validating managed roots before workers start...
"%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.startup_preflight
if errorlevel 1 (
    echo [File Agent] Startup cancelled. Fix the reported configuration before retrying.
    popd
    exit /b 1
)

echo [File Agent] Starting scheduler and five consolidated workers...

rem 预检把本机路径提交到数据库后才能启动 scheduler，避免扫描读取其他机器的旧路径。
set "FILESYSTEM_WORKER_ID="
set "FILESYSTEM_WORKER_QUEUES="
start "File Agent - Lifecycle Scheduler" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.scheduler"

rem 每个 worker 通过子脚本在自己的 CMD 内设置身份和队列，不能依赖父 CMD 的可变环境。
rem 对账和扫描只发现原始文件；初始化只提交 SOURCE_ANALYSIS，不复制全部工作副本。
start "File Agent - Scan Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%PROJECT_ROOT%\scripts\run-file-agent-worker.cmd" "reconcile-scan-worker" "RECONCILE,SCAN" "%FILE_AGENT_PYTHON%""

rem 文件复制和文件生命周期任务共用一个 I/O worker，避免为 MATERIALIZE/IMPORT 额外常驻进程。
start "File Agent - Lifecycle and Materialize Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%PROJECT_ROOT%\scripts\run-file-agent-worker.cmd" "lifecycle-worker" "DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION,MATERIALIZE,IMPORT" "%FILE_AGENT_PYTHON%""

rem 源侧与工作副本分析共用解析 worker；串行使用 LibreOffice，避免多个 soffice 抢占资源。
start "File Agent - Document Analysis Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%PROJECT_ROOT%\scripts\run-file-agent-worker.cmd" "source-analysis-worker" "SOURCE_ANALYSIS,ANALYSIS" "%FILE_AGENT_PYTHON%""

rem PP-StructureV3 和字段模型使用独立慢队列；未启用功能时该 worker 保持空闲。
start "File Agent - Structured Extraction Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%PROJECT_ROOT%\scripts\run-file-agent-worker.cmd" "structured-extraction-worker" "STRUCTURED_EXTRACTION" "%FILE_AGENT_PYTHON%""

rem Neo4j bootstrap 和正式分类 outbox 由独立 GRAPH worker 消费，不阻塞 API 启动或文件复制。
start "File Agent - Graph Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%PROJECT_ROOT%\scripts\run-file-agent-worker.cmd" "graph-worker" "GRAPH" "%FILE_AGENT_PYTHON%""

if /I "%~1"=="--with-watcher" (
    rem watcher 是可选进程；scheduler 轮询已经提供最终一致的目录同步。
    set "FILESYSTEM_WORKER_ID="
    set "FILESYSTEM_WORKER_QUEUES="
    start "File Agent - Managed Root Watcher" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.watcher"
)

echo [File Agent] Worker windows were started. Failed jobs stop after at most 3 attempts.
popd
exit /b 0
