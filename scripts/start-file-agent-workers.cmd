@echo off
rem File Agent Windows CMD worker launcher.
rem Starts isolated consoles so scanning can enqueue imports while the lifecycle worker consumes them.

setlocal EnableExtensions
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"

rem FILE_AGENT_PYTHON may be set to an absolute interpreter path before invoking this script.
if not defined FILE_AGENT_PYTHON set "FILE_AGENT_PYTHON=python"
"%FILE_AGENT_PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo [File Agent] Python unavailable. Set FILE_AGENT_PYTHON to the configured interpreter path.
    exit /b 1
)

rem The backend package is under apps/api. Do not require users to activate a specific virtual environment.
set "PYTHONPATH=%PROJECT_ROOT%\apps\api"

echo [File Agent] Starting scan worker, lifecycle/import worker, and scheduler...

rem Reconciliation and scanning only discover source files; import work is deliberately handled elsewhere.
set "FILESYSTEM_WORKER_ID=reconcile-scan-worker"
set "FILESYSTEM_WORKER_QUEUES=RECONCILE,SCAN"
start "File Agent - Scan Worker" /D "%PROJECT_ROOT%" cmd /k ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem Upload duplicate checks, archive writes, imports, and confirmed file actions share one lifecycle worker.
set "FILESYSTEM_WORKER_ID=import-lifecycle-worker"
set "FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE,IMPORT,FILE_OPERATION"
start "File Agent - Lifecycle Worker" /D "%PROJECT_ROOT%" cmd /k ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem Scheduler only enqueues durable jobs; it never performs direct filesystem work.
set "FILESYSTEM_WORKER_ID="
set "FILESYSTEM_WORKER_QUEUES="
start "File Agent - Lifecycle Scheduler" /D "%PROJECT_ROOT%" cmd /k ""%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.scheduler"

if /I "%~1"=="--with-watcher" (
    rem Watcher is optional because polling/scheduler already provide eventual source synchronization.
    start "File Agent - Managed Root Watcher" /D "%PROJECT_ROOT%" cmd /k ""%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.watcher"
)

echo [File Agent] Worker windows were started. Close each worker window or press Ctrl+C inside it to stop.
exit /b 0
