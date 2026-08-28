"""统一文件任务回执阶段一、阶段二测试。

这些测试保护公共回执只消费安全投影、只覆盖已承诺的只读任务，并明确说明原文件未改变。
"""

from app.modules.agent.state import AgentRunResult, ToolInvocationRecord
from app.modules.agent.user_receipt import build_user_task_receipt


def _make_result(
    *,
    intent: str | None,
    status: str = "COMPLETED",
    document_results: list[dict] | None = None,
    tool_invocations: list[ToolInvocationRecord] | None = None,
    search_context: dict | None = None,
    final_response: str = "任务完成",
) -> AgentRunResult:
    """构造不依赖数据库或外部模型的确定性 AgentRunResult。"""

    return AgentRunResult(
        agent_run_id="run-presentation-1",
        conversation_id="conv-presentation-1",
        user_id="user-presentation-1",
        message_id="msg-presentation-1",
        intent=intent,
        status=status,
        selected_skills=[],
        tool_plan={"slots": {"document_ids": ["document-1"]}},
        tool_results=[],
        tool_invocations=tool_invocations or [],
        document_results=document_results or [],
        search_context=search_context or {},
        final_response=final_response,
    )


def test_plain_chat_keeps_legacy_text_without_file_presentation() -> None:
    """普通聊天不能被误包装成文件任务。"""

    receipt = build_user_task_receipt(
        _make_result(intent="GENERAL_CHAT", final_response="你好")
    )

    assert receipt.response_type == "text"
    assert receipt.presentation is None


def test_later_phase_classification_keeps_existing_receipt_during_stage_two() -> None:
    """阶段二不能把分类结果误包装成只读文件读取回执。"""

    receipt = build_user_task_receipt(
        _make_result(
            intent="CLASSIFY_FILES",
            document_results=[
                {
                    "document_id": "document-1",
                    "filename": "制度.docx",
                    "extraction_status": "COMPLETED",
                    "page_count": 1,
                    "char_count": 100,
                    "text_reused": False,
                    "classification_reused": False,
                    "categories": [],
                    "warnings": [],
                    "errors": [],
                }
            ],
        )
    )

    assert receipt.response_type == "file_results"
    assert receipt.presentation is None


def test_document_results_without_read_intent_do_not_infer_read_presentation() -> None:
    """逐文件结果是结果容器，不得被回执层单独解释成用户要求读取文件。"""

    receipt = build_user_task_receipt(
        _make_result(
            intent="BACKGROUND_TASK",
            document_results=[
                {
                    "document_id": "document-1",
                    "filename": "制度.docx",
                    "extraction_status": "COMPLETED",
                }
            ],
        )
    )

    assert receipt.response_type == "file_results"
    assert receipt.presentation is None


def test_historical_lifecycle_rename_result_is_not_presented_as_file_read() -> None:
    """历史生命周期消息应按结构化命名建议映射，不能显示为文件读取结果。"""

    receipt = build_user_task_receipt(
        _make_result(
            intent="SYSTEM_FILE_LIFECYCLE",
            tool_invocations=[
                ToolInvocationRecord(
                    tool_name="document-background-analysis",
                    input_json={"target_type": "document_version"},
                    output_json={"status": "COMPLETED"},
                    status="COMPLETED",
                )
            ],
            document_results=[
                {
                    "document_id": "document-1",
                    "working_copy_id": "copy-1",
                    "filename": "审计报告.pdf",
                    "extraction_status": "COMPLETED",
                    "rename_suggestion": {
                        "proposed_filename": "2025_审计报告.pdf",
                    },
                    "pending_decision": {
                        "type": "rename_suggestion",
                        "message": "是否生成改名计划？",
                    },
                }
            ],
            final_response="系统已生成命名建议，当前尚未改名。",
        )
    )

    assert receipt.presentation is not None
    assert receipt.presentation.task_kind == "RENAME_SUGGESTION"
    assert receipt.presentation.title == "文件命名建议"
    assert "读取" not in str(receipt.presentation.model_dump())
    assert receipt.presentation.change_impact.operation_executed is False
    assert receipt.presentation.change_impact.working_copies_changed is False
    assert receipt.task_status == "needs_attention"


