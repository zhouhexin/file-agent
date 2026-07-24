@echo off
rem File Agent Windows CMD worker 启动器。
rem 使用独立窗口并行执行扫描和导入，启动前必须完成当前机器目录配置预检。

setlocal EnableExtensions
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"

rem 可以在执行脚本前把 FILE_AGENT_PYTHON 设置为当前环境解释器的绝对路径。
if not defined FILE_AGENT_PYTHON set "FILE_AGENT_PYTHON=python"
"%FILE_AGENT_PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo [File Agent] Python unavailable. Set FILE_AGENT_PYTHON to the configured interpreter path.
    exit /b 1
)

rem 后端包位于 apps/api，不强制用户切换或新建 Python 环境。
set "PYTHONPATH=%PROJECT_ROOT%\apps\api"

echo [File Agent] Synchronizing and validating managed roots before workers start...
"%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.startup_preflight
if errorlevel 1 (
    echo [File Agent] Startup cancelled. Fix the reported configuration before retrying.
    exit /b 1
)

echo [File Agent] Starting scheduler, scan worker, and lifecycle/import worker...

rem 预检把本机路径提交到数据库后才能启动 scheduler，避免扫描读取其他机器的旧路径。
set "FILESYSTEM_WORKER_ID="
set "FILESYSTEM_WORKER_QUEUES="
start "File Agent - Lifecycle Scheduler" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.scheduler"

rem 对账和扫描只发现原始文件；复制和导入由独立生命周期 worker 消费。
set "FILESYSTEM_WORKER_ID=reconcile-scan-worker"
set "FILESYSTEM_WORKER_QUEUES=RECONCILE,SCAN"
start "File Agent - Scan Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem 上传查重、归档写入、导入和已确认文件操作共用生命周期 worker。
set "FILESYSTEM_WORKER_ID=import-lifecycle-worker"
set "FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE,IMPORT,FILE_OPERATION"
start "File Agent - Lifecycle Worker" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

if /I "%~1"=="--with-watcher" (
    rem watcher 是可选进程；scheduler 轮询已经提供最终一致的目录同步。
    set "FILESYSTEM_WORKER_ID="
    set "FILESYSTEM_WORKER_QUEUES="
    start "File Agent - Managed Root Watcher" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.watcher"
)

echo [File Agent] Worker windows were started. Close each worker window or press Ctrl+C inside it to stop.
exit /b 0
