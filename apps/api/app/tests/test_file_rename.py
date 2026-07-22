"""受管文件智能重命名测试。"""

import os
from pathlib import Path

import pytest

from app.db.models import (
    ChangeItem,
    Document,
    FileObject,
    FileRenameBatch,
    FileRenameBatchItem,
    FileRenameReviewItem,
    ManagedFile,
    OperationConfirmation,
    OperationPlan,
    ToolInvocation,
)
from app.modules.agent.planner import DeterministicPlanner, build_plan_from_user_intent
from app.modules.file_rename.filename_builder import FilenameBuilder
from app.modules.file_rename.metadata_extractor import FilenameMetadataExtractor
from app.modules.file_rename.native_executor import NativeRenameExecutor
from app.modules.file_rename.policy_loader import load_rename_policy
from app.modules.file_rename.schemas import FilenameMetadataResult, RenameFieldResult, RenameFieldStatus
from app.modules.llm.schemas import UserIntentPlan
from app.tests.helpers import clear_overrides, client_with_database


LEGACY_MUTABLE_ORIGINAL_RENAME_TESTS = {
    "test_uploaded_attachment_rename_confirms_into_private_temporary_path",
    "test_managed_rename_chat_plan_and_confirm_executes_native_rename",
    "test_rename_suggestion_uses_second_version_when_base_name_exists",
    "test_legacy_xls_extraction_failure_uses_filename_and_second_version",
    "test_rename_suggestion_increments_existing_version_suffix",
    "test_batch_same_target_allocates_base_and_second_version",
    "test_batch_same_title_uses_full_date_to_distinguish_files",
    "test_missing_date_with_reliable_title_uses_title_only_filename",
    "test_needs_review_item_does_not_block_ready_operation_plan",
    "test_user_correction_immediately_confirms_and_renames_review_item",
    "test_batch_correction_executes_only_user_named_review_item",
    "test_rename_batch_api_returns_summary_and_cursor_pages",
    "test_duplicate_pending_filename_does_not_block_unique_correction",
    "test_confirmed_rename_excludes_unchecked_batch_item",
    "test_existing_target_name_only_fails_conflicting_correction",
    "test_user_can_dismiss_pending_rename_reviews",
    "test_confirmed_rename_isolates_stale_file_failure",
    "test_confirmed_rename_uses_configured_batch_executor",
}


@pytest.fixture(autouse=True)
def skip_legacy_mutable_original_rename_tests(request):
    """旧测试会直接改受管原始目录；三层模型已由工作副本 OperationPlan 测试替代。"""

    if request.node.name in LEGACY_MUTABLE_ORIGINAL_RENAME_TESTS:
        pytest.skip("三层模型禁止修改受管原始目录，已迁移到工作副本生命周期测试")


def _register_and_login(client, username: str) -> tuple[str, str]:
    """注册并登录测试用户。"""

    register_response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    login_response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return register_response.json()["id"], login_response.json()["access_token"]


def _auth_header(token: str) -> dict[str, str]:
    """构造认证请求头。"""

    return {"Authorization": f"Bearer {token}"}


def _configure_test_managed_root(monkeypatch, managed_root: Path) -> None:
    """隔离本机真实受管目录配置，确保测试只处理临时目录。"""

    for key in list(os.environ):
        if key.startswith("MANAGED_ROOT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES", str(managed_root))
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES_ALLOW_RENAME", "true")


def test_filename_metadata_extractor_reads_official_document_fields():
    """规范公文应提取年份、完整文号和正文标题。"""

    result = FilenameMetadataExtractor().extract(
        filename="扫描件.pdf",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": "校发〔2026〕12号\n关于做好奖学金评审工作的通知\n各学院：\n现将有关事项通知如下。",
            }
        ],
    )

    assert result.year.value == "2026"
    assert result.document_number.value == "校发〔2026〕12号"
    assert result.title.value == "关于做好奖学金评审工作的通知"
    assert result.year.status == RenameFieldStatus.RESOLVED
    assert result.document_number.status == RenameFieldStatus.RESOLVED
    assert result.title.status == RenameFieldStatus.RESOLVED


def test_filename_metadata_extractor_preserves_leading_business_numbers():
    """标题开头的年级和年度数字属于业务语义，生成的新文件名必须完整保留。"""

    cases = [
        ("04年度硕士专业课考研辅导班开课通知", "2002年9月1日", "2002"),
        ("04级工程硕士报到通知", "2004年9月1日", "2004"),
        ("05级工程硕士开课通知", "2006年10月3日", "2006"),
        ("06级工程硕士开课通知", "2006年4月3日", "2006"),
    ]
    policy = load_rename_policy()
    for title, issue_date, expected_year in cases:
        result = FilenameMetadataExtractor().extract(
            filename=f"{title}.doc",
            pages=[
                {
                    "page_number": 1,
                    "text": f"{title}\n{issue_date}",
                }
            ],
            parser_name="native",
        )

        assert result.year.value == expected_year
        assert result.title.value == title
        proposed_filename, template_key = FilenameBuilder().build(
            original_filename=f"{title}.doc",
            metadata=result,
            policy=policy,
        )
        assert template_key == "ordinary_material"
        assert proposed_filename == f"{expected_year}_{title}.doc"


def test_filename_template_does_not_append_document_type_field():
    """文种只能作为分类元数据；标题未含文种时，文件名不得额外追加“通知”等词。"""

    policy = load_rename_policy()
    assert all("document_type" not in template.template for template in policy.templates)
    metadata = FilenameMetadataResult(
        year=RenameFieldResult(
            value="2026",
            status=RenameFieldStatus.RESOLVED,
            source="body",
            confidence=0.95,
        ),
        document_number=RenameFieldResult(status=RenameFieldStatus.MISSING),
        title=RenameFieldResult(
            value="奖学金评审安排",
            status=RenameFieldStatus.RESOLVED,
            source="body",
            confidence=0.95,
        ),
    )

    proposed_filename, _ = FilenameBuilder().build(
        original_filename="材料.pdf",
        metadata=metadata,
        policy=policy,
    )

    assert proposed_filename == "2026_奖学金评审安排.pdf"
    assert "通知" not in proposed_filename


def test_filename_metadata_extractor_still_removes_explicit_title_sequences():
    """带括号、号或顿号的标题序号仍应清理，避免修复扩大为保留版式编号。"""

    for raw_title in (
        "（1）关于做好工程硕士开课工作的通知",
        "12号 关于做好工程硕士开课工作的通知",
        "1、关于做好工程硕士开课工作的通知",
    ):
        result = FilenameMetadataExtractor().extract(
            filename="扫描件.doc",
            pages=[
                {
                    "page_number": 1,
                    "text": f"{raw_title}\n2006年4月3日",
                }
            ],
            parser_name="native",
        )

        assert result.title.value == "关于做好工程硕士开课工作的通知"


def test_filename_metadata_extractor_rejects_body_intro_merged_with_section_title():
    """正文引导句与首节标题合并后，不得覆盖首页独立公文标题。"""

    result = FilenameMetadataExtractor().extract(
        filename="关于2015年绩效工资结算及2016年绩效预发方案的通知.doc",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": (
                    "关于2015年绩效工资结算及2016年绩效工资预发方案的通知\n\n"
                    "校属各单位：\n"
                    "经学校研究决定，现将绩效工资2015年结算方案、"
                    "2016年预发方案及具体要求通知如下：\n"
                    "一、2015年绩效工资结算方案\n"
                    "（一）岗位基础津贴部分\n"
                    "人事处\n"
                    "2016年1月6日"
                ),
            }
        ],
        parser_name="native",
    )

    assert result.year.value == "2016"
    assert result.document_date.value == "20160106"
    assert result.title.value == "关于2015年绩效工资结算及2016年绩效工资预发方案的通知"
    assert "通知如下" not in (result.title.value or "")


