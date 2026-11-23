"""第四步验收：一条调用串起安全反馈、建议、复测、比较和历史回写，全部使用Fake。"""

from copy import deepcopy
from functools import lru_cache
import json
import os

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness import service, history, optimizer, comparator, policy
from app.modules.evidence_harness.schemas import ExecutionAuthorization, OptimizationProposal
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic
from app.tests.test_evidence_evaluation import response
from app.tests.test_evidence_harness_execution import execution, _run
from app.tests.test_evidence_harness_optimizer import FakeAdvisor, _suggestion
from app.tests.test_evidence_harness_feedback import _save


def _kwargs(execution, name="cycle", experiments=None, authorization=None):
    """构造一次独立授权，旧数据与已有建议均不被覆盖。"""
    return dict(dataset_path=execution["dataset"], baseline_path=execution["dataset"].with_name("baseline.json"),
        history_paths=[], experiment_dirs=experiments or [], output=execution["root"] / f"experiments/harness-v1/{name}",
        settings=execution["settings"], authorization=authorization or ExecutionAuthorization(), source="fake")


def _factory(scenario, calls):
    """按测试指定的语义返回结果，不读取Gold；输出带真实输入证据ID。"""
    def create(arm, profile, case, repetition):
        class Client:
            def complete_json_with_usage(self, **kwargs):
                calls.append((arm, case.case_id, repetition, deepcopy(kwargs)))
                if scenario == "interrupt" and len(calls) == 7:
                    raise KeyboardInterrupt
                if case.case_id == "R034" and (arm == "control" or scenario == "no_gain"):
                    raise LLMResponseError("safe", diagnostic=make_diagnostic(category="TIMEOUT", stage="LLM_REQUEST"))
                if case.case_id == "R001" and arm == "candidate" and scenario == "regress":
                    return response("UNSUPPORTED"), {"input_tokens": 10, "output_tokens": 10}
                return response("UNSUPPORTED" if case.case_id == "R016" else "SUPPORTED"), {"input_tokens": 10, "output_tokens": 10}
        return Client()
    return create


def _advisor(execution, no_change=False):
    return FakeAdvisor(_suggestion(optimizer.load_feedback(execution["feedback_dir"]), no_change=no_change))


@pytest.mark.parametrize("scenario,expected,used", [
    ("improve", "IMPROVED", 19), ("no_gain", "NO_GAIN", 19),
    ("regress", "REGRESSION", 19), ("interrupt", "INTERRUPTED", 8),
])
def test_single_entry_completes_all_stages_and_records_new_history(execution, scenario, expected, used):
    """同一个入口完成全部环节；Fake永远不变成真实成功候选，旧数据字节不变。"""
    calls=[];advisor=_advisor(execution);kwargs=_kwargs(execution)
    protected=[execution["dataset"], *execution["feedback_dir"].glob('*.json')]
    before={str(p):p.read_bytes() for p in protected}
    result=service.run_cycle(**kwargs, advisor_client=advisor, client_factory=_factory(scenario,calls))
    assert result['stage']==expected and result['total_request_attempts']==used
    assert result['model_calls']==0 and result['fake_calls']==used and len(advisor.calls)==1
    assert len(calls)==used-1 and not result['automatically_adopted']
    assert result['history_count_before']==0 and result['history_count_after']==1
    if scenario == 'interrupt':
        assert result['stop_reason'] == 'USER_INTERRUPTED'
    packet=optimizer.load_feedback(kwargs['output']/'feedback-next')
    assert packet.schema_version=='harness-feedback-v2'
    assert packet.experiment_history[0].decision==('INCONCLUSIVE' if scenario=='interrupt' else expected)
    assert service.inspect_cycle(kwargs['output'])==result
    assert before=={str(p):p.read_bytes() for p in protected}
    if os.name != "nt":  # POSIX mode bits do not describe Windows ACLs.
        assert all(p.stat().st_mode & 0o777==0o600 for p in kwargs['output'].rglob('*.json'))


def test_no_gain_history_reaches_actual_request_and_repeated_profile_is_rejected(execution):
    """下一次模型实际看到NO_GAIN与失败诊断，重提120秒仅消耗建议1次且不复测。"""
    _run(execution,'no_gain')
    calls=[];advisor=_advisor(execution);kwargs=_kwargs(execution,experiments=[execution['output']])
    result=service.run_cycle(**kwargs,advisor_client=advisor,client_factory=_factory('improve',calls))
    assert result['stage']=='REJECTED' and result['total_request_attempts']==1
    assert not calls and not (kwargs['output']/'execution').exists()
    payload=advisor.calls[0]['user_payload']
    entry=payload['feedback']['experiment_history'][0]
    assert entry['decision']=='NO_GAIN' and entry['candidate_budget']['timeout_seconds']==120
    assert any(t['failure_kinds']==['TIMEOUT'] for t in entry['trials'])
    validation=json.loads((kwargs['output']/'proposal/validation.json').read_text())
    assert validation['reason_code']=='PREVIOUSLY_UNSUCCESSFUL_EXPERIMENT'
    assert result['stop_reason'] == validation['reason_code']
    sent=json.dumps(payload,ensure_ascii=False)
    for marker in ('GOLD_PRIVATE_SENTINEL','expected_issues','expected_explanation','annotation_sha256','source-index','issue_types','case_id'):
        assert '"'+marker+'"' not in sent
    assert '可定位的真实测试原文' not in sent and 'GOLD_PRIVATE_SENTINEL' not in sent


