"""独立输出额度实验入口：先核对冻结基线，仅显式--run才调用真实模型。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "runtime/evidence-test"
CONFIG = TEST_ROOT / "experiments/output-budget-6000.json"
DATASET = TEST_ROOT / "datasets/frozen-real-v1/cases-v1.json"
BASELINE = TEST_ROOT / "datasets/frozen-real-v1/runs/live-v1-01.json"

_spec = importlib.util.spec_from_file_location(
    "evidence_test_launcher", PROJECT_ROOT / "scripts/run_evidence_test.py"
)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


def read_budget(path: Path) -> int:
    """配置只接受输出额度，不允许在此混入密钥、超时或其他变量。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"max_output_tokens"}:
        raise ValueError("配置只允许max_output_tokens一个字段")
    budget = data["max_output_tokens"]
    if type(budget) is not int or not 3000 < budget <= 8000:
        raise ValueError("本次对照输出额度须为3001至8000之间的整数")
    return budget


def experiment_code(budget: int, *, run: bool) -> str:
    """复用隔离目录校验，核对单变量与输入散列后才允许独立评测。"""
    output = DATASET.parent / f"runs/live-output{budget}-v1-01.json"
    # 只使用受控文件常量与整数生成Python代码，不构造或执行Shell字符串。
    return launcher.PREFLIGHT + f'''
import json
import sys
from app.modules.evidence_evaluation.frozen_inputs import digest, runtime_config
from app.scripts.evaluate_evidence import load_cases, main as evaluate_main
baseline = json.loads(Path({str(BASELINE)!r}).read_text(encoding='utf-8'))
cases = load_cases(Path({str(DATASET)!r}))
if baseline['mode'] != 'live' or baseline['repetitions'] != 3 or baseline['sample_count'] != 3:
    raise SystemExit('拒绝执行：基线必须是原三题三轮live报告。')
if baseline['dataset_sha256'] != digest([c.model_dump(mode='json') for c in cases]):
    raise SystemExit('拒绝执行：输入或标签与原基线不同。')
actual = runtime_config(s)
expected = baseline['runtime_config']
changes = sorted(k for k in set(actual) | set(expected) if actual.get(k) != expected.get(k))
if changes != ['max_output_tokens'] or actual['max_output_tokens'] != {budget}:
    raise SystemExit('拒绝执行：除输出额度外，还有配置或评估代码发生变化；差异字段：' + ','.join(changes))
if not s.llm_enabled:
    raise SystemExit('拒绝执行：独立测试配置未开启LLM。')
output = Path({str(output)!r})
if output.exists():
    raise SystemExit('拒绝覆盖已有实验报告：请先核对已有结果。')
print('检查通过：三题输入和人工标签一致；仅输出额度从' + str(expected['max_output_tokens']) + '变为' + str(actual['max_output_tokens']) + '；超时仍为' + str(actual['timeout_seconds']) + '秒。', flush=True)
if not {run!r}:
    print('本次仅检查，没有连接数据库或调用模型。')
else:
    sys.argv = ['evaluate_evidence', '--dataset', {str(DATASET)!r}, '--mode', 'live',
                '--repetitions', '3', '--output', str(output)]
    evaluate_main()
'''


def main() -> None:
    """默认只检查；保留原env不变，子进程仅注入一个已校验的输出额度。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="检查通过后执行三题三轮live，会调用模型")
    args = parser.parse_args()
    if not (TEST_ROOT / ".env").is_file():
        parser.error("缺少独立测试.env，不会读取项目根配置")
    try:
        budget = read_budget(CONFIG)
    except (ValueError, OSError):
        parser.error("实验配置缺失或无效：仅允许max_output_tokens整数，范围3001至8000")
    env = launcher.isolated_environment(dict(os.environ))
    # 原.env只填未设置变量；本进程覆盖输出额度，其他字段不继承终端残留值。
    env["EVIDENCE_EVALUATION_MAX_OUTPUT_TOKENS"] = str(budget)
    result = subprocess.run(
        [sys.executable, "-c", experiment_code(budget, run=args.run)],
        cwd=TEST_ROOT, env=env, check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
