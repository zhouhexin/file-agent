"""Harness离线入口：反馈、建议、受控复测与只读比较；所有真实调用均须显式live。"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

from pydantic import ValidationError

from app.modules.evidence_harness.feedback import prepare_feedback
from app.modules.evidence_harness.optimizer import (
    check_propose, propose, build_optimizer_client, MAX_OUTPUT_TOKENS, TIMEOUT_SECONDS,
)
from app.modules.evidence_harness.executor import check_execution, run_experiment
from app.modules.evidence_harness.comparator import compare_experiment
from app.modules.evidence_harness.schemas import ExecutionAuthorization
from app.modules.evidence_harness.service import check_cycle, run_cycle, inspect_cycle


def main() -> None:
    """默认只检查；真实建议须--live，异常不回显原文、密钥或验证输入值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--dataset", type=Path, required=True)
    prepare.add_argument("--baseline", type=Path, required=True)
    prepare.add_argument("--history", type=Path, nargs="*", default=[])
    prepare.add_argument("--output", type=Path, required=True)
    advisor = sub.add_parser("propose")
    advisor.add_argument("--feedback", type=Path, required=True)
    advisor.add_argument("--output", type=Path, required=True)
    advisor.add_argument("--live", action="store_true", help="请求一次真实建议模型，会消耗Token；不执行候选")
    execution = sub.add_parser("run")
    execution.add_argument("--feedback", type=Path, required=True)
    execution.add_argument("--proposal", type=Path, required=True)
    execution.add_argument("--output", type=Path, required=True)
    execution.add_argument("--max-requests", type=int, default=19)
    execution.add_argument("--max-active-seconds", type=int, default=1800)
    execution.add_argument("--live", action="store_true", help="显式执行控制与候选，各三题三轮，会发送原文证据给已配置模型")
    comparison = sub.add_parser("compare")
    comparison.add_argument("--experiment", type=Path, required=True)
    cycle = sub.add_parser("cycle")
    cycle.add_argument("--dataset", type=Path, required=True)
    cycle.add_argument("--baseline", type=Path, required=True)
    cycle.add_argument("--history", type=Path, nargs="*", default=[])
    cycle.add_argument("--experiments", type=Path, nargs="*", default=[])
    cycle.add_argument("--previous-cycle", type=Path, help="显式开始新一轮并继承前轮历史，不恢复前轮请求")
    cycle.add_argument("--output", type=Path, required=True)
    cycle.add_argument("--max-requests", type=int, default=19)
    cycle.add_argument("--max-active-seconds", type=int, default=1800)
    cycle.add_argument("--live", action="store_true", help="执行一个候选的完整离线闭环，最多19次请求；默认只检查")
    inspection = sub.add_parser("inspect-cycle")
    inspection.add_argument("--cycle", type=Path, required=True)
    routing_prepare = sub.add_parser("routing-prepare")
    routing_prepare.add_argument("--user-id", required=True)
    routing_prepare.add_argument("--run-ids", nargs="+", required=True)
    routing_prepare.add_argument("--case-ids", nargs="*", default=[])
    routing_prepare.add_argument("--output", type=Path, required=True)
    routing_freeze = sub.add_parser("routing-freeze")
    routing_freeze.add_argument("--inputs", type=Path, required=True)
    routing_freeze.add_argument("--gold", type=Path, required=True)
    routing_freeze.add_argument("--output", type=Path, required=True)
    routing_cycle = sub.add_parser("routing-cycle")
    routing_cycle.add_argument("--dataset", type=Path, required=True)
    routing_cycle.add_argument("--previous", type=Path, nargs="*", default=[])
    routing_cycle.add_argument("--output", type=Path, required=True)
    routing_cycle.add_argument("--repetitions", type=int, default=1)
    routing_cycle.add_argument("--max-requests", type=int, default=19)
    routing_cycle.add_argument("--max-active-seconds", type=int, default=1800)
    routing_cycle.add_argument("--advisor-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=3000,
                               help="独立的建议器输出额度；规划请求输出额度为3000")
    routing_cycle.add_argument("--live", action="store_true", help="真实建议及首轮规划复测，默认只检查")
    routing_compare = sub.add_parser("routing-compare")
    routing_compare.add_argument("--experiment", type=Path, required=True)
    self_feedback = sub.add_parser("self-feedback", help="只读整理机器可读根因反馈，不调用模型")
    self_feedback.add_argument("--experiment", type=Path, required=True)
    self_feedback.add_argument("--output", type=Path, required=True)
    self_propose = sub.add_parser("self-propose", help="生成受限代码补丁，默认只检查")
    self_propose.add_argument("--feedback", type=Path, required=True)
    self_propose.add_argument("--output", type=Path, required=True)
    self_propose.add_argument("--advisor-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=8000)
    self_propose.add_argument("--live", action="store_true")
    self_sandbox = sub.add_parser("self-sandbox-check", help="本机隔离探针，不调用模型")
    self_sandbox.add_argument("--output", type=Path, required=True)
    self_compare = sub.add_parser("self-compare", help="只读重新计算自主补丁实验结果")
    self_compare.add_argument("--experiment", type=Path, required=True)
    self_cycle = sub.add_parser("self-cycle", help="在总预算内自主生成和验证补丁，默认只检查")
    self_cycle.add_argument("--feedback", type=Path, required=True)
    self_cycle.add_argument("--dataset", type=Path, required=True)
    self_cycle.add_argument("--output", type=Path, required=True)
    self_cycle.add_argument("--max-rounds", type=int, default=1)
    self_cycle.add_argument("--max-requests", type=int, default=19)
    self_cycle.add_argument("--max-active-seconds", type=int, default=1800)
    self_cycle.add_argument("--max-no-progress", type=int, default=2)
    self_cycle.add_argument("--max-infrastructure-failures", type=int, default=2)
    self_cycle.add_argument("--repetitions", type=int, default=1)
    self_cycle.add_argument("--advisor-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=8000)
    self_cycle.add_argument("--planner-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=3000)
    self_cycle.add_argument("--live", action="store_true")
    planning_evaluate = sub.add_parser("planning-evaluate", help="固定Adaptive或legacy模式的独立规划基线，默认只检查")
    planning_evaluate.add_argument("--dataset", type=Path, required=True)
    planning_evaluate.add_argument("--output", type=Path, required=True)
    planning_evaluate.add_argument("--planner-mode", choices=("enabled", "legacy"), required=True)
    planning_evaluate.add_argument("--repetitions", type=int, default=1)
    planning_evaluate.add_argument("--max-requests", type=int, default=6)
    planning_evaluate.add_argument("--max-active-seconds", type=int, default=1800)
    planning_evaluate.add_argument("--planner-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=3000)
    planning_evaluate.add_argument("--live", action="store_true")
    planning_compare = sub.add_parser("planning-compare", help="只读重放模型结构，核验实际模式和首轮路线")
    planning_compare.add_argument("--experiment", type=Path, required=True)
    adaptive_cycle = sub.add_parser("adaptive-cycle", help="B阶段自主生成通用规划说明并隔离对照，默认只检查")
    adaptive_cycle.add_argument("--baseline", type=Path, required=True)
    adaptive_cycle.add_argument("--output", type=Path, required=True)
    adaptive_cycle.add_argument("--repetitions", type=int, default=1)
    adaptive_cycle.add_argument("--max-requests", type=int, default=13)
    adaptive_cycle.add_argument("--max-active-seconds", type=int, default=1800)
    adaptive_cycle.add_argument("--advisor-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=8000)
    adaptive_cycle.add_argument("--planner-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=3000)
    adaptive_cycle.add_argument("--live", action="store_true")
    adaptive_compare = sub.add_parser("adaptive-compare", help="只读重算B阶段的提示候选及两组实际规划")
    adaptive_compare.add_argument("--experiment", type=Path, required=True)
    validation_prepare = sub.add_parser("adaptive-validation-prepare", help="合并真实任务，生成C人工分组/路由草稿，零模型调用")
    validation_prepare.add_argument("--candidate", type=Path, required=True)
    validation_prepare.add_argument("--inputs", type=Path, nargs="*", default=[])
    validation_prepare.add_argument("--output", type=Path, required=True)
    validation_freeze = sub.add_parser("adaptive-validation-freeze", help="校验人工标签、原输入和分集，另存C冻结题库")
    validation_freeze.add_argument("--draft", type=Path, required=True)
    validation_freeze.add_argument("--annotations", type=Path)
    validation_freeze.add_argument("--output", type=Path, required=True)
    validation_run = sub.add_parser("adaptive-validate", help="锁定同一候选扩大重复对照，默认只检查，显式live才请求模型")
    validation_run.add_argument("--dataset", type=Path, required=True)
    validation_run.add_argument("--output", type=Path, required=True)
    validation_run.add_argument("--max-requests", type=int, required=True)
    validation_run.add_argument("--max-active-seconds", type=int, default=3600)
    validation_run.add_argument("--repetitions", type=int, choices=(2, 3), default=3)
    validation_run.add_argument("--planner-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=3000)
    # 只有操作者明确选择后，才能在新模型上对同一固定候选做独立对照。
    validation_run.add_argument("--allow-model-profile-change", action=argparse.BooleanOptionalAction, default=None,
        help="覆盖env的模型变更许可；--no-allow-model-profile-change可明确关闭")
    validation_run.add_argument("--provider", choices=("openai-compatible", "codex-login"), default=None,
        help="覆盖EVIDENCE_HARNESS_PROVIDER，仅选择本次C的宿主模型代理")
    validation_run.add_argument("--codex-model", help="覆盖EVIDENCE_HARNESS_CODEX_MODEL，不在运行中读取Codex隐式默认模型")
    validation_run.add_argument("--codex-transport", choices=("auto", "https"), help="覆盖env中的Codex连接方式")
    validation_run.add_argument("--live", action="store_true")
    validation_compare = sub.add_parser("adaptive-validation-compare", help="只读重放C的固定候选、分集和全部重复试次")
    validation_compare.add_argument("--experiment", type=Path, required=True)
    iteration_run = sub.add_parser("adaptive-iterate", help="自动诊断开发反馈、有限生成候选并隔离复测；默认零模型调用")
    iteration_run.add_argument("--source", type=Path, required=True, help="已完成并独立核验的C实验目录")
    iteration_run.add_argument("--output", type=Path, required=True)
    iteration_run.add_argument("--max-rounds", type=int, choices=(1, 2, 3), default=2)
    iteration_run.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=2)
    iteration_run.add_argument("--max-requests", type=int, required=True)
    iteration_run.add_argument("--max-active-seconds", type=int, default=7200)
    iteration_run.add_argument("--max-no-progress", type=int, default=2)
    iteration_run.add_argument("--advisor-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=8000)
    iteration_run.add_argument("--planner-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=3000)
    iteration_run.add_argument("--provider", choices=("openai-compatible", "codex-login"), default=None)
    iteration_run.add_argument("--codex-model")
    iteration_run.add_argument("--codex-transport", choices=("auto", "https"))
    iteration_run.add_argument("--allow-model-profile-change", action=argparse.BooleanOptionalAction, default=None)
    iteration_run.add_argument("--live", action="store_true")
    iteration_compare = sub.add_parser("adaptive-iteration-compare", help="零调用只读重建逐轮诊断、建议连接与独立评分")
    iteration_compare.add_argument("--experiment", type=Path, required=True)
    campaign_run = sub.add_parser("campaign-run", help="承接开发历史，逐候选冻结重复测试，批次结束统一比较；默认零调用")
    campaign_run.add_argument("--source", type=Path, required=True)
    campaign_run.add_argument("--history", type=Path, nargs="*", default=[])
    campaign_run.add_argument("--output", type=Path, required=True)
    campaign_run.add_argument("--max-rounds", type=int, choices=range(1, 11), default=6)
    campaign_run.add_argument("--max-no-progress", type=int, default=6)
    campaign_run.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=1)
    campaign_compare = sub.add_parser("campaign-compare", help="只读重建开发历史、冻结条件和全部候选排名")
    campaign_compare.add_argument("--experiment", type=Path, required=True)
    campaign_lock = sub.add_parser("campaign-lock", help="零模型调用，独立核验后锁定合格入选方案")
    campaign_lock.add_argument("--campaign", type=Path, required=True)
    campaign_lock.add_argument("--output", type=Path, required=True)
    campaign_confirm = sub.add_parser("campaign-confirm", help="只测试已锁定的一份候选，不调用建议器；默认只检查")
    campaign_confirm.add_argument("--lock", type=Path, required=True)
    campaign_confirm.add_argument("--split", choices=("development", "holdout"), required=True)
    campaign_confirm.add_argument("--output", type=Path, required=True)
    confirm_compare = sub.add_parser("campaign-confirm-compare", help="只读重算锁定候选的开发确认或保留验收")
    confirm_compare.add_argument("--experiment", type=Path, required=True)
    campaign_retest = sub.add_parser("campaign-retest", help="筛选有改善迹象的前K份候选，冻结后增加开发重复测量；默认零调用")
    campaign_retest.add_argument("--campaign", type=Path, required=True)
    campaign_retest.add_argument("--output", type=Path, required=True)
    campaign_retest.add_argument("--top-k", type=int, choices=(1, 2, 3), default=2)
    retest_compare = sub.add_parser("campaign-retest-compare", help="只读重算研究复测，最终推荐标准保持原样")
    retest_compare.add_argument("--experiment", type=Path, required=True)
    auto_loop = sub.add_parser("campaign-loop", help="统一预算自动探索、固定复测、反馈再探索；默认零调用")
    auto_loop.add_argument("--source", type=Path, required=True)
    auto_loop.add_argument("--reference", type=Path, help="启用参考方案模式，指定永久保留的旧方案实验")
    auto_loop.add_argument("--reference-round", type=int, help="旧方案在开发迭代或复测批次中的轮次；原C实验不用填")
    auto_loop.add_argument("--strategy", choices=("mixed", "inherit", "fresh"), default="mixed")
    auto_loop.add_argument("--fresh-every", type=int, choices=range(2, 11), default=4)
    auto_loop.add_argument("--output", type=Path, required=True)
    auto_loop.add_argument("--max-cycles", type=int, choices=(1, 2, 3), default=2)
    auto_loop.add_argument("--max-no-progress-cycles", type=int, choices=(1, 2, 3), default=2)
    auto_loop.add_argument("--max-rounds", type=int, choices=range(1, 11), default=6)
    auto_loop.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=1)
    auto_loop.add_argument("--top-k", type=int, choices=(1, 2, 3), default=2)
    auto_loop.add_argument("--retest-repetitions", type=int, choices=range(2, 11), default=5)
    auto_loop.add_argument("--advisor-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=8000)
    loop_compare = sub.add_parser("campaign-loop-compare", help="只读重建自动循环的来源、预算、反馈连接与原始评分")
    loop_compare.add_argument("--experiment", type=Path, required=True)
    for command in (campaign_confirm, campaign_retest):
        command.add_argument("--repetitions", type=int, choices=range(2, 11), default=5)
    for command in (campaign_run, campaign_confirm, campaign_retest, auto_loop):
        # 新入口复用既有env选择，测试期间固定模型及额度，不向候选交付凭证。
        command.add_argument("--max-requests", type=int, required=True)
        command.add_argument("--max-active-seconds", type=int, default=7200)
        command.add_argument("--planner-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=3000)
        command.add_argument("--provider", choices=("openai-compatible", "codex-login"), default=None)
        command.add_argument("--codex-model")
        command.add_argument("--codex-transport", choices=("auto", "https"))
        command.add_argument("--allow-model-profile-change", action=argparse.BooleanOptionalAction, default=None)
        command.add_argument("--live", action="store_true")
    campaign_run.add_argument("--advisor-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=8000)
    auto_loop.set_defaults(max_active_seconds=14400)
    repair_freeze = sub.add_parser("repair-freeze", help="诊断后逐版冻结受限补丁并模拟，不选优、不自动真实对照")
    repair_freeze.add_argument("--source", type=Path, required=True)
    repair_freeze.add_argument("--reference", type=Path, required=True)
    repair_freeze.add_argument("--reference-round", type=int)
    repair_freeze.add_argument("--diagnostics", type=Path, required=True)
    repair_freeze.add_argument("--max-versions", type=int, choices=range(1, 6), default=3)
    repair_test = sub.add_parser("repair-test", help="固定旧参考，测试指定或全部冻结版本；没有留用或胜者替换")
    repair_test.add_argument("--batch", type=Path, required=True)
    repair_selection = repair_test.add_mutually_exclusive_group(required=True)
    repair_selection.add_argument("--version", action="append", help="四位版本号，可重复指定")
    repair_selection.add_argument("--all", action="store_true", help="显式测试全部冻结版本，共用一次总预算")
    for command in (repair_freeze, repair_test):
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--max-requests", type=int, required=True)
        command.add_argument("--max-active-seconds", type=int, default=3600)
        command.add_argument("--repetitions", type=int, choices=(2, 3), default=2)
        command.add_argument("--max-consecutive-failures", type=int, choices=range(1, 6), default=3)
        command.add_argument("--planner-max-output-tokens", type=int, choices=(3000, 6000, 8000), default=3000)
        command.add_argument("--provider", choices=("openai-compatible", "codex-login"), default=None)
        command.add_argument("--codex-model")
        command.add_argument("--codex-transport", choices=("auto", "https"))
        command.add_argument("--allow-model-profile-change", action=argparse.BooleanOptionalAction, default=None)
        command.add_argument("--live", action="store_true")
    for name in ("repair-list", "repair-compare"):
        reader = sub.add_parser(name, help="只读重建所有版本与结果，不登录或调用模型")
        reader.add_argument("--experiment", type=Path, required=True)
    codex_probe = sub.add_parser("codex-probe", help="使用同一测试env检查Codex；显式live只以固定假问题调用一次")
    codex_probe.add_argument("--codex-model", help="可选，覆盖env模型名")
    codex_probe.add_argument("--codex-transport", choices=("auto", "https"), help="覆盖env中的Codex连接方式")
    codex_probe.add_argument("--live", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "codex-probe":
            from app.core.config import get_settings
            from app.modules.evidence_harness.config import validation_provider_options
            from app.modules.evidence_harness.improvement import codex_backend
            settings = get_settings()  # 复用独立env加载；不使用真实题目、Gold或文件，不写入实验目录。
            if args.live and not settings.llm_enabled:
                raise ValueError("CODEX_PROBE_LLM_DISABLED")
            selected, sources = validation_provider_options(codex_model=args.codex_model, codex_transport=args.codex_transport)
            if selected["provider"] != "codex-login":
                raise ValueError("CODEX_PROBE_REQUIRES_CODEX_PROVIDER")
            result = {**codex_backend.probe(selected["codex_model"], live=args.live,
                **({"transport": selected["codex_transport"]} if "codex_transport" in selected else {})), "configuration_sources": sources}
        elif args.action.startswith("repair-"):
            from app.modules.evidence_harness import repair_versions
            if args.action in {"repair-list", "repair-compare"}:
                result = repair_versions.compare(args.experiment)
            else:
                from app.core.config import get_settings
                from app.modules.evidence_harness.config import validation_provider_options
                settings = get_settings()
                selected, sources = validation_provider_options(provider=args.provider, codex_model=args.codex_model,
                    codex_transport=args.codex_transport, allow_model_profile_change=args.allow_model_profile_change)
                auth = repair_versions.Authorization(max_total_requests=args.max_requests,
                    max_active_seconds=args.max_active_seconds, repetitions=args.repetitions,
                    max_consecutive_failures=args.max_consecutive_failures,
                    planner_max_output_tokens=args.planner_max_output_tokens,
                    max_versions=args.max_versions if args.action == "repair-freeze" else 3)
                options = dict(output=args.output, settings=settings, authorization=auth, **selected)
                if args.action == "repair-freeze":
                    options.update(source_dir=args.source, reference_dir=args.reference,
                        reference_round=args.reference_round, diagnostics=args.diagnostics)
                    checker, runner = repair_versions.freeze_check, repair_versions.freeze
                else:
                    options.update(batch_dir=args.batch, versions=None if args.all else args.version)
                    checker, runner = repair_versions.test_check, repair_versions.test
                if args.live:
                    result = runner(**options)
                else:
                    checked = checker(**options)
                    result = {"stage": "CHECK_PASSED", "model_calls": 0, "configuration_sources": sources,
                        "case_count": len(checked["cases"]), "profile": checked["profile"],
                        "planned_request_upper_bound": checked["planned_request_upper_bound"],
                        "authorization_covers_upper_bound": args.max_requests >= checked["planned_request_upper_bound"],
                        "versions": [{k: v[k] for k in ("version", "status", "testable")}
                            for v in checked.get("selected", [])],
                        "automatically_adopted": False, "reference_updated": False}
        elif args.action.startswith("campaign-"):
            from app.modules.evidence_harness.improvement import campaign, campaign_confirmation, campaign_retesting, iteration_comparator, adaptive_validation
            if args.action == "campaign-loop-compare":
                from app.modules.evidence_harness import campaign_loop, reference_loop
                from app.modules.evidence_harness import feedback as harness_store
                from app.modules.evidence_harness.optimizer import _directory
                # 新协议独立重放，旧模式的控制器和评分身份保持原样。
                directory = _directory(args.experiment)
                metadata_path = directory / "experiment.json"
                version = harness_store._read(metadata_path)[0].get("schema_version") if metadata_path.exists() else None
                result = (reference_loop if version == reference_loop.VERSION else campaign_loop).compare(directory)
            elif args.action in {"campaign-compare", "campaign-confirm-compare", "campaign-retest-compare"}:
                comparator = {"campaign-compare": iteration_comparator.compare, "campaign-confirm-compare": campaign_confirmation.compare,
                    "campaign-retest-compare": campaign_retesting.compare}[args.action]
                result = comparator(args.experiment)
            elif args.action == "campaign-lock":
                result = campaign.lock(campaign_dir=args.campaign, output=args.output)
            else:
                from app.core.config import get_settings
                from app.modules.evidence_harness.config import validation_provider_options
                settings = get_settings()
                selected, sources = validation_provider_options(provider=args.provider, codex_model=args.codex_model,
                    codex_transport=args.codex_transport, allow_model_profile_change=args.allow_model_profile_change)
                common = dict(output=args.output, settings=settings, **selected)
                if args.action == "campaign-loop":
                    from app.modules.evidence_harness import campaign_loop, reference_loop
                    selected_loop = reference_loop if args.reference else campaign_loop
                    if not args.reference:
                        common.pop("allow_model_profile_change")
                        if args.reference_round is not None or args.strategy != "mixed" or args.fresh_every != 4:
                            raise ValueError("REFERENCE_OPTION_REQUIRES_REFERENCE")
                    auth = selected_loop.Authorization(max_cycles=args.max_cycles, max_no_progress_cycles=args.max_no_progress_cycles,
                        max_rounds=args.max_rounds, max_no_progress=args.max_rounds, repetitions=args.repetitions,
                        top_k=args.top_k, retest_repetitions=args.retest_repetitions, max_total_requests=args.max_requests,
                        max_active_seconds=args.max_active_seconds, advisor_max_output_tokens=args.advisor_max_output_tokens,
                        planner_max_output_tokens=args.planner_max_output_tokens,
                        **({"strategy": args.strategy, "fresh_every": args.fresh_every} if args.reference else {}))
                    options = dict(**common, source_dir=args.source, authorization=auth)
                    if args.reference:
                        options.update(reference_dir=args.reference, reference_round=args.reference_round)
                    checker, runner = selected_loop.check, selected_loop.run
                elif args.action == "campaign-run":
                    auth = campaign.CampaignAuthorization(max_rounds=args.max_rounds, max_no_progress=args.max_no_progress,
                        repetitions=args.repetitions, max_total_requests=args.max_requests, max_active_seconds=args.max_active_seconds,
                        advisor_max_output_tokens=args.advisor_max_output_tokens, planner_max_output_tokens=args.planner_max_output_tokens)
                    options = dict(**common, source_dir=args.source, history_dirs=args.history, authorization=auth)
                    checker, runner = campaign.check, campaign.run
                elif args.action == "campaign-retest":
                    common.pop("allow_model_profile_change")
                    auth = campaign_retesting.RetestAuthorization(top_k=args.top_k, repetitions=args.repetitions,
                        max_total_requests=args.max_requests, max_active_seconds=args.max_active_seconds,
                        planner_max_output_tokens=args.planner_max_output_tokens)
                    options = dict(**common, campaign_dir=args.campaign, authorization=auth)
                    checker, runner = campaign_retesting.check, campaign_retesting.run
                else:
                    common.pop("allow_model_profile_change")  # 确认阶段必须与锁定Profile一致，不接受放宽。
                    auth = campaign_confirmation.ConfirmationAuthorization(repetitions=args.repetitions, max_total_requests=args.max_requests,
                        max_active_seconds=args.max_active_seconds, planner_max_output_tokens=args.planner_max_output_tokens)
                    options = dict(**common, lock_dir=args.lock, split=args.split.upper(), authorization=auth)
                    checker, runner = campaign_confirmation.check, campaign_confirmation.run
                if not args.live:
                    checked = checker(**options)
                    result = {"stage": "CHECK_PASSED", "model_calls": 0, "configuration_sources": sources,
                        "case_count": len(checked["cases"]), "profile": checked["profile"], "authorization": auth.model_dump(),
                        "planned_request_upper_bound": checked["planned_request_upper_bound"],
                        "authorization_covers_upper_bound": auth.max_total_requests >= checked["planned_request_upper_bound"],
                        "history_count": len(checked.get("history_sources", [])), "historical_candidate_count": len(checked.get("historical_candidates", [])),
                        "historical_requests_used": sum(h["requests_used"] for h in checked.get("history_sources", [])),
                        "initial_preflight_case_ids": checked.get("initial_preflight_case_ids", []),
                        "retest_candidates": [r["id"] for r in checked.get("chosen", [])],
                        "automatically_adopted": False}
                    if "advisor_request_characters" in checked:
                        result.update(advisor_request_characters=checked["advisor_request_characters"],
                            advisor_request_limit=checked["advisor_request_limit"])
                    if "reference_candidate" in checked:
                        result.update(reference_mode=True, reference_policy_ref=checked["reference_candidate"]["prompt_ref"],
                            reference_source=checked["reference"]["path"], strategy=auth.strategy)
                else:
                    import signal
                    def interrupt_campaign(_signal, _frame):
                        """保存本批已开始请求，清理隔离进程；不自动续费或重启下一批。"""
                        raise KeyboardInterrupt
                    previous = signal.signal(signal.SIGTERM, interrupt_campaign)
                    try:
                        result = runner(**options)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
        elif args.action in {"adaptive-iterate", "adaptive-iteration-compare"}:
            from app.modules.evidence_harness.improvement import adaptive_iteration, iteration_comparator
            if args.action == "adaptive-iteration-compare":
                result = iteration_comparator.compare(args.experiment)
            else:
                from app.core.config import get_settings
                from app.modules.evidence_harness.config import validation_provider_options
                settings = get_settings()
                selected, sources = validation_provider_options(provider=args.provider, codex_model=args.codex_model,
                    codex_transport=args.codex_transport, allow_model_profile_change=args.allow_model_profile_change)
                auth = adaptive_iteration.IterationAuthorization(max_rounds=args.max_rounds, repetitions=args.repetitions,
                    max_total_requests=args.max_requests, max_active_seconds=args.max_active_seconds,
                    max_no_progress=args.max_no_progress, advisor_max_output_tokens=args.advisor_max_output_tokens,
                    planner_max_output_tokens=args.planner_max_output_tokens)
                options = dict(source_dir=args.source, output=args.output, settings=settings, authorization=auth, **selected)
                if not args.live:
                    checked = adaptive_iteration.check(**options)
                    result = {"stage": "CHECK_PASSED", "model_calls": 0, "configuration_sources": sources,
                        "development_case_count": len(checked["cases"]), "holdout_usage": "EXCLUDED_FROM_ITERATION",
                        "diagnostic_items": len(checked["feedback"]["items"]),
                        "parameter_diagnostic_count": sum(len(i["parameter_diagnostics"]) for i in checked["feedback"]["items"]),
                        "authorization": auth.model_dump(), "profile": checked["profile"],
                        "provider": checked["provider"], "planned_request_upper_bound": checked["planned_request_upper_bound"],
                        "authorization_covers_upper_bound": checked["authorization_covers_upper_bound"], "automatically_adopted": False}
                else:
                    import signal
                    def interrupt_iteration(_signal, _frame):
                        """中断保留已开始调用和当前轮次，清理隔离进程，不自动恢复。"""
                        raise KeyboardInterrupt
                    previous = signal.signal(signal.SIGTERM, interrupt_iteration)
                    try:
                        result = adaptive_iteration.run(**options)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
        elif args.action.startswith("adaptive-validation-") or args.action == "adaptive-validate":
            from app.modules.evidence_harness.improvement import validation_dataset, adaptive_validation
            if args.action == "adaptive-validation-prepare":
                result = validation_dataset.prepare(candidate_dir=args.candidate, inputs_paths=args.inputs, output=args.output)
            elif args.action == "adaptive-validation-freeze":
                result = validation_dataset.freeze(draft_path=args.draft, output=args.output, annotations_path=args.annotations)
            elif args.action == "adaptive-validation-compare":
                result = adaptive_validation.compare(args.experiment)
            else:
                from app.core.config import get_settings
                from app.modules.evidence_harness.config import validation_provider_options
                # 先复用现有dotenv加载，再将最终选择作为明确参数传入受控C执行器。
                settings = get_settings()
                selected, configuration_sources = validation_provider_options(provider=args.provider, codex_model=args.codex_model,
                    allow_model_profile_change=args.allow_model_profile_change, codex_transport=args.codex_transport)
                authorization = adaptive_validation.ValidationAuthorization(max_total_requests=args.max_requests,
                    max_active_seconds=args.max_active_seconds, repetitions=args.repetitions, planner_max_output_tokens=args.planner_max_output_tokens)
                options = dict(dataset_path=args.dataset, output=args.output, settings=settings, authorization=authorization, **selected)
                if not args.live:
                    checked = adaptive_validation.check(**options)
                    result = {"stage": "CHECK_PASSED", "model_calls": 0, "advisor_calls": 0, "configuration_sources": configuration_sources,
                        "case_count": len(checked["cases"]), "split_counts": dict(Counter(a.split for a in checked["dataset"].annotations)),
                        "locked_policy_ref": checked["locked"]["candidate"]["prompt_ref"], "authorization": authorization.model_dump(),
                        **{k: checked[k] for k in ("planned_request_upper_bound", "authorization_covers_upper_bound", "worst_request_wait_seconds", "time_budget_covers_request_wait_only",
                            "origin_profile", "profile", "allow_model_profile_change", "model_changed", "endpoint_changed", "comparison_scope", "provider")}}
                else:
                    import signal
                    def interrupt_validation(_signal, _frame):
                        """保存开始请求和剩余试次，清理隔离进程，不自动恢复或补跑。"""
                        raise KeyboardInterrupt
                    previous = signal.signal(signal.SIGTERM, interrupt_validation)
                    try:
                        result = adaptive_validation.run(**options)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
        elif args.action.startswith("adaptive-"):
            from app.core.config import get_settings
            from app.modules.evidence_harness.improvement import adaptive_policy
            if args.action == "adaptive-compare":
                result = adaptive_policy.compare(args.experiment)
            else:
                authorization = adaptive_policy.PolicyAuthorization(repetitions=args.repetitions,
                    max_total_requests=args.max_requests, max_active_seconds=args.max_active_seconds,
                    advisor_max_output_tokens=args.advisor_max_output_tokens, planner_max_output_tokens=args.planner_max_output_tokens)
                options = dict(baseline_dir=args.baseline, output=args.output, settings=get_settings(), authorization=authorization)
                if not args.live:
                    checked = adaptive_policy.check_cycle(**options)
                    result = {"stage": "CHECK_PASSED", "model_calls": 0, "planner_mode": "enabled",
                        "case_count": len(checked["cases"]), "baseline_correct": checked["report"]["correct"],
                        "feedback": adaptive_policy.feedback_summary(checked["feedback"]),
                        "authorization": authorization.model_dump(), "planned_request_upper_bound": checked["planned_request_upper_bound"],
                        "authorization_covers_upper_bound": checked["authorization_covers_upper_bound"],
                        "change_kind": "MODEL_GENERATED_PROMPT_APPENDIX", "automatically_adopted": False}
                else:
                    import signal
                    def interrupt_policy(_signal, _frame):
                        """先退出隔离子进程并记录已开始请求，不自动补发或续跑。"""
                        raise KeyboardInterrupt
                    previous = signal.signal(signal.SIGTERM, interrupt_policy)
                    try:
                        result = adaptive_policy.run_cycle(**options)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
        elif args.action.startswith("planning-"):
            from app.core.config import get_settings
            from app.modules.evidence_harness.improvement import service, comparator
            from app.modules.evidence_harness.improvement.schemas import PlanningEvaluationAuthorization
            if args.action == "planning-compare":
                result = comparator.compare_planning_evaluation(args.experiment)
            else:
                authorization = PlanningEvaluationAuthorization(max_total_requests=args.max_requests,
                    max_active_seconds=args.max_active_seconds, repetitions=args.repetitions,
                    planner_max_output_tokens=args.planner_max_output_tokens)
                options = dict(dataset_path=args.dataset, output=args.output, planner_mode=args.planner_mode,
                               settings=get_settings(), authorization=authorization)
                if not args.live:
                    checked = service.check_planning_evaluation(**options)
                    result = {"stage": "CHECK_PASSED", "model_calls": 0, "planner_mode": args.planner_mode,
                              "case_count": len(checked["cases"]), "authorization": authorization.model_dump(),
                              "execution_inputs_ref": checked["execution_inputs_ref"],
                              "planned_request_upper_bound": checked["planned_request_upper_bound"],
                              "authorization_covers_upper_bound": checked["authorization_covers_upper_bound"],
                              "evaluation_level": "INITIAL_ROUTING", "candidate_generated": False}
                else:
                    import signal
                    def interrupt_planning(_signal, _frame):
                        """中断先清理隔离进程再保存已付费调用，不能自动补发。"""
                        raise KeyboardInterrupt
                    previous = signal.signal(signal.SIGTERM, interrupt_planning)
                    try:
                        result = service.run_planning_evaluation(**options)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
        elif args.action.startswith("self-"):
            # 新协议不复用预置策略的建议格式，模型实际生成受限源码差异。
            from app.core.config import get_settings
            from app.modules.evidence_harness.improvement import feedback, service, comparator, isolation
            from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization
            if args.action == "self-feedback":
                result = feedback.prepare_feedback(experiment=args.experiment, output=args.output)
            elif args.action == "self-compare":
                result = comparator.compare(args.experiment)
            elif args.action == "self-sandbox-check":
                output = service.new_output(args.output)
                output.mkdir(parents=True, mode=0o700, exist_ok=False)
                isolation.snapshot(output / "code")
                checks = isolation.probe_sandbox(output / "code", output)
                from app.modules.evidence_evaluation.frozen_inputs import write_new_json
                result = {"stage": "SANDBOX_VERIFIED", "checks": checks, "model_calls": 0}
                if sys.platform == "win32":
                    from app.modules.evidence_harness.improvement import local_runtime
                    result.update(stage="LOCAL_TEST_VERIFIED", runtime=local_runtime.runtime())
                write_new_json(output / "manifest.json", result)
            elif args.action == "self-propose":
                authorization = ImprovementAuthorization(advisor_max_output_tokens=args.advisor_max_output_tokens)
                if args.live:
                    result = service.propose_only(feedback_dir=args.feedback, output=args.output,
                        settings=get_settings(), authorization=authorization)
                else:
                    feedback.load_feedback(args.feedback)
                    service.new_output(args.output)
                    result = {"stage": "CHECK_PASSED", "model_calls": 0, "candidate_executed": False,
                              "advisor_max_output_tokens": authorization.advisor_max_output_tokens}
            else:
                authorization = ImprovementAuthorization(max_rounds=args.max_rounds, max_total_requests=args.max_requests,
                    max_active_seconds=args.max_active_seconds, max_no_progress=args.max_no_progress,
                    max_infrastructure_failures=args.max_infrastructure_failures, repetitions=args.repetitions,
                    advisor_max_output_tokens=args.advisor_max_output_tokens,
                    planner_max_output_tokens=args.planner_max_output_tokens)
                options = dict(feedback_dir=args.feedback, dataset_path=args.dataset, output=args.output,
                               settings=get_settings(), authorization=authorization)
                if args.live:
                    import signal
                    def interrupt_self(_signal, _frame):
                        """终止信号也保存预算和已完成记录，不自动续发请求。"""
                        raise KeyboardInterrupt
                    previous = signal.signal(signal.SIGTERM, interrupt_self)
                    try:
                        result = service.run_cycle(**options)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
                else:
                    checked = service.check_cycle(**options)
                    result = {"stage": "CHECK_PASSED", "model_calls": 0,
                              "case_count": len(checked["dataset"].inputs.cases),
                              "planned_request_upper_bound": checked["planned_request_upper_bound"],
                              "authorization_covers_upper_bound": checked["authorization_covers_upper_bound"],
                              "authorization": authorization.model_dump(), "evaluation_level": "INITIAL_ROUTING"}
        elif args.action.startswith("routing-"):
            # 路由实验使用独立协议，旧A阶段反馈及执行入口继续按原协议加载。
            from app.core.config import get_settings
            from app.modules.evidence_harness.routing import dataset, service, comparator
            from app.modules.evidence_harness.routing.schemas import RoutingAuthorization
            if args.action == "routing-prepare":
                result = dataset.prepare(user_id=args.user_id, run_ids=args.run_ids, case_ids=args.case_ids,
                                         output=args.output, settings=get_settings())
            elif args.action == "routing-freeze":
                result = dataset.freeze(inputs_path=args.inputs, gold_path=args.gold, output=args.output)
            elif args.action == "routing-compare":
                result = comparator.compare(args.experiment)
            else:
                authorization = RoutingAuthorization(max_total_requests=args.max_requests,
                    max_active_seconds=args.max_active_seconds, repetitions=args.repetitions,
                    advisor_max_output_tokens=args.advisor_max_output_tokens)
                kwargs = dict(dataset_path=args.dataset, output=args.output, history=args.previous,
                              settings=get_settings(), authorization=authorization, source="live")
                if args.live:
                    import signal
                    def interrupt_routing(_signal, _frame):
                        """正常终止沿统一中断路径保存可能已计费的发送记录。"""
                        raise KeyboardInterrupt
                    previous = signal.signal(signal.SIGTERM, interrupt_routing)
                    try:
                        result = service.run_cycle(**kwargs)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
                else:
                    checked = service.check_cycle(**kwargs)
                    result = {"stage": "CHECK_PASSED", "evaluation_level": "INITIAL_ROUTING", "model_calls": 0,
                        "case_count": len(checked["dataset"].inputs.cases),
                        "planned_request_upper_bound": checked["planned_request_upper_bound"],
                        "authorization_covers_upper_bound": checked["authorization_covers_upper_bound"],
                        "request_limits": {"advisor_max_output_tokens": authorization.advisor_max_output_tokens,
                                           "planner_max_output_tokens": service.MAX_OUTPUT_TOKENS,
                                           "timeout_seconds": service.TIMEOUT_SECONDS},
                        "authorization": authorization.model_dump()}
        elif args.action == "prepare":
            result = prepare_feedback(dataset_path=args.dataset, baseline_path=args.baseline,
                                      history_paths=args.history, output=args.output)
        elif args.action == "propose":
            packet, _payload, _output = check_propose(feedback_dir=args.feedback, output=args.output)
            if not args.live:
                result = {"stage": "CHECK_PASSED", "model_calls": 0, "candidate_executed": False,
                          "limits": {"max_calls": 1, "max_output_tokens": MAX_OUTPUT_TOKENS,
                                     "timeout_seconds": TIMEOUT_SECONDS},
                          "message": "反馈和目标检查通过；追加--live才请求一次建议模型。"}
            else:
                from app.core.config import get_settings
                client = build_optimizer_client(get_settings(), packet)
                result = propose(feedback_dir=args.feedback, output=args.output, client=client, source="live")
        elif args.action == "run":
            from app.core.config import get_settings
            authorization = ExecutionAuthorization(max_total_requests=args.max_requests,
                                                   max_active_seconds=args.max_active_seconds)
            kwargs = dict(feedback_dir=args.feedback, proposal_dir=args.proposal, output=args.output,
                          settings=get_settings(), authorization=authorization, source="live")
            context = check_execution(**kwargs)
            if not args.live:
                result = {"stage": "CHECK_PASSED", "new_model_calls": 0,
                          "planned_new_requests": 18, "prior_optimizer_requests": 1,
                          "authorization": authorization.model_dump(),
                          "profiles": {k: v.model_dump() for k, v in context["profiles"].items()}}
            else:
                import signal
                def interrupt(_signal, _frame):
                    """正常SIGTERM沿人工中断路径清理请求进程并持久化已完成记录。"""
                    raise KeyboardInterrupt
                previous = signal.signal(signal.SIGTERM, interrupt)
                try:
                    result = run_experiment(**kwargs)
                finally:
                    signal.signal(signal.SIGTERM, previous)
        elif args.action == "cycle":
            from app.core.config import get_settings
            authorization = ExecutionAuthorization(max_total_requests=args.max_requests,
                                                   max_active_seconds=args.max_active_seconds)
            kwargs = dict(dataset_path=args.dataset, baseline_path=args.baseline, history_paths=args.history,
                experiment_dirs=args.experiments, output=args.output, settings=get_settings(),
                authorization=authorization, source="live", previous_cycle=args.previous_cycle)
            context = check_cycle(**kwargs)
            if not args.live:
                result = {"stage": "CHECK_PASSED", "model_calls": 0, "max_candidates": 1,
                          "history_count": len(context["packet"].experiment_history),
                          "authorization": authorization.model_dump()}
            else:
                import signal
                def interrupt_cycle(_signal, _frame):
                    """统一中断后保留阶段记录，不触发自动恢复或第二个候选。"""
                    raise KeyboardInterrupt
                previous = signal.signal(signal.SIGTERM, interrupt_cycle)
                try:
                    result = run_cycle(**kwargs)
                finally:
                    signal.signal(signal.SIGTERM, previous)
        elif args.action == "inspect-cycle":
            result = inspect_cycle(args.cycle)
        else:
            result = compare_experiment(args.experiment)
    except Exception as exc:
        # 不展开ValidationError，它包含输入值；固定说明让用户保留现场核对。
        code = (
            "REPORT_SCHEMA_INVALID" if isinstance(exc, ValidationError)
            else "OUTPUT_ALREADY_EXISTS" if isinstance(exc, FileExistsError)
            else str(exc) if type(exc) is ValueError and re.fullmatch(r"[A-Z_]+", str(exc))
            else str(exc) if type(exc) is RuntimeError and re.fullmatch(
                r"(?:HARNESS_(?:DOCKER|CODEX|CONTAINER|NATIVE)|CONTAINER_SECCOMP|OS_SANDBOX)_[A-Z_]+", str(exc))
            else "INPUT_JSON_INVALID" if isinstance(exc, (json.JSONDecodeError, UnicodeError))
            else "INPUT_OR_OUTPUT_INVALID"
        )
        parser.exit(2, f"Harness操作未完成：{code}；请核对输入、配置及私有产物，勿自动重试。\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if (result.get("stage") in {"OPTIMIZER_FAILED", "INTERRUPTED", "STOPPED", "FAILED", "INVALID_CYCLE", "INVALID_EXPERIMENT"}
        or result.get("decision") == "INVALID_EXPERIMENT" or result.get("comparison") == "INVALID_EXPERIMENT"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
