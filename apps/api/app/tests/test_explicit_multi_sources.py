"""多来源题名+文号锁定回归，不调用模型、真实库或本机业务原件。"""

from copy import deepcopy

import pytest

from app.db.models import Document, DocumentVersion, WorkingCopy
from app.modules.agent.planner import DeterministicPlanner, has_explicit_filename_content_request
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.state import ToolInvocationRecord
from app.modules.evidence_answer.service import EvidenceAnswerService
from app.modules.retrieval.explicit_sources import extract_explicit_sources
from app.tests.test_evidence_answer import _session, _seed, _settings, FakeEvidenceClient

QUESTION = ('只根据1.《计算机学院关于开展2025年品牌创建活动的通知》 文号：西理计算机党函〔2025〕6号。 '
            '2.《计算机学院关于公布2025年品牌创建立项清单的通知》 文号：西理计算机党发〔2025〕20号。'
            '分别说明培育时间和每项支持金额。请引用原文。')


def test_literal_identities_preserve_question_and_do_not_turn_into_global_search():
    """明确的两份身份只生成受控回答计划，保留完整问题供后端核验。"""
    identities = extract_explicit_sources(QUESTION)
    assert len(identities) == 2
    assert identities[0].issuer == '西理计算机党函' and identities[1].number == '20'
    assert has_explicit_filename_content_request(QUESTION)
    plan = DeterministicPlanner().plan(conversation_id='conversation-1',user_id='user-1',message_id='m',
                                      message=QUESTION,attachments=[])
    assert plan.intent=='EVIDENCE_ANSWER'
    assert [s.tool_name for s in plan.steps]==['evidence-answer']
    assert plan.steps[0].input['question']==QUESTION


@pytest.mark.parametrize('text',[
    QUESTION.replace('分别说明','分别删除'),
    QUESTION+'请删除这两份文件。',
    QUESTION.replace('西理计算机党函〔2025〕6号。',''),
    '请查找这些品牌创建相关文件。',
    '只根据品牌申报通知和立项通知，分别说明金额。',
])
def test_generic_search_or_write_requests_are_not_rewritten(text):
    """通用主题、缺文号和写操作不得被窄范围读取规则抢占。"""
    assert extract_explicit_sources(text)==[]


def test_title_and_full_formal_number_both_required():
    """题名、年份及流水号同时匹配，避免相似文件或其他年份混入。"""
    identity=extract_explicit_sources(QUESTION)[0]
    name='2025_西理计算机党函〔2025〕6号_计算机学院关于开展2025年品牌创建活动的通知.pdf'
    assert identity.matches([name])
    assert not identity.matches([name.replace('〔2025〕6号','〔2025〕16号')])
    assert not identity.matches([name.replace('〔2025〕6号','〔2024〕6号')])
    assert not identity.matches([name.replace('品牌创建活动','奖学金申报')])


def _seed_two(db):
    """只在隔离库建立两份不同身份，不访问用户原件。"""
    first,version=_seed(db)
    refs=extract_explicit_sources(QUESTION)
    first.filename=version.filename=f'{refs[0].issuer}〔2025〕6号_{refs[0].title}.pdf'
    d=Document(id='document-2',user_id='user-1',workspace_id=first.workspace_id,
               original_filename=f'{refs[1].title}（{refs[1].issuer}〔2025〕20号）.pdf',
               content_type='application/pdf',size_bytes=128,sha256='f'*64,status='READY',ingest_status='READY')
    v=DocumentVersion(id='version-2',document_id=d.id,version_number=1,storage_tier='WORKING_COPY',
                     storage_path='test/source-2.pdf',filename=d.original_filename,sha256=d.sha256)
    c=WorkingCopy(id='working-copy-2',document_id=d.id,current_version_id=v.id,
                  working_copy_root_id=first.working_copy_root_id,workspace_id=first.workspace_id,
                  managed_file_id=first.managed_file_id,relative_path='source-2.pdf',relative_path_hash='f'*64,
                  filename=d.original_filename,extension='.pdf',size_bytes=128,content_sha256=d.sha256,
                  imported_source_sha256=d.sha256,status='ACTIVE')
    db.add_all([d,v,c]);db.flush()
    return first,c


def service(db):
    """为每次测试构造绑定当前会话的服务，模型始终使用确定性替身。"""
    return EvidenceAnswerService(db=db,user_id='user-1',conversation_id='conversation-1',
                                settings=_settings(),client=FakeEvidenceClient())


