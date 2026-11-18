"""隔离进程的固定入口，仅规划和编译，不接受命令、密钥、Gold或Tool handler。"""

import json
import logging
from pathlib import Path
import sys


def emit(value):
    """所有IPC采用单行JSON，业务日志不能混入协议通道。"""
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


class BrokerClient:
    """模型凭证和请求预算留在父进程，子进程只提出原规划服务的请求。"""

    def complete_json(self, **kwargs):
        """错误只接收安全类别，父进程拒绝的请求不能绕过代理重发。"""
        from app.modules.llm.client import LLMResponseError
        from app.modules.llm.diagnostics import make_diagnostic
        emit({"type": "MODEL_REQUEST", **kwargs})
        response = json.loads(sys.stdin.readline())
        if response.get("status") != "COMPLETED":
            raise LLMResponseError("BROKER_MODEL_FAILED", diagnostic=make_diagnostic(
                category="INTERNAL_ERROR", stage="LLM_REQUEST"))
        return response["raw"]


def configure_test_settings(raw):
    """先绑定当前独立进程的明确配置，再导入业务组件，避免任何上级.env读取。"""
    from app.core import config
    mode = raw.get("adaptive_planner_mode") if isinstance(raw, dict) else None
    if mode not in {"legacy", "enabled"}:
        raise ValueError("ISOLATED_TEST_SETTINGS_INVALID")
    expected = {"database_url": "postgresql://offline:offline@127.0.0.1/file_agent_unit_tests",
                "llm_enabled": True, "adaptive_planner_mode": mode, "log_dir": str(Path.cwd() / "logs")}
    if not isinstance(raw, dict) or set(raw) != set(expected) or any(
        type(raw[k]) is not type(v) or raw[k] != v for k, v in expected.items()
    ):
        raise ValueError("ISOLATED_TEST_SETTINGS_INVALID")
    settings = config.Settings(**raw)

    def explicit_settings():
        """子进程内所有新导入组件共享同一明确配置，密钥使用空默认值。"""
        return settings

    # 仅改当前一次性worker的模块绑定；宿主及在线服务不受影响。
    # 必须早于业务模块的from app.core.config import get_settings。
    config.get_settings = explicit_settings
    return settings


def main():
    """仅固定模式可运行，不导入比较器或人工标签。"""
    logging.disable(logging.CRITICAL)
    request = json.loads(sys.stdin.readline())
    try:
        settings = configure_test_settings(request.get("test_settings"))
        if request["mode"] == "compile":
            for path in Path(__file__).resolve().parents[3].rglob("*.py"):
                compile(path.read_bytes(), str(path), "exec")
            result = {"compiled": True}
        elif request["mode"] == "regression":
            from app.modules.agent.planner import build_plan_from_user_intent
            from app.modules.llm.schemas import UserIntentPlan
            result = build_plan_from_user_intent(intent_plan=UserIntentPlan.model_validate(request["intent"]),
                message=request["message"], attachments=request["attachments"]).model_dump(mode="json")
        elif request["mode"] == "planning":
            from app.modules.evidence_harness.routing.runtime import run_planning
            from app.modules.evidence_harness.routing.schemas import RoutingCase
            case = RoutingCase.model_validate(request["case"])
            if case.adaptive_mode != settings.adaptive_planner_mode:
                raise ValueError("ISOLATED_CASE_MODE_MISMATCH")
            result = run_planning(case, catalog=request["catalog"], client=BrokerClient(), settings=settings,
                                  capture_audit=request.get("capture_audit") is True)
        elif request["mode"] == "probe":
            # 探针仅用于本机隔离验收，路径和动作由可信测试程序固定。
            import socket
            import subprocess
            actions = {
                "read": lambda: Path(request["path"]).read_text(),
                "write": lambda: Path(request["path"]).write_text("forbidden"),
                "network": lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
                "exec": lambda: subprocess.run(["/usr/bin/true"], check=True),
            }
            try:
                actions[request["action"]]()
                result = {"blocked": False}
            except PermissionError:
                result = {"blocked": True}
        else:
            raise ValueError("MODE_NOT_ALLOWED")
        emit({"type": "RESULT", "status": "COMPLETED", "result": result})
    except Exception as exc:
        # 仅异常类型和Python模块名可用于隔离依赖诊断，不转储异常原文或任务。
        emit({"type": "RESULT", "status": "FAILED", "error_code": "ISOLATED_WORKER_FAILED",
              "error_type": type(exc).__name__,
              "missing_module": exc.name if isinstance(exc, ModuleNotFoundError) else None,
              "blocked_basename": Path(exc.filename).name if isinstance(exc, PermissionError) and exc.filename else None})


if __name__ == "__main__":
    main()