def test_lifecycle_classification_and_archive_have_explicit_business_titles() -> None:
    """后台分类和上传归档必须展示各自业务类型，不得共用读取标题。"""

    classification_receipt = build_user_task_receipt(
        _make_result(
            intent="SYSTEM_FILE_LIFECYCLE",
            tool_invocations=[
                ToolInvocationRecord(
                    tool_name="managed-source-auto-classification",
                    input_json={"target_type": "managed_file_revision"},
                    output_json={"status": "COMPLETED"},
                    status="COMPLETED",
                )
            ],
            document_results=[
                {
                    "document_id": "document-1",
                    "filename": "工会材料.doc",
                    "extraction_status": "COMPLETED",
                    "categories": [{"name": "工会"}],
                }
            ],
        )
    )
    archive_receipt = build_user_task_receipt(
        _make_result(
            intent="SYSTEM_FILE_LIFECYCLE",
            tool_invocations=[
                ToolInvocationRecord(
                    tool_name="upload-archive",
                    input_json={"target_type": "managed_file"},
                    output_json={"status": "COMPLETED"},
                    status="COMPLETED",
                )
            ],
            final_response="文件原件已归档。",
        )
    )

    assert classification_receipt.presentation is not None
    assert classification_receipt.presentation.task_kind == "CLASSIFY"
    assert classification_receipt.presentation.title == "文件分类结果"
    assert archive_receipt.presentation is not None
    assert archive_receipt.presentation.task_kind == "INGEST"
    assert archive_receipt.presentation.title == "文件归档结果"
    assert "读取结果" not in str(classification_receipt.presentation.model_dump())
    assert "读取结果" not in str(archive_receipt.presentation.model_dump())


def test_search_receipt_has_scope_counts_change_impact_and_safe_actions() -> None:
    """文件搜索必须展示业务范围、结果完整性和只读状态。"""

    invocation = ToolInvocationRecord(
        tool_name="hybrid-search",
        input_json={"query": "工作总结"},
        output_json={
            "kind": "workspace_file_search",
            "ok": True,
            "query": "工作总结",
            "total_returned": 2,
            "supported_count": 1,
            "possible_count": 1,
            "partial": False,
            "results": [
                {
                    "managed_file_id": "managed-1",
                    "document_id": "document-1",
                    "document_version_id": "version-1",
                    "filename": "学校工作总结.docx",
                    "root_key": "school_files",
                    "relative_path": "办公室/学校工作总结.docx",
                    "relevance_tier": "SUPPORTED",
                },
                {
                    "managed_file_id": "managed-2",
                    "document_id": "document-2",
                    "document_version_id": "version-2",
                    "filename": "学院工作要点.docx",
                    "root_key": "school_files",
                    "relative_path": "学院/学院工作要点.docx",
                    "relevance_tier": "POSSIBLE",
                },
            ],
            "search_completeness": {
                "status": "COMPLETE",
                "can_claim_complete": True,
                "scope_label": "当前共享工作区全部活动文件",
                "eligible_file_count": 20,
                "ready_file_count": 20,
                "pending_file_count": 0,
                "failed_file_count": 0,
                "candidate_limit_reached": False,
                "message": "已检索当前范围内全部可用文件。",
            },
        },
        status="COMPLETED",
    )
    receipt = build_user_task_receipt(
        _make_result(
            intent="SEARCH_FILES",
            tool_invocations=[invocation],
            search_context={
                "effective_conditions": [
                    {
                        "label": "范围",
                        "value": "学校",
                        "condition_type": "scope",
                        "status": "APPLIED",
                        "source": "user_and_llm",
                        "document_ids": ["document-1"],
                    },
                    {
                        "label": "主题",
                        "value": "工作总结",
                        "condition_type": "topic",
                        "status": "APPLIED",
                        "source": "user",
                    },
                ]
            },
        )
    )

    presentation = receipt.presentation
    assert presentation is not None
    assert presentation.task_kind == "SEARCH"
    assert presentation.request.scope_label == "学校"
    assert presentation.outcome.total_count == 2
    assert presentation.outcome.completed_count == 1
    assert presentation.outcome.needs_review_count == 1
    assert presentation.outcome.completeness == "COMPLETE"
    assert presentation.change_impact.originals_changed is False
    assert "原文件" in presentation.change_impact.message
    assert all(action.action_kind == "FILL_PROMPT" for action in presentation.next_actions)
    serialized = presentation.model_dump()
    assert "workspace" not in str(serialized)
    assert "document_ids" not in str(serialized)
    assert "user_and_llm" not in str(serialized)