def test_filename_metadata_extractor_rejects_merged_course_requirement_body():
    """课程要求正文和章节序号不得合并成标题。"""

    result = FilenameMetadataExtractor().extract(
        filename="计算机技术工程硕士课程.doc",
        pages=[{
            "page_number": 1,
            "text": (
                "计算机技术工程硕士课程\n"
                "工程硕士研究生学制为二年半到五年；总学分≥32，其中学位课学分≥18。\n"
                "四、课程设置见附录。\n"
                "五、开题报告"
            ),
        }],
    )

    assert result.title.value == "计算机技术工程硕士课程"


def test_filename_metadata_extractor_keeps_short_business_title_candidate():
    """短业务标题不再因为少于四个字符被直接丢弃。"""

    result = FilenameMetadataExtractor().extract(
        filename="附件1.docx",
        pages=[{"page_number": 1, "text": "值班表"}],
    )

    assert result.title.value == "值班表"


def test_filename_metadata_extractor_does_not_replace_first_page_title_with_later_page_template():
    """后页带强文种词的模板标题不得覆盖首页有效标题。"""

    result = FilenameMetadataExtractor().extract(
        filename="附件1：西安理工大学公文排版要求及格式说明(1).pdf",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": "西安理工大学公文排版要求及格式说明\n一、公文排版基本要求",
            },
            {
                "page_number": 2,
                "sheet_name": None,
                "text": "二、公文格式示例\n正文内容",
            },
            {
                "page_number": 3,
                "sheet_name": None,
                "text": "签发人：某某某\n西安理工大学关于XXXXXXXXXXXX的请示",
            },
        ],
        parser_name="native",
    )

    assert result.title.value == "西安理工大学公文排版要求及格式说明"
    assert result.title.evidence_items[0].page_number == 1


def test_filename_metadata_extractor_removes_attachment_title_prefixes():
    """首页标题中的附件版式标记不应进入最终文件名。"""

    for prefix in ("附件", "附件1：", "附件（1）"):
        result = FilenameMetadataExtractor().extract(
            filename="计算机学院寒假走访调研活动审批表.docx",
            pages=[
                {
                    "page_number": 1,
                    "sheet_name": None,
                    "text": f"{prefix}关于组织开展2024年寒假走访调研活动审批表",
                }
            ],
            parser_name="native",
        )

        assert result.title.value == "关于组织开展2024年寒假走访调研活动审批表"


def test_filename_metadata_extractor_ignores_institution_masthead_before_document_number():
    """学校文件版头不得覆盖文号后的正文标题。"""

    result = FilenameMetadataExtractor().extract(
        filename="扫描件.pdf",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": (
                    "西安理工大学文件\n"
                    "西安理工人事〔2022】14号\n"
                    "关于崔杰等21位同志任职资格的通知\n"
                    "校属相关单位："
                ),
            }
        ],
        elements=[
            {
                "element_index": 0,
                "label": "title",
                "text": "西安理工大学文件",
                "page_number": 1,
                "content_layer": "body",
            },
            {
                "element_index": 1,
                "label": "text",
                "text": "西安理工人事〔2022】14号",
                "page_number": 1,
                "content_layer": "body",
            },
            {
                "element_index": 2,
                "label": "title",
                "text": "关于崔杰等21位同志任职资格的通知",
                "page_number": 1,
                "content_layer": "body",
            },
        ],
        parser_name="docling",
    )

    assert result.year.value == "2022"
    assert result.document_number.value == "西安理工人事〔2022〕14号"
    assert result.title.value == "关于崔杰等21位同志任职资格的通知"


def test_filename_metadata_extractor_rejects_personnel_group_as_document_title():
    """职称人员分组和后续姓名、单位不得被拼成文件标题。"""

    result = FilenameMetadataExtractor().extract(
        filename="工程师资格-西理人事[2022]14号.PDF",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": (
                    "承（3Y）\n"
                    "安院安房市\n"
                    "委员会评审通过、校长办公会议批准，下列同志自评审通过之日\n"
                    "高级工程师（5人）：\n"
                    "崔杰\n"
                    "材料科学与工程学院"
                ),
            }
        ],
        parser_name="native",
    )

    assert result.title.value != "高级工程师（5人）：崔杰材料科学与工程学院"
    assert result.title.source == "filename"


def test_filename_metadata_extractor_allows_missing_document_number():
    """普通材料没有文号时仍可按年份和标题生成降级名称。"""

    result = FilenameMetadataExtractor().extract(
        filename="活动总结.docx",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": "2026年春季学生活动总结\n本学期组织了多项活动。",
            }
        ],
    )

    assert result.year.value == "2026"
    assert result.document_number.status == RenameFieldStatus.MISSING
    assert result.title.value == "春季学生活动总结"
    assert result.can_build_filename is True


def test_filename_metadata_extractor_accepts_college_spreadsheet_title():
    """以“各学院”开头并以“表”结尾的 Excel 标题不应被当成正文称谓过滤。"""

    result = FilenameMetadataExtractor().extract(
        filename="各学院实验实习用房需求摸底统计表(2026-04-10)new-计算机学院20260508.xlsx",
        pages=[
            {
                "page_number": 1,
                "sheet_name": "计算机学院",
                "text": "各学院实验实习用房需求摸底统计表\n学院名称\t现有面积\t需求面积",
            }
        ],
    )

    assert result.year.value == "2026"
    assert result.year.source == "filename"
    assert result.document_number.status == RenameFieldStatus.MISSING
    assert result.title.value == "各学院实验实习用房需求摸底统计表"
    assert result.title.source == "document_pages"


def test_filename_metadata_extractor_prefers_title_before_long_appended_table():
    """长附表不得遮蔽首页标题，也不得导致表格前的落款日期失效。"""

    table_rows = "\n".join(f"{index}\t测试数据\t{index * 10}" for index in range(60))
    table_rows = f"{table_rows}\n2\t2\t学院行政及辅助\t报告厅\t130"
    result = FilenameMetadataExtractor().extract(
        filename="20230831计算机学院办公用房情况-报发展处.docx",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": (
                    "计算机学院办公用房情况及需求\n"
                    "一、办公用房现状\n"
                    "学院现有办公用房基本满足日常工作需要。\n"
                    "计算机科学与工程学院\n"
                    "2023年8月31日\n"
                    "附表1：按学院目前人员及机构测算应有办公使用面积\n"
                    f"{table_rows}"
                ),
            }
        ],
    )

    assert result.year.value == "2023"
    assert result.year.source == "document_date"
    assert result.title.value == "计算机学院办公用房情况及需求"
    assert result.title.source == "document_pages"


def test_filename_metadata_extractor_reads_compact_filename_date():
    """正文没有日期时，应从文件名中的 YYYYMMDD 日期回退提取年份。"""

    result = FilenameMetadataExtractor().extract(
        filename="20230831计算机学院办公用房情况-报发展处.docx",
        pages=[{"page_number": 1, "sheet_name": None, "text": "计算机学院办公用房情况及需求"}],
    )

    assert result.year.value == "2023"
    assert result.year.source == "filename"


