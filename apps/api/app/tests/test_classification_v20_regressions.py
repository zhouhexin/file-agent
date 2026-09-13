"""真实其他分类样本的脱敏回归：正文结构、部门兜底与用途落位证据链。"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import DocumentVersion, WorkingCopy
from app.modules.classification.auto_placement_policy import AutoPlacementPolicy
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.matcher import DocumentFeatures, apply_unclassified_fallback, detect_structured_document_purpose
from app.modules.classification.purpose_placement import validate_placement_purpose
from app.modules.classification.purpose_policy import evaluate_purpose_package
from app.modules.classification.purpose_repository import PurposePackageRepository
from app.modules.classification.rule_policy import evaluate_rule_policy
from app.tests.test_classification_purpose_policy import _package
from app.tests.test_purpose_package_inference import _add_ready_member
from app.db.models import ManagedRoot, User, Workspace


@pytest.mark.parametrize(('body', 'expected'), [
    ('2022申报CJ计划人员情况汇总表\n学院：计算机科学与工程学院\n姓名\t申报项目名称\t入选人才计划\n长江学者', 'college.hr.talent-work'),
    ('青年教师奖申报汇总表\n姓名\t项目类别\t入校时间\n计算机科学与工程学院\n科研及教学获奖情况', 'college.hr.talent-work'),
    ('西安理工大学人事代理人员合同期满考核评分表\n评价目标\t评价标准\t得分\n工作态度\t遵守规章制度\t5', 'school.hr.appointment-assessment'),
    ('工号\t姓名\t聘任时间\t任职状态\t聘期\t单位名称\t承担本科教学任务\n计算机科学与工程学院', 'college.hr'),
    ('关于做好秦创原校招共用人才目录编制工作的通知\n相关学院：现建立引才用才机制，签订聘任合同。\n人事处', 'school.hr.talent-work'),
    ('提前进校工作申请\n本人系计算机学院拟调入教师，人事处已上报调动材料，申请提前进校工作。', 'college.hr'),
])
def test_real_hr_shapes_select_business_with_original_sheet_evidence(monkeypatch, body, expected, filename='普通材料.xlsx'):
    """经完整分类服务及落位门控验证，匿名文件名不影响真实题名和字段证据。"""
    service = DocumentClassificationService(graph_mode='off', settings=Settings(database_url='sqlite://'))
    monkeypatch.setattr(service, '_load_pages', lambda **_: [
        SimpleNamespace(page_number=1, sheet_name='Sheet1', text_content=body)
    ])
    result = service.classify(document_id='', extraction_run_id='', filename=filename)
    primary = result['categories'][0]
    assert primary['category_id'] == expected
    assert primary['evidence_items']
    assert all(e['quote'] in body for e in primary['evidence_items'])
    placed = AutoPlacementPolicy(service.settings).evaluate(categories=result['categories'], extraction_status='COMPLETED')
    assert placed.primary_category['category_id'] == expected


def test_long_personnel_table_keeps_structure_before_scope_mirror(monkeypatch):
    """多个同范围背景候选不得把尚未校正组织镜像的强结构挤出默认五候选。"""
    body = ('工号\t姓名\t聘任时间\t任职状态\t聘期\t单位名称\t学历\t专业技术职称\t承担本科教学任务\n'
            '中国科学院\n信息化研究院\n研究生\n科技发展公司\n信息系统')
    test_real_hr_shapes_select_business_with_original_sheet_evidence(
        monkeypatch, body, 'college.hr', filename='外聘和兼职教师基本信息采集表（学院填报）.xlsx'
    )


def test_department_fallback_recomputed_without_parent_in_top_n():
    """原文新增人事机关后，根级旧兜底不得挡住独立部门识别。"""
    result = apply_unclassified_fallback(
        document_features=DocumentFeatures(filename='普通材料.txt', full_text='各单位请按时将材料报送至人事处人事管理科。'),
        taxonomy=load_default_taxonomy(),
        matches=[{'category_id':'school.other', 'category_path':['学校','其他'], 'source':'rule_fallback'}],
    )
    assert result[-1]['category_id'] == 'school.hr.other'
    assert len([x for x in result if x['source']=='rule_fallback']) == 1


def test_department_conflict_does_not_reuse_stale_fallback():
    """两个受控部门并存且无法唯一确定时不保留旧人事归属。"""
    result = apply_unclassified_fallback(
        document_features=DocumentFeatures(full_text='人事处与财务处共同办理临时事项。'),
        taxonomy=load_default_taxonomy(),
        matches=[{'category_id':'school.hr.other','category_path':['学校','人事师资','其他'],'source':'rule_fallback'}],
    )
    assert result[-1]['category_id'] == 'system.other'


def test_filename_alone_and_generic_personnel_fields_do_not_create_purpose():
    """伪题名、普通花名册、学生表单均不能触发新增人事结构用途。"""
    for body in ['姓名\t班级\t学号', '姓名\t职称\t工作单位', '评价目标\t评价标准\t得分']:
        assert detect_structured_document_purpose(filename='人事代理人员合同期满考核评分表.xls', full_text=body) is None
    matches = evaluate_rule_policy(filename='校招共用引才用才通知.docx',title='',body_text='请编制校园招聘会毕业生就业统计。',organization_root='学校')
    assert not any(x.rule_id=='hr.shared-talent-program' for x in matches)


@pytest.mark.parametrize('failure', [None, 'hash', 'version', 'workspace', 'taxonomy', 'digest', 'category'])
def test_persisted_package_source_to_working_version_placement(failure):
    """真实仓库包经源/工作版本血缘核验后落位；任一关键事实失配均关闭继承。"""
    engine=create_engine('sqlite+pysqlite:///:memory:'); Base.metadata.create_all(engine)
    db=sessionmaker(bind=engine,autoflush=False)()
    try:
        db.add_all([User(id='user',username='purpose-user'), Workspace(id='workspace-1',name='test'),ManagedRoot(id='root',root_key='managed',display_name='test',container_path='/managed')]);db.flush()
        mf, revision, source = _add_ready_member(db,user_id='user',workspace_id='workspace-1',root_id='root',suffix='1',relative_path='pack/attachment.pdf',text='')
        db.flush()
        package=_package().model_copy(update={'members':(_package().members[0].model_copy(update={'managed_file_id':mf.id,'document_version_id':source.id,'sha256':source.sha256}),)})
        PurposePackageRepository(db).create(package)
        # 工作副本内容相同但版本 ID 不同，不能用工作版本直接匹配源包成员。
        version=DocumentVersion(id='working-version',document_id=source.document_id,version_number=2,storage_path='working/file.pdf',filename='file.pdf',size_bytes=0,sha256=source.sha256)
        db.add(version);db.flush()
        copy=WorkingCopy(id='copy',workspace_id='workspace-1',managed_file_id=mf.id,document_id=source.document_id,current_version_id=version.id,content_sha256=version.sha256)
        candidate=evaluate_purpose_package(package=package,taxonomy=load_default_taxonomy(),document_version_id=source.id,content_sha256=source.sha256,managed_file_id=mf.id).candidate
        if failure=='hash': copy.content_sha256='b'*64
        if failure=='version': revision.analysis_document_version_id='not-frozen';db.flush()
        if failure=='workspace': copy.workspace_id='outside'
        if failure=='taxonomy': candidate['taxonomy_version']='old'
        if failure=='digest': candidate['evidence_items'][0]['manifest_digest']='forged'
        if failure=='category': candidate['category_id']='school.finance'
        verified=validate_placement_purpose(db,working_copy=copy,candidate=candidate)
        result=AutoPlacementPolicy(Settings(database_url='sqlite://')).evaluate(categories=[candidate],extraction_status='COMPLETED',verified_purpose=verified)
        assert result.primary_category['category_id'] == ('system.other' if failure else 'college.hr.faculty-recruitment')
        if not failure:
            assert result.feature_snapshot['verified_purpose_package'] is True
            # 候选字典即使来自分类服务，也不能替代落位前的数据库校验。
            unverified=AutoPlacementPolicy(Settings(database_url='sqlite://')).evaluate(categories=[candidate],extraction_status='COMPLETED')
            assert unverified.primary_category['category_id']=='system.other'
    finally:
        db.rollback();db.close();engine.dispose()