def test_resolver_reads_only_exact_two_current_shared_files():
    """验证实际ORM查询能把两个题名文号映射到两个活动文档。"""
    with _session() as db:
        _seed_two(db)
        s=service(db)
        rows,status=s._resolve_explicit_source_working_copies(extract_explicit_sources(QUESTION))
        assert status=='MATCHED'
        assert {copy.document_id for copy,_ in rows}=={'document-1','document-2'}


@pytest.mark.parametrize('status',['TRASHED','IMPORTING'])
def test_partial_source_match_does_not_answer_from_remaining_file(status):
    """第二份在回收站或尚未发布时，不用第一份材料冒充全部来源。"""
    with _session() as db:
        _,second=_seed_two(db);second.status=status;db.flush()
        s=service(db)
        result=s.answer(question=QUESTION)
        assert result['index_status']=='EXPLICIT_SOURCE_NOT_RESOLVED'
        assert s.client.calls==0


def test_private_workspace_is_not_used_to_fill_missing_source():
    """缺失来源不能通过扩大到其他私有工作区补足。"""
    with _session() as db:
        _,second=_seed_two(db);second.workspace_id='not-shared';db.flush()
        rows,status=service(db)._resolve_explicit_source_working_copies(extract_explicit_sources(QUESTION))
        assert status=='MISSING' and not rows


def test_duplicate_formal_identity_requires_user_selection():
    """出现重复身份须创建持久化选择记录，不能替用户随意选中。"""
    with _session() as db:
        first,second=_seed_two(db)
        payload={c.name:getattr(first,c.name) for c in WorkingCopy.__table__.columns}
        payload.update(id='working-copy-duplicate',relative_path='duplicate.pdf',relative_path_hash='z'*64)
        db.add(WorkingCopy(**payload));db.flush()
        s=service(db)
        result=s.answer(question=QUESTION)
        assert result['kind']=='file_selection'
        assert len(result['choices'])==3 and s.client.calls==0


def test_answer_uses_resolved_scope_and_never_recalls_global_candidates(monkeypatch):
    """固定范围传到原证据加载步骤，禁止退回全库召回。"""
    with _session() as db:
        _seed_two(db);s=service(db);seen=[]
        original=s._load_evidence
        def load(**kwargs):
            seen.append({copy.document_id for copy,_ in kwargs['working_copy_rows']})
            # 此测试只测两个真实身份的范围传递；只复用已有fixture的正文证据。
            return original(**{**kwargs,'working_copy_rows':[r for r in kwargs['working_copy_rows'] if r[0].document_id=='document-1']})
        monkeypatch.setattr(s,'_load_evidence',load)
        def forbidden(*args,**kwargs):pytest.fail('Explicit sources must not recall globally')
        monkeypatch.setattr(s,'_recall_active_working_copies',forbidden)
        result=s.answer(question=QUESTION)
        assert result['ok'] and seen==[{'document-1','document-2'}]


@pytest.mark.parametrize('adaptive_mode',['legacy','enabled'])
def test_explicit_identities_reach_controlled_tool_without_waiting_for_intent_model(adaptive_mode,monkeypatch):
    """两种规划模式均通过原白名单分发读取，避免模型超时丢失明确身份。"""
    monkeypatch.setattr('app.modules.agent.service._adaptive_planner_mode_for_user',lambda **kwargs:adaptive_mode)
    class Intent:
        enabled=True
        def understand_user_request(self,**kwargs):pytest.fail('Identity preflight must not call intent LLM')
    class Registry:
        def __init__(self):self.calls=[]
        def invoke(self,tool_name,input_json):
            self.calls.append((tool_name,deepcopy(input_json)))
            return ToolInvocationRecord(tool_name=tool_name,input_json=input_json,
                output_json={'ok':True,'kind':'evidence_answer','status':'NO_EVIDENCE','answer':'测试','references':[]},
                status='COMPLETED')
    registry=Registry()
    result=AgentRuntimeService(registry_factory=lambda _db,_user_id:registry,
        llm_intent_service=Intent()).run_message(
            conversation_id='conversation-1',user_id='user-1',message_id='m',message=QUESTION,attachments=[])
    assert result.intent=='EVIDENCE_ANSWER'
    assert [x[0] for x in registry.calls]==['evidence-answer']
    assert registry.calls[0][1]['question']==QUESTION
