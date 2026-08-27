@echo off
rem File Agent 上传查重与双栏预览最小 Worker 启动器。
rem 只启动：上传生命周期、工作副本物化/导入、文档分析。

setlocal EnableExtensions
set "PROJECT_ROOT=%~dp0.."
for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"
pushd "%PROJECT_ROOT%"
if errorlevel 1 (
    echo [File Agent] Project root is unavailable.
    exit /b 1
)

rem 可在运行前设置 FILE_AGENT_PYTHON，复用当前已经配置好的 Python 环境。
if not defined FILE_AGENT_PYTHON set "FILE_AGENT_PYTHON=python"
"%FILE_AGENT_PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo [File Agent] Python unavailable. Set FILE_AGENT_PYTHON to the configured interpreter path.
    popd
    exit /b 1
)

set "PYTHONPATH=%PROJECT_ROOT%\apps\api"

echo [File Agent] Validating managed roots...
"%FILE_AGENT_PYTHON%" -m app.modules.file_lifecycle.startup_preflight
if errorlevel 1 (
    echo [File Agent] Startup cancelled. Fix the reported configuration before retrying.
    popd
    exit /b 1
)

rem 负责上传 SHA-256/同名/近似查重、上传归档和确认后的文件操作。
set "FILESYSTEM_WORKER_ID=duplicate-review-lifecycle-worker"
set "FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION"
start "File Agent - Duplicate Review Lifecycle" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem 负责首次上传通过查重后生成共享工作副本。
set "FILESYSTEM_WORKER_ID=duplicate-review-materialize-worker"
set "FILESYSTEM_WORKER_QUEUES=MATERIALIZE,IMPORT"
start "File Agent - Duplicate Review Materialize" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

rem 负责 Office/Excel 正文页生成；双栏预览会复用这些解析结果。
set "FILESYSTEM_WORKER_ID=duplicate-review-analysis-worker"
set "FILESYSTEM_WORKER_QUEUES=ANALYSIS"
start "File Agent - Duplicate Review Analysis" /D "%PROJECT_ROOT%" "%ComSpec%" /D /K ""%FILE_AGENT_PYTHON%" -m app.modules.managed_files.worker"

echo [File Agent] Duplicate review workers started:
echo   - DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION
echo   - MATERIALIZE,IMPORT
echo   - ANALYSIS
popd
exit /b 0