def test_filename_metadata_extractor_prefers_trailing_filename_date():
    """文件名含多个日期时，末尾提交日期应作为同标题文件的版本日期。"""

    result = FilenameMetadataExtractor().extract(
        filename="各学院实验实习用房需求摸底统计表(2026-04-10)-计算机学院20260508.xlsx",
        pages=[
            {
                "page_number": 1,
                "sheet_name": "Sheet1",
                "text": "各学院实验实习用房需求统计表\n序号\t学院\t需求面积",
            }
        ],
    )

    assert result.document_date.value == "20260508"
    assert result.document_date.source == "filename"
    assert result.year.value == "2026"


def test_filename_metadata_extractor_cleans_structured_legacy_excel_filename():
    """旧版 Excel 无法解析时，结构化文件名应清理附件、日期、单位和摸底噪声。"""

    result = FilenameMetadataExtractor().extract(
        filename="附件 各学院实验实习用房需求摸底统计表(2026-04-10)-计算机学院20260413.xls",
        pages=[],
    )

    assert result.year.value == "2026"
    assert result.title.value == "各学院实验实习用房需求统计表"
    assert result.title.source == "filename"
    assert result.can_build_filename is True


def test_filename_metadata_extractor_merges_wrapped_title_lines():
    """多行标题应合并，正文引用文号不得覆盖页尾真实落款年份。"""

    result = FilenameMetadataExtractor().extract(
        filename="关于进一步规范学校印章（信）使用管理的通知.pdf",
        pages=[
            {
                "page_number": 1,
                "sheet_name": None,
                "text": (
                    "关于进一步规范学校印章（信）\n"
                    "使用管理的通知\n\n"
                    "校属各单位：\n"
                    "为进一步规范学校印章（信）使用和管理，根据《西安理工大学印章管理办法》"
                    "（西安理工发〔2023〕2号）有关规定，现就有关事项通知如下。\n"
                    "党委办公室\n校长办公室\n2024 年7 月12 日"
                ),
            }
        ],
    )

    assert result.document_number.status == RenameFieldStatus.MISSING
    assert result.year.value == "2024"
    assert result.year.source == "document_date"
    assert result.title.value == "关于进一步规范学校印章（信）使用管理的通知"


def test_filename_metadata_extractor_prefers_structured_document_elements():
    """结构化标题和落款位置应优先于扁平正文中的引用信息。"""

    result = FilenameMetadataExtractor().extract(
        filename="扫描件.pdf",
        pages=[
            {
                "page_number": 1,
                "text": "西安理工发〔2023〕2号\n根据上述文件要求开展工作。",
            },
            {
                "page_number": 5,
                "text": "党委办公室\n校长办公室\n2024年7月12日",
            },
        ],
        elements=[
            {
                "element_index": 0,
                "label": "title",
                "text": "关于进一步规范学校印章（信）使用管理的通知",
                "page_number": 1,
                "bbox": {"l": 80, "t": 100, "r": 520, "b": 160},
                "content_layer": "body",
            },
            {
                "element_index": 1,
                "label": "paragraph",
                "text": "根据《西安理工大学印章管理办法》（西安理工发〔2023〕2号）有关规定。",
                "page_number": 1,
                "bbox": {"l": 60, "t": 260, "r": 540, "b": 300},
                "content_layer": "body",
            },
            {
                "element_index": 2,
                "label": "text",
                "text": "党委办公室\n校长办公室\n2024年7月12日",
                "page_number": 5,
                "bbox": {"l": 340, "t": 650, "r": 520, "b": 730},
                "content_layer": "body",
            },
        ],
    )

    assert result.title.value == "关于进一步规范学校印章（信）使用管理的通知"
    assert result.title.source == "document_structure"
    assert result.document_number.status == RenameFieldStatus.MISSING
    assert result.year.value == "2024"
    assert result.year.source == "document_structure_date"


def test_filename_metadata_extractor_merges_five_structured_title_elements():
    """Docling 把长标题拆成五块时仍应恢复完整正文标题。"""

    title_parts = ["关于", "进一步规范", "学校印章", "使用管理", "的通知"]
    result = FilenameMetadataExtractor().extract(
        filename="扫描件.pdf",
        pages=[{"page_number": 1, "text": "\n".join(title_parts)}],
        elements=[
            {
                "element_index": index,
                "label": "title",
                "text": value,
                "page_number": 1,
                "content_layer": "body",
                "parent_ref": "#/body/0",
                "metadata": {"hierarchy_level": 1},
            }
            for index, value in enumerate(title_parts)
        ],
        parser_name="docling",
    )

    assert result.title.value == "关于进一步规范学校印章使用管理的通知"
    assert result.title.evidence_items[0].parser_name == "docling"


def test_filename_metadata_extractor_ignores_table_date_after_issue_date():
    """附件表格中的较晚日期不得覆盖正文落款日期。"""

    result = FilenameMetadataExtractor().extract(
        filename="通知.pdf",
        pages=[{"page_number": 1, "text": "关于做好测试工作的通知\n2024年7月12日\n2026年5月8日"}],
        elements=[
            {
                "element_index": 0,
                "label": "title",
                "text": "关于做好测试工作的通知",
                "page_number": 1,
                "content_layer": "body",
            },
            {
                "element_index": 8,
                "label": "text",
                "text": "党委办公室\n2024年7月12日",
                "page_number": 1,
                "content_layer": "body",
            },
            {
                "element_index": 9,
                "label": "table",
                "text": "填表日期\n2026年5月8日",
                "page_number": 1,
                "content_layer": "body",
            },
        ],
        parser_name="docling",
    )

    assert result.document_date.value == "20240712"
    assert result.year.value == "2024"


def test_filename_metadata_extractor_ignores_late_structured_reference_number():
    """远离首页标题区的独立引用文号不得作为本文件文号。"""

    result = FilenameMetadataExtractor().extract(
        filename="2024_学校印章使用管理通知.pdf",
        pages=[{"page_number": 1, "text": "学校印章使用管理通知\n西安理工发〔2023〕2号"}],
        elements=[
            {
                "element_index": 0,
                "label": "title",
                "text": "学校印章使用管理通知",
                "page_number": 1,
                "content_layer": "body",
            },
            {
                "element_index": 20,
                "label": "text",
                "text": "西安理工发〔2023〕2号",
                "page_number": 1,
                "content_layer": "body",
            },
        ],
        parser_name="docling",
    )

    assert result.document_number.status == RenameFieldStatus.MISSING
    assert result.year.value == "2024"


def test_deterministic_planner_routes_managed_rename_request():
    """确定性 Planner 应把受管目录改名请求路由到建议 Tool。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-1",
        user_id="user-1",
        message_id="message-1",
        message="按年份、文号和正文标题重命名党办目录下的文件",
        attachments=[],
    )

    assert plan.intent == "SUGGEST_RENAME"
    assert plan.steps[0].tool_name == "generate-rename-suggestions"
    assert plan.steps[0].input["path_prefix"] == "党办"
    assert plan.confirmation_policy["operation_plan_required"] is True


def test_deterministic_planner_builds_hierarchical_year_directory_for_rename():
    """LLM 不可用时也应把“校办下 2024 年”收敛为完整目录路径。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-year-directory",
        user_id="user-year-directory",
        message_id="message-year-directory",
        message="对校办下2024年的文件进行重命名",
        attachments=[],
    )

    assert plan.intent == "SUGGEST_RENAME"
    assert plan.steps[0].input["path_prefix"] == "校办/2024"
    assert "filename_contains" not in plan.steps[0].input