@pytest.mark.parametrize('kind,stage',[('no_change','NO_CHANGE'),('invalid','REJECTED'),('timeout','OPTIMIZER_FAILED'),('interrupt','INTERRUPTED')])
def test_early_terminal_states_do_not_execute_or_invent_experiments(execution,kind,stage):
    """正常不调整、非法建议、建议失败和人工中断都立即结束，不循环寻优。"""
    calls=[];advisor=_advisor(execution,no_change=kind=='no_change')
    if kind=='invalid':advisor.response['shell']='must not execute'
    if kind=='timeout':advisor.response=LLMResponseError('private',diagnostic=make_diagnostic(category='TIMEOUT',stage='LLM_REQUEST'))
    if kind=='interrupt':advisor.response=KeyboardInterrupt()
    if kind=='interrupt':
        class Interrupted(FakeAdvisor):
            def complete_json_with_usage(self,**kwargs):
                self.calls.append(kwargs);raise KeyboardInterrupt
        advisor=Interrupted(None)
    kwargs=_kwargs(execution)
    result=service.run_cycle(**kwargs,advisor_client=advisor,client_factory=_factory('improve',calls))
    assert result['stage']==stage and result['total_request_attempts']==1
    assert not calls and len(advisor.calls)==1 and result['history_count_after']==0
    assert not (kwargs['output']/'execution').exists()
    assert service.inspect_cycle(kwargs['output'])==result


def test_cycle_budget_includes_advisor_and_stops_before_following_requests(execution):
    """3次总额度包含当前建议调用，最多再发送2次评估请求。"""
    calls=[];kwargs=_kwargs(execution,authorization=ExecutionAuthorization(max_total_requests=3))
    result=service.run_cycle(**kwargs,advisor_client=_advisor(execution),client_factory=_factory('improve',calls))
    assert result['stage']=='STOPPED' and result['total_request_attempts']==3 and len(calls)==2
    assert result['stop_reason'] == 'REQUEST_BUDGET_EXHAUSTED'
    assert result['history_count_after']==1
    assert optimizer.load_feedback(kwargs['output']/'feedback-next').experiment_history[0].decision=='INCONCLUSIVE'


def test_wall_budget_can_stop_before_advisor_without_cost(execution):
    """预算不足完整建议请求时，模型和评估均零调用。"""
    calls=[];advisor=_advisor(execution)
    result=service.run_cycle(**_kwargs(execution,authorization=ExecutionAuthorization(max_active_seconds=1)),
                             advisor_client=advisor,client_factory=_factory('improve',calls))
    assert result['stage']=='STOPPED' and result['total_request_attempts']==0
    assert not advisor.calls and not calls


def test_full_cycle_elapsed_time_is_carried_into_executor(execution):
    """执行器不能重新从零开始计算时间，准备与建议阶段耗时计入总时限。"""
    now=[0.0]
    class Delayed(FakeAdvisor):
        def complete_json_with_usage(self,**kwargs):
            now[0]=100.0
            return super().complete_json_with_usage(**kwargs)
    advisor=Delayed(_advisor(execution).response);calls=[];kwargs=_kwargs(execution)
    result=service.run_cycle(**kwargs,advisor_client=advisor,client_factory=_factory('improve',calls),clock=lambda:now[0])
    assert result['total_request_attempts']==19
    record=json.loads((kwargs['output']/'execution/experiment.json').read_text())
    assert record['active_before_execution_ms']==100000


def test_timeout_after_advisor_prevents_execution(execution):
    """建议返回时已超过全局时限，不允许继续启动120秒候选或控制组。"""
    now=[0.0]
    class Delayed(FakeAdvisor):
        def complete_json_with_usage(self,**kwargs):
            now[0]=1801.0
            return super().complete_json_with_usage(**kwargs)
    calls=[];advisor=Delayed(_advisor(execution).response);kwargs=_kwargs(execution)
    result=service.run_cycle(**kwargs,advisor_client=advisor,client_factory=_factory('improve',calls),clock=lambda:now[0])
    assert result['stage']=='STOPPED' and result['total_request_attempts']==1 and not calls
    assert result['stop_reason']=='ACTIVE_TIME_BUDGET_EXHAUSTED'


