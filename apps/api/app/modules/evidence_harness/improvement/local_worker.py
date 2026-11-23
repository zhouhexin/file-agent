"""本地 Python 的可信引导器：先收受控输入、绑定测试配置，再启用审计检查。"""

import json
import importlib.util
import mimetypes
import os
from pathlib import Path
import sys
import sysconfig
import threading


def install_audit(code, scratch):
    """约束受限候选的 Python I/O；不能用于运行任意恶意程序。"""
    read_roots = [code.resolve(), scratch.resolve(), Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
    system = os.environ.get("SYSTEMROOT")
    if system:
        read_roots.append(Path(system).resolve())
    state = threading.local()

    def path_allowed(value, write=False):
        if isinstance(value, int):
            return True  # 已打开的 stdin/stdout 或通过 open 审计建立的文件描述符。
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        path = Path(os.fsdecode(value)).resolve()
        if path.name.casefold() == ".env":
            return False
        return any(path == root or path.is_relative_to(root) for root in ([scratch.resolve()] if write else read_roots))

    def require(value, write=False):
        if not path_allowed(value, write):
            raise PermissionError("HARNESS_LOCAL_PATH_BLOCKED")

    def audit(event, args):
        if getattr(state, "checking", False):
            return
        state.checking = True
        try:
            if event == "open":
                path, mode, flags = args
                write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
                    flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
                require(path, write)
            elif event in {"os.listdir", "os.scandir", "os.chdir"}:
                require(args[0] if args and args[0] is not None else os.getcwd())
            elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
                require(args[0], True)
            elif event in {"os.rename", "os.replace"}:
                require(args[0], True)
                require(args[1], True)
            elif (event.startswith(("socket.", "winreg.", "ctypes."))
                  or event in {"subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.link", "os.symlink"}):
                raise PermissionError("HARNESS_LOCAL_ACTION_BLOCKED")
        finally:
            state.checking = False
    sys.addaudithook(audit)


def main():
    """使用现有环境的标准库和安装包，跳过用户 site 与可执行 .pth。"""
    global Path
    # 只导入可信宿主的路径适配，不全局替换 pathlib，也不把其目录加入候选导入路径。
    path_module = Path(__file__).resolve().parents[1] / "paths.py"
    spec = importlib.util.spec_from_file_location("_trusted_harness_paths", path_module)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    Path = module.Path
    code, scratch = (Path(p).resolve() for p in sys.argv[1:])
    # Job 必须在父进程发送这行之前成功设置；候选导入不能抢先执行。
    request = json.loads(sys.stdin.readline())
    os.chdir(scratch)
    for name in ("purelib", "platlib"):
        path = sysconfig.get_path(name)
        if path and path not in sys.path:
            sys.path.append(path)
    handles = []
    if os.name == "nt":
        dlls = Path(sys.prefix) / "Library/bin"
        if dlls.is_dir():
            handles.append(os.add_dll_directory(str(dlls)))
    import_root = os.fspath(code)
    if os.name == "nt" and not import_root.startswith("\\\\?\\"):
        # 根目录未到 MAX_PATH，内部模块路径也可能超长；导入入口始终使用扩展路径。
        import_root = "\\\\?\\UNC\\" + import_root[2:] if import_root.startswith("\\\\") else "\\\\?\\" + import_root
    sys.path.insert(0, import_root)
    sys.dont_write_bytecode = True
    from app.modules.evidence_harness.improvement import worker
    worker.Path = Path
    worker.configure_test_settings(request.get("test_settings"))
    # 预载可信依赖，避免把 Python 原生扩展的正常初始化当作候选行为。
    from app.modules.agent import graph
    mimetypes.init()
    install_audit(code, scratch)
    original = sys.stdin
    class Input:
        """只回放已验证首行，后续模型代理响应继续读取真实管道。"""
        first = True
        def readline(self):
            if self.first:
                self.first = False
                return json.dumps(request, ensure_ascii=False) + "\n"
            return original.readline()
    sys.stdin = Input()
    worker.main()


if __name__ == "__main__":
    main()