def test_managed_file_list_uses_common_read_only_shell() -> None:
    """目录列举应进入统一外壳，并继续保留安全逻辑路径明细。"""

    invocation = ToolInvocationRecord(
        tool_name="managed-file-list",
        input_json={"root_key": "school_files"},
        output_json={
            "kind": "managed_file_list",
            "ok": True,
            "query": {
                "root_key": "school_files",
                "root_display_name": "学校文件库",
            },
            "files": [
                {
                    "managed_file_id": "managed-1",
                    "root_key": "school_files",
                    "display_name": "学校文件库",
                    "relative_path": "办公室/总结.docx",
                    "filename": "总结.docx",
                    "extension": ".docx",
                    "size_bytes": 100,
                    "status": "ACTIVE",
                }
            ],
        },
        status="COMPLETED",
    )

    receipt = build_user_task_receipt(
        _make_result(intent="LIST_MANAGED_FILES", tool_invocations=[invocation])
    )

    assert receipt.presentation is not None
    assert receipt.presentation.task_kind == "LIST"
    assert receipt.presentation.outcome.total_count == 1
    assert receipt.presentation.request.scope_label == "学校文件库"
    assert receipt.managed_file_result["root_display_name"] == "学校文件库"
    assert receipt.presentation.change_impact.operation_executed is False


def test_managed_file_list_without_root_keeps_all_roots_scope() -> None:
    """跨多个受管根目录列举时，不能把第一个文件的位置误报为整个查询范围。"""

    invocation = ToolInvocationRecord(
        tool_name="managed-file-list",
        input_json={},
        output_json={
            "kind": "managed_file_list",
            "ok": True,
            "query": {
                "root_key": None,
                "root_display_name": "全部受管目录",
            },
            "files": [
                {
                    "root_key": "school_files",
                    "display_name": "学校文件库",
                    "relative_path": "办公室/总结.docx",
                    "filename": "总结.docx",
                },
                {
                    "root_key": "student_affairs",
                    "display_name": "学工文件库",
                    "relative_path": "资助/名单.xlsx",
                    "filename": "名单.xlsx",
                },
            ],
        },
        status="COMPLETED",
    )

    receipt = build_user_task_receipt(
        _make_result(intent="LIST_MANAGED_FILES", tool_invocations=[invocation])
    )

    assert receipt.managed_file_result is not None
    assert receipt.managed_file_result["root_key"] is None
    assert receipt.managed_file_result["root_display_name"] == "全部受管目录"
    assert receipt.presentation is not None
    assert receipt.presentation.request.scope_label == "全部受管目录"
    assert receipt.presentation.request.target_label == "全部受管目录"


