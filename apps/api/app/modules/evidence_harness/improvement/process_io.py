"""受控子进程管道与清理：兼容 Windows 管道，限制缓存并关闭本次 Job。"""

import os
import queue
import signal
import threading


class PipeReader:
    """单个有界读取线程，显式区分暂无输出、EOF 与读取失败。"""

    def __init__(self, stream):
        self.stream = stream
        self.events = queue.Queue(maxsize=4)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _put(self, value):
        while not self.stopped.is_set():
            try:
                self.events.put(value, timeout=.1)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            while not self.stopped.is_set():
                block = os.read(self.stream.fileno(), 65536)
                self._put(block)
                if not block:
                    return
        except (OSError, ValueError) as exc:
            self._put(exc)

    def read(self, timeout):
        """在截止时间内取一个数据块，避免 Windows 管道依赖 select。"""
        try:
            item = self.events.get(timeout=max(0, timeout))
        except queue.Empty:
            return None
        if isinstance(item, Exception):
            raise RuntimeError("SUBPROCESS_PIPE_FAILED") from None
        return item

    def close(self):
        """停止排队并等待读取线程退出，不保留后台无限队列。"""
        self.stopped.set()
        self.thread.join(timeout=2)


def stop_process(process):
    """Windows 关闭本次 Job，POSIX 清理本次进程组，不影响其他服务。"""
    job = getattr(process, "_harness_job", None)
    if job is not None:
        job.close()
    if os.name == "nt":
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)