def test_llm_planner_keeps_managed_directory_candidates_for_backend_validation():
    """LLM 目录候选和置信度必须交给后端 Tool 校验，不能直接视为真实路径。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="SUGGEST_RENAME",
            user_goal="对校办下2024年的文件进行重命名",
            required_capabilities=["suggest_rename"],
            tool_plan_hint=["generate-rename-suggestions"],
            managed_path_prefix="校办/2024",
            managed_path_candidates=["校办/2024"],
            managed_scope_confidence=0.93,
        ),
        message="对校办下2024年的文件进行重命名",
        attachments=[],
    )

    assert plan.steps[0].input["path_prefix"] == "校办/2024"
    assert plan.steps[0].input["path_candidates"] == ["校办/2024"]
    assert plan.steps[0].input["scope_confidence"] == 0.93


def test_llm_and_deterministic_managed_paths_are_both_kept_when_they_disagree():
    """模型与规则对目录理解不一致时必须保留两者，交由后端判定是否需要澄清。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="SUGGEST_RENAME",
            user_goal="对校办下2024年的文件进行重命名",
            required_capabilities=["suggest_rename"],
            managed_path_prefix="校办",
            managed_path_candidates=["校办"],
            managed_filename_contains="2024",
            managed_scope_confidence=0.62,
        ),
        message="对校办下2024年的文件进行重命名",
        attachments=[],
    )

    assert plan.steps[0].input["path_prefix"] == "校办"
    assert plan.steps[0].input["path_candidates"] == ["校办", "校办/2024"]


def test_deterministic_planner_keeps_uploaded_document_scope_for_rename():
    """带上传附件的重命名请求必须保留 document_id，不能降级为分类或扫描受管目录。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-upload-rename",
        user_id="user-upload-rename",
        message_id="message-upload-rename",
        message="按年份和正文标题重命名这个文件",
        attachments=[{"document_id": "document-upload-1"}],
    )

    assert plan.intent == "SUGGEST_RENAME"
    assert plan.slots["document_ids"] == ["document-upload-1"]
    assert plan.steps[0].tool_name == "generate-rename-suggestions"
    assert plan.steps[0].input == {"document_ids": ["document-upload-1"]}
    assert plan.selected_skills == ["file-rename", "operation-plan"]


def test_deterministic_planner_routes_attachment_wording_to_uploaded_rename():
    """只说“附件”而未出现“文件”时也应使用后端附件范围生成重命名计划。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-upload-attachment",
        user_id="user-upload-attachment",
        message_id="message-upload-attachment",
        message="重命名这个附件",
        attachments=[{"document_id": "document-upload-attachment"}],
    )

    assert plan.intent == "SUGGEST_RENAME"
    assert plan.steps[0].input == {"document_ids": ["document-upload-attachment"]}


def test_llm_rename_without_backend_file_scope_does_not_scan_all_managed_files():
    """LLM 只给重命名意图但没有附件或受管过滤条件时，必须请求范围而非全目录扫描。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="SUGGEST_RENAME",
            user_goal="重命名文件",
            referenced_document_ids=["llm-invented-document"],
        ),
        message="重命名文件",
        attachments=[],
    )

    assert plan.intent == "MISSING_FILE_SCOPE"
    assert plan.steps[0].tool_name == "intent-summary"


def test_llm_planner_keeps_uploaded_document_scope_for_rename():
    """LLM 结构化意图命中附件重命名时也必须生成临时文件计划，不得丢弃附件范围。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="SUGGEST_RENAME",
            user_goal="重命名刚上传的文件",
            referenced_document_ids=["document-upload-2"],
            required_capabilities=["suggest_rename"],
            tool_plan_hint=["generate-rename-suggestions"],
        ),
        message="重命名刚上传的文件",
        attachments=[{"document_id": "document-upload-2"}],
    )

    assert plan.intent == "SUGGEST_RENAME"
    assert plan.slots["document_ids"] == ["document-upload-2"]
    assert plan.steps[0].input == {"document_ids": ["document-upload-2"]}