def test_read_and_summary_document_results_use_common_shell() -> None:
    """读取和总结必须使用相同外壳，但保留不同任务名称。"""

    document_results = [
        {
            "document_id": "document-1",
            "filename": "制度.docx",
            "extraction_status": "COMPLETED",
            "page_count": 2,
            "char_count": 300,
            "text_reused": False,
            "classification_reused": False,
            "categories": [],
            "warnings": [],
            "errors": [],
        }
    ]
    read_receipt = build_user_task_receipt(
        _make_result(intent="READ_DOCUMENTS", document_results=document_results)
    )
    summary_receipt = build_user_task_receipt(
        _make_result(intent="SUMMARIZE_DOCUMENTS", document_results=document_results)
    )

    assert read_receipt.presentation is not None
    assert read_receipt.presentation.task_kind == "READ"
    assert read_receipt.presentation.outcome.completed_count == 1
    assert summary_receipt.presentation is not None
    assert summary_receipt.presentation.task_kind == "SUMMARIZE"
    assert summary_receipt.presentation.outcome.headline == "已总结 1 个文件"
    assert summary_receipt.presentation.change_impact.originals_changed is False


def test_evidence_answer_uses_reference_count_without_copying_quotes_to_shell() -> None:
    """证据回答公共外壳只统计来源文件，不复制原文片段。"""

    invocation = ToolInvocationRecord(
        tool_name="evidence-answer",
        input_json={"query": "制度何时生效"},
        output_json={
            "kind": "evidence_answer",
            "ok": True,
            "status": "ANSWERED",
            "answer": "制度自发布之日起施行。",
            "limitations": [],
            "references": [
                {
                    "document_id": "document-1",
                    "document_version_id": "version-1",
                    "working_copy_id": "copy-1",
                    "filename": "制度.docx",
                    "category_labels": ["规章制度"],
                    "availability": "AVAILABLE",
                    "reference_indexes": [1],
                    "evidence_items": [
                        {"quote": "本制度自发布之日起施行。", "page_number": 2}
                    ],
                }
            ],
        },
        status="COMPLETED",
    )

    receipt = build_user_task_receipt(
        _make_result(intent="EVIDENCE_ANSWER", tool_invocations=[invocation])
    )

    assert receipt.presentation is not None
    assert receipt.presentation.task_kind == "ANSWER"
    assert receipt.presentation.outcome.total_count == 1
    assert "本制度自发布之日起施行" not in str(receipt.presentation.model_dump())
    assert "原文件未改变" in receipt.presentation.change_impact.message


def test_partial_evidence_answer_does_not_invent_file_review_counts() -> None:
    """证据不足是回答级状态，不能凭空减去一个完成文件或增加一个待复核文件。"""

    invocation = ToolInvocationRecord(
        tool_name="evidence-answer",
        input_json={"query": "制度是否适用于全部单位"},
        output_json={
            "kind": "evidence_answer",
            "ok": True,
            "status": "PARTIAL",
            "answer": "现有依据只能确认部分单位。",
            "references": [
                {
                    "document_id": f"document-{index}",
                    "document_version_id": f"version-{index}",
                    "filename": f"制度{index}.docx",
                    "evidence_items": [
                        {"quote": "本办法适用于校内单位。", "page_number": index}
                    ],
                }
                for index in (1, 2)
            ],
        },
        status="COMPLETED",
    )

    receipt = build_user_task_receipt(
        _make_result(intent="EVIDENCE_ANSWER", tool_invocations=[invocation])
    )

    assert receipt.presentation is not None
    assert receipt.presentation.outcome.total_count == 2
    assert receipt.presentation.outcome.completed_count == 2
    assert receipt.presentation.outcome.needs_review_count == 0
    assert receipt.presentation.outcome.completeness == "PARTIAL"


