"""离线实验专用子进程传输：单次硬等待上限、零重试，退出时清理请求进程。"""

import multiprocessing
import signal
import time

from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient
from app.modules.llm.diagnostics import SafeErrorDiagnostic, make_diagnostic


def _request_worker(connection, credentials: dict, timeout: int, kwargs: dict):
    """凭证仅经内存IPC传递，不写文件、命令参数、普通日志或实验配置。"""
    # Ctrl-C由父进程统一持久化/清理；测试直接调用此函数时不改变主进程信号。
    if multiprocessing.current_process().name != "MainProcess":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        client = OpenAICompatibleLLMClient(**credentials, timeout_seconds=timeout, max_retries=0)
        raw, usage = client.complete_json_with_usage(**kwargs)
        connection.send(("ok", raw, usage))
    except Exception as exc:
        diagnostic = (exc.diagnostic if isinstance(exc, LLMResponseError) and exc.diagnostic
                      else make_diagnostic(category="INTERNAL_ERROR", stage="LLM_REQUEST", exc=exc))
        try:
            connection.send(("error", diagnostic.model_dump(mode="json")))
        except (BrokenPipeError, EOFError, OSError):
            pass  # 父进程已退出时不在控制台转储异常或请求内容。
    finally:
        connection.close()


class ProcessTransport:
    """父进程控制一次请求的总等待，不依赖HTTP逐块读取超时来实现整轮时限。"""

    def __init__(self, credentials: dict, timeout: int, *, worker=_request_worker):
        """仅受控构造器传入设置；worker参数用于无网络的进程终止测试。"""
        self.credentials, self.timeout, self.worker = credentials, timeout, worker

    def complete_json_with_usage(self, **kwargs):
        """超时或人工中断均终止子进程，不留下后台自动重试；服务器费用可能仍未知。"""
        ctx = multiprocessing.get_context("spawn")
        reader, writer = ctx.Pipe(duplex=False)
        process = ctx.Process(target=self.worker, args=(writer, self.credentials, self.timeout, kwargs), daemon=True)
        started = time.monotonic()
        try:
            process.start()
            writer.close()
            if not reader.poll(max(0, self.timeout - (time.monotonic() - started))):
                raise LLMResponseError("EXPERIMENT_REQUEST_TIMEOUT", diagnostic=make_diagnostic(
                    category="TIMEOUT", stage="LLM_REQUEST", exc=TimeoutError()))
            message = reader.recv()
            if message[0] == "error":
                raise LLMResponseError("EXPERIMENT_PROVIDER_FAILED",
                    diagnostic=SafeErrorDiagnostic.model_validate(message[1]))
            return message[1], message[2]
        finally:
            reader.close(); writer.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill(); process.join(timeout=1)