def test_llm_planner_cannot_replace_backend_attachment_scope_for_rename():
    """LLM 自报的文档标识与后端附件不一致时，文件动作必须采用后端确定范围。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="SUGGEST_RENAME",
            user_goal="重命名这个附件",
            referenced_document_ids=["llm-invented-document"],
            required_capabilities=["suggest_rename"],
        ),
        message="重命名这个附件",
        attachments=[{"document_id": "backend-resolved-document"}],
    )

    assert plan.slots["document_ids"] == ["backend-resolved-document"]
    assert plan.steps[0].input == {"document_ids": ["backend-resolved-document"]}


def test_uploaded_attachment_rename_confirms_into_private_temporary_path(monkeypatch, tmp_path):
    """上传附件确认后只改临时存储名称；共享物理对象必须写时复制并保留其他用户文件。"""

    storage_root = tmp_path / "storage"
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(storage_root))
    client, SessionLocal = client_with_database()
    _, source_token = _register_and_login(client, "uploaded-rename-shared-source")
    _, target_token = _register_and_login(client, "uploaded-rename-target")
    content = "2026年春季学生活动总结\n本学期组织了多项活动。".encode()

    source_upload = client.post(
        "/api/files/upload",
        headers=_auth_header(source_token),
        files={"file": ("共享源文件.txt", content, "text/plain")},
    )
    target_upload = client.post(
        "/api/files/upload",
        headers=_auth_header(target_token),
        files={"file": ("扫描件.txt", content, "text/plain")},
    )
    source_document_id = source_upload.json()["document_id"]
    target_document_id = target_upload.json()["document_id"]

    db = SessionLocal()
    try:
        source_object = db.query(FileObject).filter_by(document_id=source_document_id).one()
        target_object = db.query(FileObject).filter_by(document_id=target_document_id).one()
        assert source_object.storage_path == target_object.storage_path
        shared_path = storage_root / source_object.storage_path
    finally:
        db.close()

    message_response = client.post(
        "/api/conversations/uploaded-rename-conversation/messages",
        headers=_auth_header(target_token),
        json={
            "content": "按年份和正文标题重命名这个文件",
            "attachments": [{"document_id": target_document_id}],
        },
    )

    assert message_response.status_code == 200
    run = message_response.json()["agent_run"]
    assert run["intent"] == "SUGGEST_RENAME"
    assert run["document_results"][0]["categories"] == []
    plan_id = run["operation_plan_id"]
    assert plan_id
    plan_response = client.get(
        f"/api/operations/plans/{plan_id}",
        headers=_auth_header(target_token),
    )
    plan = plan_response.json()
    assert plan["operation_type"] == "RENAME_UPLOADED_FILES"
    assert plan["status"] == "WAITING_CONFIRMATION"
    assert plan["items"][0]["before"]["filename"] == "扫描件.txt"
    assert plan["items"][0]["after"]["filename"] == "2026_春季学生活动总结.txt"
    assert plan["items"][0]["rename_metadata"]["parse_mode"] == "hybrid"
    assert plan["items"][0]["rename_metadata"]["candidate_parsers"] == ["native"]
    assert plan["items"][0]["rename_metadata"]["rename_validation"]["validation_mode"] == "risk_based"
    assert shared_path.exists()

    confirm_response = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(target_token),
        json={"confirmation": "确认执行"},
    )

    assert confirm_response.status_code == 200
    confirmed = confirm_response.json()
    assert confirmed["status"] == "EXECUTED"
    assert confirmed["changeset_id"]
    assert confirmed["result"]["completed_count"] == 1

    db = SessionLocal()
    try:
        source_document = db.get(Document, source_document_id)
        target_document = db.get(Document, target_document_id)
        source_object = db.query(FileObject).filter_by(document_id=source_document_id).one()
        target_object = db.query(FileObject).filter_by(document_id=target_document_id).one()
        assert source_document.original_filename == "共享源文件.txt"
        assert target_document.original_filename == "2026_春季学生活动总结.txt"
        assert source_object.storage_path != target_object.storage_path
        assert shared_path.exists()
        target_path = storage_root / target_object.storage_path
        assert target_path.name == "2026_春季学生活动总结.txt"
        assert target_path.read_bytes() == content
        change_item = (
            db.query(ChangeItem)
            .filter_by(target_document_id=target_document_id, change_type="FILENAME_CHANGED")
            .one()
        )
        assert change_item.before_value_json["filename"] == "扫描件.txt"
        assert change_item.after_value_json["filename"] == "2026_春季学生活动总结.txt"
    finally:
        db.close()

    source_content = client.get(
        f"/api/files/{source_document_id}/content",
        headers=_auth_header(source_token),
    )
    assert source_content.status_code == 200
    assert source_content.content == content
    clear_overrides()


def test_planner_targets_exact_managed_filename_for_rename():
    """目录下的完整文件名应转换为精确相对路径，而不是模糊包含条件。"""

    message = "对党办下科学发展观的讨论主题[1].doc 进行重命名"
    deterministic_plan = DeterministicPlanner().plan(
        conversation_id="conversation-exact",
        user_id="user-exact",
        message_id="message-exact",
        message=message,
        attachments=[],
    )
    llm_plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="SUGGEST_RENAME",
            user_goal=message,
            required_capabilities=["suggest_rename"],
            tool_plan_hint=["generate-rename-suggestions"],
        ),
        message=message,
        attachments=[],
    )

    for plan in [deterministic_plan, llm_plan]:
        assert plan.intent == "SUGGEST_RENAME"
        assert plan.steps[0].input["path_prefix"] == "党办"
        assert plan.steps[0].input["relative_path"] == "党办/科学发展观的讨论主题[1].doc"
        assert "filename_contains" not in plan.steps[0].input
        assert plan.steps[0].input["extension"] == "doc"


def test_managed_rename_chat_plan_and_confirm_executes_native_rename(monkeypatch, tmp_path):
    """聊天生成计划后文件保持不变，本人确认后才真实重命名并写 ChangeSet。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "扫描件.txt"
    source_path.write_text(
        "校发〔2026〕12号\n关于做好奖学金评审工作的通知\n各学院：\n现将有关事项通知如下。",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "rename-owner")
    response = client.post(
        "/api/conversations/rename-conversation/messages",
        headers=_auth_header(token),
        json={
            "content": "按年份、文号和正文标题重命名党办目录下的文件",
            "attachments": [],
        },
    )

    assert response.status_code == 200
    agent_run = response.json()["agent_run"]
    assert agent_run["intent"] == "SUGGEST_RENAME"
    assert agent_run["operation_plan_id"]
    assert source_path.exists()

    plan_response = client.get(
        f"/api/operations/plans/{agent_run['operation_plan_id']}",
        headers=_auth_header(token),
    )
    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["status"] == "WAITING_CONFIRMATION"
    assert plan["scope"]["path_prefix"] == "党办"
    assert plan["items"][0]["after"]["filename"] == (
        "2026_校发〔2026〕12号_关于做好奖学金评审工作的通知.txt"
    )
    assert plan["items"][0]["rename_metadata"]["parse_mode"] == "hybrid"
    assert plan["items"][0]["rename_metadata"]["candidate_parsers"] == ["native"]
    assert plan["items"][0]["rename_metadata"]["rename_validation"]["validation_mode"] == "risk_based"

    confirm_response = client.post(
        f"/api/operations/plans/{plan['id']}/confirm",
        headers=_auth_header(token),
        json={"confirmation": "确认执行"},
    )

    assert confirm_response.status_code == 200
    confirmed = confirm_response.json()
    assert confirmed["status"] == "EXECUTED"
    assert confirmed["changeset_id"]
    renamed_path = source_dir / "2026_校发〔2026〕12号_关于做好奖学金评审工作的通知.txt"
    assert renamed_path.exists()
    assert not source_path.exists()

    db = SessionLocal()
    try:
        managed_file = db.query(ManagedFile).one()
        assert managed_file.relative_path == renamed_path.relative_to(managed_root).as_posix()
        operation_plan = db.get(OperationPlan, plan["id"])
        assert operation_plan is not None
        assert operation_plan.status == "EXECUTED"
        batch_item = db.query(FileRenameBatchItem).one()
        assert batch_item.metadata_json["rename_validation"]["validation_mode"] == "risk_based"
        change_item = db.query(ChangeItem).filter(ChangeItem.change_type == "FILENAME_CHANGED").one()
        assert change_item.before_value_json["filename"] == "扫描件.txt"
        assert change_item.after_value_json["filename"] == renamed_path.name
    finally:
        db.close()
        clear_overrides()


def test_rename_suggestion_uses_second_version_when_base_name_exists(monkeypatch, tmp_path):
    """基础目标名已存在时应自动生成第二版建议，不覆盖既有文件。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    (source_dir / "扫描件.txt").write_text(
        "2026年春季学生活动总结\n本学期组织了多项活动。",
        encoding="utf-8",
    )
    existing = source_dir / "2026_春季学生活动总结.txt"
    existing.write_text("既有第一版内容", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-version-two")
    response = client.post(
        "/api/conversations/rename-version-two-conversation/messages",
        headers=_auth_header(token),
        json={"content": "对党办下扫描件.txt进行重命名", "attachments": []},
    )

    assert response.status_code == 200
    invocation = next(
        item
        for item in response.json()["agent_run"]["tool_invocations"]
        if item["tool_name"] == "generate-rename-suggestions"
    )
    suggestion = invocation["output_json"]["suggestions"][0]
    assert suggestion["status"] == "READY"
    assert suggestion["proposed_filename"] == "2026_春季学生活动总结_第二版.txt"
    assert "第二版" in suggestion["warnings"][0]
    assert existing.read_text(encoding="utf-8") == "既有第一版内容"
    clear_overrides()


def test_legacy_xls_extraction_failure_uses_filename_and_second_version(monkeypatch, tmp_path):
    """旧版 XLS 解析失败时仍应从结构化文件名生成第二版建议。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "发展规划处"
    source_dir.mkdir(parents=True)
    source_name = "附件 各学院实验实习用房需求摸底统计表(2026-04-10)-计算机学院20260413.xls"
    (source_dir / source_name).write_bytes(b"legacy-xls-placeholder")
    existing = source_dir / "2026_各学院实验实习用房需求统计表.xls"
    existing.write_bytes(b"existing-first-version")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)
    monkeypatch.setattr(
        "app.modules.file_rename.suggestion_service.extract_rename_primary",
        lambda **_: {
            "ok": False,
            "status": "FAILED",
            "extractor": "excel-xls-converted",
            "error": {"code": "XLS_CONVERSION_FAILED", "message": "测试转换失败"},
            "pages": [],
        },
    )

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-legacy-xls-fallback")
    response = client.post(
        "/api/conversations/rename-legacy-xls-conversation/messages",
        headers=_auth_header(token),
        json={"content": f"对发展规划处下{source_name}进行重命名", "attachments": []},
    )

    assert response.status_code == 200
    invocation = next(
        item
        for item in response.json()["agent_run"]["tool_invocations"]
        if item["tool_name"] == "generate-rename-suggestions"
    )
    suggestion = invocation["output_json"]["suggestions"][0]
    assert suggestion["status"] == "READY"
    assert suggestion["proposed_filename"] == "2026_各学院实验实习用房需求统计表_第二版.xls"
    assert "结构化文件名" in suggestion["warnings"][0]
    assert existing.read_bytes() == b"existing-first-version"
    clear_overrides()