def test_spreadsheet_text_result_also_gets_common_shell() -> None:
    """当前仍以文本返回的表格分析也必须进入统一文件任务外壳。"""

    invocation = ToolInvocationRecord(
        tool_name="analyze-spreadsheet",
        input_json={"document_id": "document-1"},
        output_json={
            "kind": "spreadsheet_analysis",
            "ok": True,
            "status": "COMPLETED",
            "document_id": "document-1",
            "analysis": {"title": "表格汇总结果"},
        },
        status="COMPLETED",
    )

    receipt = build_user_task_receipt(
        _make_result(
            intent="ANALYZE_SPREADSHEET",
            tool_invocations=[invocation],
            final_response="合计金额为 300。",
        )
    )

    assert receipt.response_type == "text"
    assert receipt.presentation is not None
    assert receipt.presentation.task_kind == "SPREADSHEET"
    assert receipt.presentation.outcome.completed_count == 1
    assert receipt.presentation.change_impact.originals_changed is False


def test_processing_read_task_uses_business_phase_without_internal_tool_name() -> None:
    """处理中回执只能展示业务阶段，不能泄漏 Tool 名称。"""

    receipt = build_user_task_receipt(
        _make_result(intent="READ_DOCUMENTS", status="RUNNING_TOOL")
    )

    assert receipt.presentation is not None
    assert receipt.presentation.phase.code == "PROCESSING"
    assert receipt.presentation.phase.label == "正在读取或处理文件内容"
    assert receipt.presentation.outcome.completed_count == 0
    assert receipt.presentation.outcome.headline == "正在读取 1 个文件"
    assert "Tool" not in receipt.presentation.phase.label
    assert receipt.presentation.next_actions == []


def test_failed_read_task_does_not_claim_it_is_still_processing() -> None:
    """任务级读取失败且没有逐文件事实时，结果摘要不得继续显示“正在读取”。"""

    receipt = build_user_task_receipt(
        _make_result(intent="READ_DOCUMENTS", status="FAILED", final_response="读取失败")
    )

    assert receipt.task_status == "failed"
    assert receipt.presentation is not None
    assert receipt.presentation.phase.code == "FAILED"
    assert receipt.presentation.outcome.headline == "文件读取任务未完成"
    assert "正在" not in receipt.presentation.outcome.headline


def test_topic_containing_workspace_word_is_not_rewritten_as_scope() -> None:
    """只有范围条件允许清洗 workspace 术语，普通主题必须保持用户原意。"""

    invocation = ToolInvocationRecord(
        tool_name="hybrid-search",
        input_json={"query": "工作区管理办法"},
        output_json={
            "kind": "workspace_file_search",
            "ok": True,
            "query": "工作区管理办法",
            "total_returned": 0,
            "results": [],
        },
        status="COMPLETED",
    )
    receipt = build_user_task_receipt(
        _make_result(
            intent="SEARCH_FILES",
            tool_invocations=[invocation],
            search_context={
                "effective_conditions": [
                    {
                        "label": "主题",
                        "value": "工作区管理办法",
                        "condition_type": "topic",
                        "status": "APPLIED",
                    },
                    {
                        "label": "范围",
                        "value": "当前工作区",
                        "condition_type": "scope",
                        "status": "APPLIED",
                    },
                ]
            },
        )
    )

    assert receipt.presentation is not None
    condition_values = {
        condition.label: condition.value
        for condition in receipt.presentation.request.conditions
    }
    assert condition_values["主题"] == "工作区管理办法"
    assert condition_values["范围"] == "当前可用文件范围"


def test_pending_decision_uses_needs_attention_phase_in_common_shell() -> None:
    """最终回执因待决策升级状态时，公共外壳不得仍显示处理完成。"""

    receipt = build_user_task_receipt(
        _make_result(
            intent="READ_DOCUMENTS",
            document_results=[
                {
                    "document_id": "document-1",
                    "filename": "制度.docx",
                    "extraction_status": "COMPLETED",
                    "pending_decision": {
                        "type": "document_review",
                        "message": "请确认文件范围。",
                    },
                }
            ],
        )
    )

    assert receipt.task_status == "needs_attention"
    assert receipt.presentation is not None
    assert receipt.presentation.phase.code == "NEEDS_ATTENTION"
    assert "需要确认" in receipt.presentation.phase.label
