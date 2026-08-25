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

echo [File Agent] Starting scheduler, scan worker, source analysis worker, materialize workers, analysis worker, structured extraction worker, and graph worker...

rem 预检把本机路径提交到数据库后才能启动 scheduler，避免扫描读取其他机器的旧路径。
set "FILESYSTEM_WORKER_ID="
set "FILESYSTEM_WORKER_QUEUES="
start "File Agent - Lifecycle Scheduler" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.scheduler"

rem 对账和扫描只发现原始文件；初始化只提交 SOURCE_ANALYSIS，不复制全部工作副本。
set "FILESYSTEM_WORKER_ID=reconcile-scan-worker"
set "FILESYSTEM_WORKER_QUEUES=RECONCILE,SCAN"
start "File Agent - Scan Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem 上传查重、归档写入和已确认文件操作由生命周期 worker 消费。
set "FILESYSTEM_WORKER_ID=lifecycle-worker"
set "FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION"
start "File Agent - Lifecycle Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem 源侧分析包括旧 Office 的 LibreOffice 转换；默认一个 worker，避免多个 soffice 抢占资源。
set "FILESYSTEM_WORKER_ID=source-analysis-worker"
set "FILESYSTEM_WORKER_QUEUES=SOURCE_ANALYSIS"
start "File Agent - Source Analysis Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem 只有用户查询、阅读或选择的相关文件才由 MATERIALIZE 复制为工作副本，可独立扩容。
set "FILESYSTEM_WORKER_ID=materialize-worker-1"
set "FILESYSTEM_WORKER_QUEUES=MATERIALIZE,IMPORT"
start "File Agent - Materialize Worker 1" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

set "FILESYSTEM_WORKER_ID=materialize-worker-2"
set "FILESYSTEM_WORKER_QUEUES=MATERIALIZE,IMPORT"
start "File Agent - Materialize Worker 2" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem 工作副本内容发生变化后才在 ANALYSIS 队列重新解析、摘要、分类和建索引。
set "FILESYSTEM_WORKER_ID=analysis-worker"
set "FILESYSTEM_WORKER_QUEUES=ANALYSIS"
start "File Agent - Analysis Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem PP-StructureV3 和字段模型使用独立慢队列；未启用功能时该 worker 保持空闲。
set "FILESYSTEM_WORKER_ID=structured-extraction-worker"
set "FILESYSTEM_WORKER_QUEUES=STRUCTURED_EXTRACTION"
start "File Agent - Structured Extraction Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem Neo4j bootstrap 和正式分类 outbox 由独立 GRAPH worker 消费，不阻塞 API 启动或文件复制。
set "FILESYSTEM_WORKER_ID=graph-worker"
set "FILESYSTEM_WORKER_QUEUES=GRAPH"
start "File Agent - Graph Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

if /I "%~1"=="--with-watcher" (
    rem watcher 是可选进程；scheduler 轮询已经提供最终一致的目录同步。
    set "FILESYSTEM_WORKER_ID="
    set "FILESYSTEM_WORKER_QUEUES="
    start "File Agent - Managed Root Watcher" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.watcher"
)

echo [File Agent] Worker windows were started. Failed jobs stop after at most 3 attempts.
popd
exit /b 0