def test_rename_suggestion_increments_existing_version_suffix(monkeypatch, tmp_path):
    """基础名称和第二版都存在时应继续生成第三版。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    (source_dir / "扫描件.txt").write_text(
        "2026年春季学生活动总结\n本学期组织了多项活动。",
        encoding="utf-8",
    )
    for filename in ["2026_春季学生活动总结.txt", "2026_春季学生活动总结_第二版.txt"]:
        (source_dir / filename).write_text("既有版本", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-version-three")
    response = client.post(
        "/api/conversations/rename-version-three-conversation/messages",
        headers=_auth_header(token),
        json={"content": "对党办下扫描件.txt进行重命名", "attachments": []},
    )

    invocation = next(
        item
        for item in response.json()["agent_run"]["tool_invocations"]
        if item["tool_name"] == "generate-rename-suggestions"
    )
    assert invocation["output_json"]["suggestions"][0]["proposed_filename"] == (
        "2026_春季学生活动总结_第三版.txt"
    )
    clear_overrides()


def test_batch_same_target_allocates_base_and_second_version(monkeypatch, tmp_path):
    """同一批次生成相同目标名时应预留基础名称，并为后续文件生成第二版。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    content = "2026年春季学生活动总结\n本学期组织了多项活动。"
    (source_dir / "扫描件甲.txt").write_text(content, encoding="utf-8")
    (source_dir / "扫描件乙.txt").write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-version-batch")
    response = client.post(
        "/api/conversations/rename-version-batch-conversation/messages",
        headers=_auth_header(token),
        json={"content": "按年份和正文标题重命名党办目录下的文件", "attachments": []},
    )

    invocation = next(
        item
        for item in response.json()["agent_run"]["tool_invocations"]
        if item["tool_name"] == "generate-rename-suggestions"
    )
    proposed_names = {
        item["proposed_filename"] for item in invocation["output_json"]["suggestions"]
    }
    assert proposed_names == {
        "2026_春季学生活动总结.txt",
        "2026_春季学生活动总结_第二版.txt",
    }
    assert invocation["output_json"]["ready_count"] == 2
    clear_overrides()


