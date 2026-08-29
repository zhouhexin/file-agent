"""Windows CMD worker 启动脚本的静态契约测试。"""

from pathlib import Path


def test_windows_worker_launcher_starts_required_isolated_processes():
    """脚本必须先预检再启动隔离进程，不能让扫描 worker 抢占旧路径任务。"""

    script = (
        Path(__file__).resolve().parents[4]
        / "scripts"
        / "start-file-agent-workers.cmd"
    )
    runner = script.with_name("run-file-agent-worker.cmd")
    # Windows CMD 在当前部署环境中按 GBK 读取中文注释，测试必须遵守生产脚本编码。
    content = script.read_text(encoding="gbk")
    runner_content = runner.read_text(encoding="gbk")
    assert '"reconcile-scan-worker" "RECONCILE,SCAN"' in content
    assert (
        '"lifecycle-worker" '
        '"DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION,MATERIALIZE,IMPORT"'
    ) in content
    assert '"source-analysis-worker" "SOURCE_ANALYSIS,ANALYSIS"' in content
    assert '"structured-extraction-worker" "STRUCTURED_EXTRACTION"' in content
    assert '"graph-worker" "GRAPH"' in content
    assert content.count("run-file-agent-worker.cmd") == 5
    assert 'set "FILESYSTEM_WORKER_ID=%~1"' in runner_content
    assert 'set "FILESYSTEM_WORKER_QUEUES=%~2"' in runner_content
    assert 'app.modules.managed_files.worker' in runner_content
    assert 'app.modules.file_lifecycle.scheduler' in content
    assert 'app.modules.file_lifecycle.startup_preflight' in content
    assert 'start "File Agent - Scan Worker"' in content
    assert 'start "File Agent - Lifecycle and Materialize Worker"' in content
    assert 'start "File Agent - Document Analysis Worker"' in content
    assert 'start "File Agent - Structured Extraction Worker"' in content
    assert 'start "File Agent - Graph Worker"' in content
    assert "if errorlevel 1" in content
    assert 'pushd "%PROJECT_ROOT%"' in content
    assert "popd" in content
    assert content.index("startup_preflight") < content.index('start "File Agent - Scan Worker"')
    assert content.index('start "File Agent - Lifecycle Scheduler"') < content.index(
        'start "File Agent - Scan Worker"'
    )
    assert '"%ComSpec%" /D /K' in content
