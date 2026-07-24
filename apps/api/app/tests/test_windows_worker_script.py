"""Windows CMD worker 启动脚本的静态契约测试。"""

from pathlib import Path


def test_windows_worker_launcher_starts_required_isolated_processes():
    """脚本必须启动扫描、导入生命周期和 scheduler，不能把所有队列塞进同一 worker。"""

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
    assert 'start "File Agent - Scan Worker"' in content
    assert 'start "File Agent - Lifecycle Worker"' in content
