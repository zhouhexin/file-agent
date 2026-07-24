"""Windows CMD worker 启动脚本的静态契约测试。"""

from pathlib import Path


def test_windows_worker_launcher_starts_required_isolated_processes():
    """脚本必须先预检再启动隔离进程，不能让扫描 worker 抢占旧路径任务。"""

    script = (
        Path(__file__).resolve().parents[4]
        / "scripts"
        / "start-file-agent-workers.cmd"
    )
    content = script.read_text(encoding="utf-8")
    assert 'FILESYSTEM_WORKER_QUEUES=RECONCILE,SCAN' in content
    assert 'FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE,IMPORT,FILE_OPERATION' in content
    assert 'app.modules.managed_files.worker' in content
    assert 'app.modules.file_lifecycle.scheduler' in content
    assert 'app.modules.file_lifecycle.startup_preflight' in content
    assert 'start "File Agent - Scan Worker"' in content
    assert 'start "File Agent - Lifecycle Worker"' in content
    assert "if errorlevel 1" in content
    assert content.index("startup_preflight") < content.index('start "File Agent - Scan Worker"')
    assert content.index('start "File Agent - Lifecycle Scheduler"') < content.index(
        'start "File Agent - Scan Worker"'
    )
    assert '"%ComSpec%" /D /K' in content