def test_history_uses_independent_comparison_not_cached_verdict(execution):
    """缓存摘要被改成改善也不能欺骗下一次模型，仍从Gold与逐轮数据得到NO_GAIN。"""
    _run(execution,'no_gain')
    path=execution['output']/'comparison.json';data=json.loads(path.read_text());data['decision']='IMPROVED';_save(path,data)
    context=service.check_cycle(**_kwargs(execution,experiments=[execution['output']]))
    assert context['packet'].experiment_history[0].decision=='NO_GAIN'


def test_bad_history_and_fake_live_mismatch_stop_before_advisor(execution):
    """损坏历史或把Fake经验当live输入时，预检即止步，不发出付费请求。"""
    _run(execution,'no_gain')
    kwargs=_kwargs(execution,experiments=[execution['output']]);kwargs['source']='live'
    with pytest.raises(ValueError,match='LIVE_FAKE_MISMATCH'):
        service.check_cycle(**kwargs)
    kwargs['source']='fake'
    path=next((execution['output']/'candidate/rounds').glob('*.json'));data=json.loads(path.read_text());data['input_sha256']='0'*64;_save(path,data)
    with pytest.raises(ValueError,match='HISTORY_EXPERIMENT_INVALID'):
        service.check_cycle(**kwargs)
    assert not kwargs['output'].exists()


def test_duplicate_history_is_not_counted_twice(execution):
    """重复列出同一个实验必须拒绝，不能人为放大已有反馈。"""
    _run(execution,'no_gain')
    with pytest.raises(ValueError,match='DUPLICATE_EXPERIMENT_HISTORY'):
        service.check_cycle(**_kwargs(execution,experiments=[execution['output'],execution['output']]))


def test_new_cycle_inherits_all_previous_history_without_resending_old_requests(execution):
    """新一轮继承旧NO_GAIN，模型再提相同方案只调用建议一次，不续跑前轮实验。"""
    calls=[];first=_kwargs(execution,'first-cycle')
    result=service.run_cycle(**first,advisor_client=_advisor(execution),client_factory=_factory('no_gain',calls))
    assert result['stage']=='NO_GAIN' and len(calls)==18
    before={str(p):p.read_bytes() for p in first['output'].rglob('*.json')}
    second=_kwargs(execution,'second-cycle');second['previous_cycle']=first['output']
    extra=[];advisor=_advisor(execution)
    result=service.run_cycle(**second,advisor_client=advisor,client_factory=_factory('improve',extra))
    assert result['stage']=='REJECTED' and result['history_count_before']==1
    assert not extra and len(advisor.calls)==1 and result['total_request_attempts']==1
    assert before=={str(p):p.read_bytes() for p in first['output'].rglob('*.json')}


def test_different_context_history_does_not_automatically_ban_current_candidate(execution):
    """历史条件不同则不能仅凭同一120秒数值拒绝当前单变量候选。"""
    _run(execution,'no_gain')
    context=service.check_cycle(**_kwargs(execution,experiments=[execution['output']]))
    packet=context['packet']
    changed=packet.experiment_history[0].model_copy(update={'condition_ref':'0'*64})
    packet=packet.model_copy(update={'experiment_history':[changed]})
    raw=_advisor(execution).response
    result=policy.validate_proposal(OptimizationProposal.model_validate(raw),packet)
    assert result.status=='VALIDATED'


def test_inspection_and_repeat_are_read_only_and_do_not_resume(execution):
    """完整闭环不可覆盖，检查不会自动补发，缺最终标记时只报告未完成。"""
    calls=[];kwargs=_kwargs(execution)
    service.run_cycle(**kwargs,advisor_client=_advisor(execution),client_factory=_factory('improve',calls))
    before={str(p):p.read_bytes() for p in kwargs['output'].rglob('*.json')}
    assert service.inspect_cycle(kwargs['output'])['stage']=='IMPROVED'
    assert before=={str(p):p.read_bytes() for p in kwargs['output'].rglob('*.json')}
    advisor=_advisor(execution)
    with pytest.raises(FileExistsError):
        service.run_cycle(**kwargs,advisor_client=advisor,client_factory=_factory('improve',calls))
    assert not advisor.calls and len(calls)==18
    (kwargs['output']/'manifest.json').unlink()
    assert service.inspect_cycle(kwargs['output'])['stage']=='UNFINALIZED'


def test_cli_cycle_defaults_to_no_calls(execution,monkeypatch,capsys):
    """CLI不加live时只做真实预检，不创建闭环目录或请求模型。"""
    import sys
    from app.scripts import evidence_harness
    from app.core import config
    monkeypatch.setattr(config,'get_settings',lru_cache()(lambda:execution['settings']))
    kwargs=_kwargs(execution)
    monkeypatch.setattr(sys,'argv',['harness','cycle','--dataset',str(kwargs['dataset_path']),
        '--baseline',str(kwargs['baseline_path']),'--output',str(kwargs['output'])])
    evidence_harness.main()
    result=json.loads(capsys.readouterr().out)
    assert result['stage']=='CHECK_PASSED' and result['model_calls']==0 and not kwargs['output'].exists()
