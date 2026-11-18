"""从独立测试目录启动评估链，隔离原 .env 和旧库/旧文件配置。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "runtime/evidence-test"

# 子进程先校验测试库名前缀及存储路径，再允许建表或启动服务。
# 不读取项目根 .env；配置、数据库口令和正文不得打印到控制台。
PREFLIGHT = '''
from pathlib import Path
from sqlalchemy.engine import make_url
from app.core.config import get_settings
s = get_settings()
db_name = make_url(s.database_url).database or ''
if not db_name.startswith('file_agent_evidence_test'):
    raise SystemExit('拒绝运行：DATABASE_URL必须指向独立的file_agent_evidence_test_测试库。')
for name in ('file_storage_root','managed_root_archive_write_path','working_copy_storage_root','trash_storage_root','log_dir'):
    value = getattr(s,name)
    if not value or not Path(value).resolve().is_relative_to(Path.cwd().resolve()):
        raise SystemExit('拒绝运行：所有存储和日志目录必须位于独立测试目录。')
'''
ONLINE_PREFLIGHT = PREFLIGHT + '''
if not s.llm_enabled or s.evidence_evaluation_mode != 'shadow':
    raise SystemExit('拒绝运行：测试配置须开启LLM和shadow证据评估。')
'''

# 只在harness子进程加载完独立env后使用；不把代理配置传给API、worker或正式服务。
HARNESS_NETWORK_PREFLIGHT = '''
from urllib.parse import urlsplit
proxy = os.environ.get('EVIDENCE_HARNESS_PROXY_URL')
if proxy is not None:
    try:
        parsed = urlsplit(proxy)
        valid = (parsed.scheme in {'http','https'} and bool(parsed.hostname)
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and parsed.path in {'','/'} and not parsed.query and not parsed.fragment
                 and not any(ch.isspace() for ch in proxy))
    except ValueError:
        valid = False
    if not valid:
        raise SystemExit('HARNESS_PROXY_URL_INVALID：请检查独立测试env中的代理URL；地址不回显。')
    for key in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'):
        os.environ[key] = proxy
'''


def isolated_environment(parent: dict[str, str]) -> dict[str, str]:
    """仅传递系统运行变量，排除终端中残留的旧库、密钥和MANAGED_ROOT配置。"""
    allowed = (
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
        "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    )
    result = {key: parent[key] for key in allowed if key in parent}
    result.update(PYTHONPATH=str(PROJECT_ROOT / "apps/api"), PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    return result


def command_code(action: str, arguments: list[str] | None = None, capture_plan: str | None = None) -> str:
    """构造固定启动参数；不接受任意Shell命令或数据库写SQL。"""
    preflight = PREFLIGHT if action in {"frozen", "evaluate", "harness"} else ONLINE_PREFLIGHT
    if action in {"frozen", "evaluate", "harness"}:
        module = {"frozen": "app.scripts.frozen_evidence", "evaluate": "app.scripts.evaluate_evidence",
                  "harness": "app.scripts.evidence_harness"}[action]
        argv = [sys.executable, "-m", module, *(arguments or [])]
        network = HARNESS_NETWORK_PREFLIGHT if action == "harness" else ""
        return preflight + "\nimport os\n" + network + f"\nos.execv({sys.executable!r}, {argv!r})\n"
    if action == "check":
        return preflight + "\nimport jieba\nprint('独立配置校验通过；目标库：'+db_name+'；Jieba：'+jieba.__version__)\nprint('此检查没有连接数据库或LLM。')\n"
    if action == "migrate":
        # 只升级到正式head，不使用create_all冒充迁移，不执行reset/drop。
        return preflight + f'''
from alembic import command
from alembic.config import Config
c = Config({str(PROJECT_ROOT / 'apps/api/alembic.ini')!r})
c.set_main_option('script_location', {str(PROJECT_ROOT / 'apps/api/alembic')!r})
c.set_main_option('prepend_sys_path', {str(PROJECT_ROOT / 'apps/api')!r})
command.upgrade(c, 'head')
print('测试库迁移完成。')
'''
    arguments = [sys.executable, "-m", "app.modules.managed_files.worker"]
    extra = ""
    if action == "api":
        arguments = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
    elif action == "lifecycle":
        extra = "os.environ['FILESYSTEM_WORKER_ID']='evidence-test-lifecycle'\nos.environ['FILESYSTEM_WORKER_QUEUES']='DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION,MATERIALIZE,IMPORT'\n"
    elif action == "analysis":
        extra = "os.environ['FILESYSTEM_WORKER_ID']='evidence-test-analysis'\nos.environ['FILESYSTEM_WORKER_QUEUES']='SOURCE_ANALYSIS,ANALYSIS,AGENT'\n"
    else:
        raise ValueError("未知测试动作")
    if capture_plan:
        extra += f"os.environ['EVIDENCE_EVALUATION_CAPTURE_PLAN']={capture_plan!r}\n"
    return preflight + "\nimport os\n" + extra + f"os.execv({sys.executable!r}, {arguments!r})\n"


def main() -> None:
    """只有显式执行migrate/api/worker动作才产生对应副作用；默认check只校验配置。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "migrate", "api", "lifecycle", "analysis", "frozen", "evaluate", "harness"), default="check", nargs="?")
    parser.add_argument("--capture-plan", type=Path)
    args, remaining = parser.parse_known_args()
    if remaining and args.action not in {"frozen", "evaluate", "harness"}:
        parser.error("此启动动作不接受额外参数")
    capture_plan = None
    if args.capture_plan:
        capture_plan = args.capture_plan.resolve()
        if args.action not in {"api", "analysis"} or not capture_plan.is_relative_to((TEST_ROOT / "datasets").resolve()):
            parser.error("采集计划仅允许api/analysis显式加载，且必须位于独立测试datasets目录")
        if not capture_plan.is_file():
            parser.error("采集计划不存在")
    if not (TEST_ROOT / ".env").is_file():
        parser.error("缺少runtime/evidence-test/.env；不会回退加载项目根.env。")
    result = subprocess.run(
        [sys.executable, "-c", command_code(args.action, remaining, str(capture_plan) if capture_plan else None)], cwd=TEST_ROOT,
        env=isolated_environment(dict(os.environ)), check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
