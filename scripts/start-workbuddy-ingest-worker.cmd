@echo off
rem WorkBuddy 批次附带请求使用独立 AGENT 队列，避免阻塞文件复制与 OCR 任务。

setlocal EnableExtensions
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
pushd "%PROJECT_ROOT%"
if errorlevel 1 exit /b 1

if not defined FILE_AGENT_PYTHON set "FILE_AGENT_PYTHON=python"
set "PYTHONPATH=%PROJECT_ROOT%\apps\api"
set "FILESYSTEM_WORKER_ID=workbuddy-agent-worker"
set "FILESYSTEM_WORKER_QUEUES=AGENT"
"%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker

popd
exit /b 0
