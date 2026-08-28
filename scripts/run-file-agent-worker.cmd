@echo off
rem 在子 CMD 内固定 worker 身份和队列，避免一键启动多个窗口时继承到其他 worker 的环境变量。

setlocal EnableExtensions
if "%~3"=="" (
    echo [File Agent] Usage: run-file-agent-worker.cmd WORKER_ID QUEUES PYTHON_EXE
    exit /b 2
)

set "FILESYSTEM_WORKER_ID=%~1"
set "FILESYSTEM_WORKER_QUEUES=%~2"
set "FILE_AGENT_WORKER_PYTHON=%~3"
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
set "PYTHONPATH=%PROJECT_ROOT%\apps\api"

pushd "%PROJECT_ROOT%"
if errorlevel 1 (
    echo [File Agent] Project root is unavailable: %PROJECT_ROOT%
    exit /b 1
)

echo [File Agent] Starting worker_id=%FILESYSTEM_WORKER_ID% queues=%FILESYSTEM_WORKER_QUEUES%
"%FILE_AGENT_WORKER_PYTHON%" -m app.modules.managed_files.worker
set "WORKER_EXIT_CODE=%ERRORLEVEL%"
popd

if not "%WORKER_EXIT_CODE%"=="0" (
    echo [File Agent] Worker exited with code %WORKER_EXIT_CODE%.
)
exit /b %WORKER_EXIT_CODE%