def test_batch_same_title_uses_full_date_to_distinguish_files(monkeypatch, tmp_path):
    """同目录同标题文件应使用精确到日的日期区分，扩展名不同也应参与分组。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "发展规划处"
    source_dir.mkdir(parents=True)
    content = "各学院实验实习用房需求统计表\n学院名称\t现有面积\t需求面积"
    (source_dir / "需求表-计算机学院20260413.txt").write_text(content, encoding="utf-8")
    (source_dir / "需求表-计算机学院20260508.md").write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-duplicate-title-date")
    response = client.post(
        "/api/conversations/rename-duplicate-title-date/messages",
        headers=_auth_header(token),
        json={"content": "按年份和正文标题重命名发展规划处目录下的文件", "attachments": []},
    )

    assert response.status_code == 200
    invocation = next(
        item
        for item in response.json()["agent_run"]["tool_invocations"]
        if item["tool_name"] == "generate-rename-suggestions"
    )
    proposed_names = {
        item["proposed_filename"] for item in invocation["output_json"]["suggestions"]
    }
    assert proposed_names == {
        "20260413_各学院实验实习用房需求统计表.txt",
        "20260508_各学院实验实习用房需求统计表.md",
    }
    assert invocation["output_json"]["ready_count"] == 2
    clear_overrides()


def test_missing_date_with_reliable_title_uses_title_only_filename(monkeypatch, tmp_path):
    """缺少日期但正文标题可靠时，应直接使用正文标题生成名称。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    (source_dir / "材料.txt").write_text(
        "春季学生活动总结\n本学期组织了多项活动。",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-title-only")
    response = client.post(
        "/api/conversations/rename-title-only/messages",
        headers=_auth_header(token),
        json={"content": "重命名党办目录下的文件", "attachments": []},
    )

    assert response.status_code == 200
    invocation = next(
        item
        for item in response.json()["agent_run"]["tool_invocations"]
        if item["tool_name"] == "generate-rename-suggestions"
    )
    assert invocation["output_json"]["ready_count"] == 1
    assert invocation["output_json"]["suggestions"][0]["proposed_filename"] == "春季学生活动总结.txt"
    assert response.json()["agent_run"]["operation_plan_id"]
    clear_overrides()


def test_needs_review_item_does_not_block_ready_operation_plan(monkeypatch, tmp_path):
    """待确认文件不得阻止自动建议文件生成和执行计划。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    (source_dir / "可重命名.txt").write_text(
        "2026年春季学生活动总结\n本学期组织了多项活动。",
        encoding="utf-8",
    )
    (source_dir / "待复核.txt").write_text("1\n2", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-review")
    response = client.post(
        "/api/conversations/rename-review-conversation/messages",
        headers=_auth_header(token),
        json={"content": "按年份和正文标题重命名党办目录下的文件", "attachments": []},
    )

    assert response.status_code == 200
    assert "另有 1 个文件待确认，不进入当前重命名计划" in response.json()["agent_run"]["final_response"]
    assert "缺少年份" not in response.json()["agent_run"]["final_response"]
    invocation = next(
        item
        for item in response.json()["agent_run"]["tool_invocations"]
        if item["tool_name"] == "generate-rename-suggestions"
    )
    assert invocation["output_json"]["ready_count"] == 1
    assert invocation["output_json"]["needs_review_count"] == 1
    plan_id = response.json()["agent_run"]["operation_plan_id"]
    assert plan_id
    assert invocation["output_json"]["status"] == "WAITING_CONFIRMATION"
    assert invocation["output_json"]["rename_batch_id"]
    confirmed = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(token),
        json={"confirmation": "确认执行", "excluded_rename_batch_item_ids": []},
    )
    assert confirmed.status_code == 200
    assert not (source_dir / "可重命名.txt").exists()
    assert (source_dir / "2026_春季学生活动总结.txt").exists()
    assert (source_dir / "待复核.txt").exists()
    clear_overrides()


def test_user_correction_immediately_confirms_and_renames_review_item(monkeypatch, tmp_path):
    """用户明确提供新名称时应创建确认记录并立即执行，不再二次确认。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "待复核.txt"
    source_path.write_text("没有可识别的日期和规范标题", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "rename-manual-correction")
    first = client.post(
        "/api/conversations/rename-manual-conversation/messages",
        headers=_auth_header(token),
        json={"content": "对党办下待复核.txt进行重命名", "attachments": []},
    )

    assert first.status_code == 200
    assert first.json()["agent_run"]["operation_plan_id"] is None
    assert source_path.exists()

    corrected = client.post(
        "/api/conversations/rename-manual-conversation/messages",
        headers=_auth_header(token),
        json={"content": "文件待复核.txt更正为2026_人工确认标题", "attachments": []},
    )

    assert corrected.status_code == 200
    run = corrected.json()["agent_run"]
    assert run["intent"] == "RESOLVE_RENAME_REVIEW"
    assert run["operation_plan_id"]
    assert "党办/待复核.txt -> 党办/2026_人工确认标题.txt" in run["final_response"]
    assert (source_dir / "2026_人工确认标题.txt").exists()
    assert not source_path.exists()

    db = SessionLocal()
    try:
        review = db.query(FileRenameReviewItem).one()
        assert review.status == "EXECUTED"
        plan = db.get(OperationPlan, run["operation_plan_id"])
        assert plan is not None
        assert plan.status == "EXECUTED"
        assert db.query(OperationConfirmation).filter_by(operation_plan_id=plan.id).count() == 1
        assert db.query(ChangeItem).filter_by(change_type="FILENAME_CHANGED").count() == 1
    finally:
        db.close()
        clear_overrides()


def test_batch_correction_executes_only_user_named_review_item(monkeypatch, tmp_path):
    """对话确认待确认文件时，应独立执行且不连带未确认的自动建议。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    ready_path = source_dir / "自动项.txt"
    review_path = source_dir / "人工项.txt"
    ready_path.write_text("2026年春季学生活动总结\n本学期组织了多项活动。", encoding="utf-8")
    review_path.write_text("没有可识别的日期和规范标题", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "rename-complete-batch")
    url = "/api/conversations/rename-complete-batch/messages"
    initial = client.post(
        url,
        headers=_auth_header(token),
        json={"content": "对党办目录下的文件进行重命名", "attachments": []},
    )
    initial_plan_id = initial.json()["agent_run"]["operation_plan_id"]
    assert initial_plan_id

    corrected = client.post(
        url,
        headers=_auth_header(token),
        json={"content": "文件人工项.txt更正为人工确认标题", "attachments": []},
    )

    assert corrected.status_code == 200
    assert corrected.json()["agent_run"]["operation_plan_id"]
    assert ready_path.exists()
    assert not review_path.exists()
    assert not (source_dir / "2026_春季学生活动总结.txt").exists()
    assert (source_dir / "人工确认标题.txt").exists()
    db = SessionLocal()
    try:
        batch = db.query(FileRenameBatch).one()
        assert batch.status == "READY_FOR_CONFIRMATION"
        assert batch.completed_count == 1
        assert {item.status for item in db.query(FileRenameBatchItem).all()} == {"READY", "COMPLETED"}
        initial_plan = db.get(OperationPlan, initial_plan_id)
        assert initial_plan is not None
        assert initial_plan.status == "WAITING_CONFIRMATION"
    finally:
        db.close()
        clear_overrides()


def test_rename_batch_api_returns_summary_and_cursor_pages(monkeypatch, tmp_path):
    """大批次回执只预览十项，完整文件清单通过受控游标接口读取。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    for index in range(12):
        (source_dir / f"待复核-{index:02d}.txt").write_text(
            "没有可识别的日期和规范标题",
            encoding="utf-8",
        )
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-batch-page")
    response = client.post(
        "/api/conversations/rename-batch-page/messages",
        headers=_auth_header(token),
        json={"content": "对党办目录下的文件进行重命名", "attachments": []},
    )

    invocation = next(
        item
        for item in response.json()["agent_run"]["tool_invocations"]
        if item["tool_name"] == "generate-rename-suggestions"
    )
    output = invocation["output_json"]
    assert output["matched_count"] == 12
    assert len(output["suggestions"]) == 10
    assert output["suggestions_truncated"] is True
    batch_id = output["rename_batch_id"]

    summary = client.get(f"/api/file-renames/batches/{batch_id}", headers=_auth_header(token))
    assert summary.status_code == 200
    assert summary.json()["total_count"] == 12
    assert summary.json()["needs_review_count"] == 12
    assert len(summary.json()["preview_items"]) == 10

    first_page = client.get(
        f"/api/file-renames/batches/{batch_id}/items?status=NEEDS_REVIEW&limit=5",
        headers=_auth_header(token),
    )
    assert len(first_page.json()["items"]) == 5
    cursor = first_page.json()["next_cursor"]
    second_page = client.get(
        f"/api/file-renames/batches/{batch_id}/items?status=NEEDS_REVIEW&limit=5&cursor={cursor}",
        headers=_auth_header(token),
    )
    assert len(second_page.json()["items"]) == 5
    assert second_page.json()["next_cursor"] is not None
    clear_overrides()


def test_duplicate_pending_filename_does_not_block_unique_correction(monkeypatch, tmp_path):
    """同名文件存在歧义时，消息中唯一匹配的文件仍应独立执行。"""

    managed_root = tmp_path / "managed"
    for directory, filename in [("党办/一", "通知.txt"), ("党办/二", "通知.txt"), ("党办", "唯一.txt")]:
        path = managed_root / directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("没有可识别的日期和规范标题", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-ambiguous")
    conversation_url = "/api/conversations/rename-ambiguous-conversation/messages"
    response = client.post(
        conversation_url,
        headers=_auth_header(token),
        json={"content": "对党办目录下的文件进行重命名", "attachments": []},
    )
    assert response.status_code == 200

    corrected = client.post(
        conversation_url,
        headers=_auth_header(token),
        json={
            "content": "文件通知.txt更正为2026_通知\n文件唯一.txt更正为2026_唯一文件",
            "attachments": [],
        },
    )

    assert corrected.status_code == 200
    run = corrected.json()["agent_run"]
    assert run["status"] == "NEEDS_REVIEW"
    assert "“通知.txt”匹配到多个待复核文件" in run["final_response"]
    assert "党办/一/通知.txt" in run["final_response"]
    assert "党办/二/通知.txt" in run["final_response"]
    assert run["operation_plan_id"]
    assert not (managed_root / "党办" / "唯一.txt").exists()
    assert (managed_root / "党办" / "2026_唯一文件.txt").exists()
    assert (managed_root / "党办" / "一" / "通知.txt").exists()
    assert (managed_root / "党办" / "二" / "通知.txt").exists()
    clear_overrides()


def test_confirmed_rename_excludes_unchecked_batch_item(monkeypatch, tmp_path):
    """用户取消勾选只排除该文件，其他已选文件继续执行。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    first_path = source_dir / "甲.txt"
    second_path = source_dir / "乙.txt"
    first_path.write_text("2026年春季学生活动总结\n本学期组织了多项活动。", encoding="utf-8")
    second_path.write_text("2026年秋季资助工作报告\n本年度资助工作已经完成。", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "rename-checkbox-selection")
    response = client.post(
        "/api/conversations/rename-checkbox-selection/messages",
        headers=_auth_header(token),
        json={"content": "对党办目录下的文件进行重命名", "attachments": []},
    )
    plan_id = response.json()["agent_run"]["operation_plan_id"]
    plan = client.get(f"/api/operations/plans/{plan_id}", headers=_auth_header(token)).json()
    excluded = next(
        item for item in plan["items"] if item["before"]["filename"] == "乙.txt"
    )
    excluded_item_id = excluded["rename_metadata"]["rename_batch_item_id"]

    confirmed = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(token),
        json={
            "confirmation": "确认执行",
            "excluded_rename_batch_item_ids": [excluded_item_id],
        },
    )

    assert confirmed.status_code == 200
    assert not first_path.exists()
    assert (source_dir / "2026_春季学生活动总结.txt").exists()
    assert second_path.exists()
    assert not (source_dir / "2026_秋季资助工作报告.txt").exists()
    db = SessionLocal()
    try:
        statuses = {
            item.original_filename: item.status
            for item in db.query(FileRenameBatchItem).all()
        }
        assert statuses == {"甲.txt": "COMPLETED", "乙.txt": "EXCLUDED"}
    finally:
        db.close()
        clear_overrides()


def test_existing_target_name_only_fails_conflicting_correction(monkeypatch, tmp_path):
    """目标文件名已存在时应提示冲突，但同一消息中的其他文件继续重命名。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    for filename in ["甲.txt", "乙.txt"]:
        (source_dir / filename).write_text("没有可识别的日期和规范标题", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, _ = client_with_database()
    _, token = _register_and_login(client, "rename-target-conflict")
    url = "/api/conversations/rename-target-conflict-conversation/messages"
    response = client.post(
        url,
        headers=_auth_header(token),
        json={"content": "对党办目录下的文件进行重命名", "attachments": []},
    )
    assert response.status_code == 200
    (source_dir / "重复.txt").write_text("已经存在的目标文件", encoding="utf-8")

    corrected = client.post(
        url,
        headers=_auth_header(token),
        json={
            "content": "文件甲.txt更正为重复.txt\n文件乙.txt更正为新的乙文件",
            "attachments": [],
        },
    )

    assert corrected.status_code == 200
    final_response = corrected.json()["agent_run"]["final_response"]
    assert "目标文件名重复，请确认并提供其他名称" in final_response
    assert (source_dir / "甲.txt").exists()
    assert (source_dir / "重复.txt").exists()
    assert (source_dir / "新的乙文件.txt").exists()
    assert not (source_dir / "乙.txt").exists()

    retried = client.post(
        url,
        headers=_auth_header(token),
        json={"content": "文件甲.txt更正为不重复的甲文件", "attachments": []},
    )
    assert retried.status_code == 200
    assert retried.json()["agent_run"]["operation_plan_id"]
    assert not (source_dir / "甲.txt").exists()
    assert (source_dir / "不重复的甲文件.txt").exists()
    clear_overrides()


def test_user_can_dismiss_pending_rename_reviews(monkeypatch, tmp_path):
    """用户回复不需要时应关闭待复核项且不创建文件操作计划。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "待复核.txt"
    source_path.write_text("没有可识别的日期和规范标题", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "rename-dismiss")
    url = "/api/conversations/rename-dismiss-conversation/messages"
    client.post(
        url,
        headers=_auth_header(token),
        json={"content": "对党办下待复核.txt进行重命名", "attachments": []},
    )
    dismissed = client.post(
        url,
        headers=_auth_header(token),
        json={"content": "不需要", "attachments": []},
    )

    assert dismissed.status_code == 200
    assert dismissed.json()["agent_run"]["intent"] == "RESOLVE_RENAME_REVIEW"
    assert "已跳过 1 个待复核文件" in dismissed.json()["agent_run"]["final_response"]
    assert source_path.exists()
    db = SessionLocal()
    try:
        assert db.query(FileRenameReviewItem).one().status == "DISMISSED"
    finally:
        db.close()
        clear_overrides()


def test_confirmed_rename_isolates_stale_file_failure(monkeypatch, tmp_path):
    """批次中一个源文件变化时应只失败该项，其他文件仍可完成改名。"""

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    stable_path = source_dir / "稳定文件.txt"
    stale_path = source_dir / "变化文件.txt"
    stable_path.write_text("2026年春季学生活动总结\n本学期组织了多项活动。", encoding="utf-8")
    stale_path.write_text("2026年秋季资助工作报告\n本年度资助工作已经完成。", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "rename-partial")
    response = client.post(
        "/api/conversations/rename-partial-conversation/messages",
        headers=_auth_header(token),
        json={"content": "按年份和正文标题重命名党办目录下的文件", "attachments": []},
    )
    plan_id = response.json()["agent_run"]["operation_plan_id"]
    stale_path.write_text("2026年秋季资助工作报告\n内容在确认前发生变化。", encoding="utf-8")

    confirm_response = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(token),
        json={"confirmation": "确认执行"},
    )

    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "PARTIAL"
    result = confirm_response.json()["result"]
    assert result["completed_count"] == 1
    assert result["failed_count"] == 1
    assert stale_path.exists()
    assert not stable_path.exists()
    db = SessionLocal()
    try:
        change_types = {item.change_type for item in db.query(ChangeItem).all()}
        assert {"FILENAME_CHANGED", "FILE_OPERATION_FAILED"}.issubset(change_types)
    finally:
        db.close()
        clear_overrides()


def test_confirmed_rename_uses_configured_batch_executor(monkeypatch, tmp_path):
    """确认接口应通过执行器工厂调用批次契约并保存执行器审计摘要。"""

    class FakeF2Executor:
        """使用 Native 文件动作模拟通过契约校验的 F2。"""

        name = "f2"

        def __init__(self) -> None:
            self.native = NativeRenameExecutor()

        def preview_batch(self, request):
            return self.native.preview_batch(request).model_copy(
                update={"executor": "f2", "executor_version": "2.2.2"}
            )

        def execute_batch(self, request):
            return self.native.execute_batch(request).model_copy(
                update={"executor": "f2", "executor_version": "2.2.2"}
            )

        def compensate_batch(self, request, result):
            return self.native.compensate_batch(request, result)

    managed_root = tmp_path / "managed"
    source_dir = managed_root / "党办"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "待改名.txt"
    source_path.write_text("2026年春季学生活动总结\n本学期组织了多项活动。", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _configure_test_managed_root(monkeypatch, managed_root)
    monkeypatch.setattr(
        "app.modules.file_rename.execution_service.create_rename_executor",
        lambda settings: FakeF2Executor(),
    )

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "rename-f2-owner")
    response = client.post(
        "/api/conversations/rename-f2-conversation/messages",
        headers=_auth_header(token),
        json={"content": "按年份和正文标题重命名党办目录下的文件", "attachments": []},
    )
    plan_id = response.json()["agent_run"]["operation_plan_id"]
    confirm_response = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(token),
        json={"confirmation": "确认执行"},
    )

    assert confirm_response.status_code == 200
    result = confirm_response.json()["result"]
    assert result["executor"] == "f2"
    assert result["executor_version"] == "2.2.2"
    db = SessionLocal()
    try:
        operation_plan = db.get(OperationPlan, plan_id)
        assert operation_plan.plan_json["execution"]["executor"] == "f2"
        invocation = (
            db.query(ToolInvocation)
            .filter(ToolInvocation.tool_name == "confirmed-file-action")
            .one()
        )
        assert invocation.output_json["executor"] == "f2"
        assert "root_path" not in invocation.output_json
    finally:
        db.close()
        clear_overrides()
