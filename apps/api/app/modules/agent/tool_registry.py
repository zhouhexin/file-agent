"""File Agent Runtime 的 Tool 白名单与分发层。

Planner 输出永远不能直接调用 Tool handler，必须经过这里的 Registry。
这样未知 Tool 和非法输入会在副作用发生前被拒绝。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Type

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.logging import format_exception_traceback, log_event
from app.db.models import (
    Document,
    DocumentInsight,
    User,
    WorkingCopy,
)
from app.modules.agent.capabilities.service import load_agent_capabilities
from app.modules.agent.capability_suggestions import (
    CapabilitySuggestionRecordInput,
    CapabilitySuggestionService,
)
from app.modules.agent.mcp_filesystem_bridge import MCPFilesystemError, get_mcp_filesystem
from app.modules.agent.state import ToolInvocationRecord
from app.modules.agent.tool_contracts import (
    AgentCapabilitiesToolOutput,
    ClassificationTaxonomyToolOutput,
    DocumentClassificationsToolOutput,
    DocumentExtractionToolOutput,
    DocumentInsightsToolOutput,
    EvidenceAnswerToolOutput,
    GenericToolOutput,
    IntentSummaryToolOutput,
    ClassificationDecisionToolOutput,
    ManagedFileCollectionToolOutput,
    ManagedFileReadToolOutput,
    OperationPlanToolOutput,
    OriginalFileMetadataToolOutput,
    SpreadsheetToolOutput,
    StructuredExtractionToolOutput,
    ToolOutputValidationError,
    WorkspaceFileSearchToolOutput,
)
from app.modules.agent.tool_schemas import (
    AgentCapabilitiesReadInput,
    ClassificationDecisionInput,
    ClassificationTaxonomyReadInput,
    ConfirmedFileActionInput,
    DocumentClassificationsReadInput,
    DocumentInsightsReadInput,
    DocumentToolInput,
    EvidenceAnswerInput,
    FeedbackRecordInput,
    GenerateRenameSuggestionsInput,
    IntentSummaryInput,
    ManagedFileClassificationInput,
    ManagedFileListInput,
    ManagedFileReadDocumentInput,
    ManagedFileSearchInput,
    ManagedRootListInput,
    ManagedRootScanInput,
    MCPFilesystemInfoInput,
    MCPFilesystemListInput,
    MCPFilesystemSearchInput,
    ResolveRenameReviewsInput,
    SearchToolInput,
    SpreadsheetAnalysisInput,
    SpreadsheetDocumentInput,
    StructuredImageExtractionInput,
    ToolInputValidationError,
    WorkingCopyActionPlanInput,
)
from app.modules.classification.taxonomy_service import read_default_taxonomy_catalog
from app.modules.classification.evidence_reader import (
    CurrentClassificationEvidenceReader,
)
from app.modules.classification.conversation_decision import (
    ConversationalClassificationDecisionService,
)
from app.modules.chunks.service import DocumentIndexService
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.extractors import extract_document_text, extraction_config_hash
from app.modules.files.readable_source import (
    ReadableDocumentSource,
    ReadableDocumentSourceResolver,
    apply_readable_source_metadata,
)
from app.modules.file_rename.uploaded_suggestion_service import UploadedRenameSuggestionService
from app.modules.file_rename.uploaded_review_service import (
    UploadedRenameReviewResolutionService,
)
from app.modules.evidence_answer.service import EvidenceAnswerService
from app.modules.file_lifecycle.conversation_operations import ConversationalWorkingCopyPlanService
from app.modules.file_lifecycle.trash_lookup import ExactTrashFilenameLookupService
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.repository import FilesystemJobRepository, ManagedFileRepository
from app.modules.managed_files.service import (
    ManagedFileService,
    resolve_managed_file_query_scope,
    sync_configured_managed_roots,
)
from app.modules.managed_files.snapshot_service import ManagedFileSnapshotService
from app.modules.operations.schemas import OperationConfirmRequest
from app.modules.operations.service import OperationPlanService
from app.modules.retrieval.summary_search import WorkingCopySummarySearchService
from app.modules.skills.managed_file_query_feedback import (
    SKILL_ID as MANAGED_FILE_QUERY_SKILL_ID,
    record_managed_file_query_feedback_sample,
)
from app.modules.spreadsheet_analysis.service import SpreadsheetAnalysisService
from app.modules.spreadsheet_analysis.formatter import format_spreadsheet_analysis_response
from app.modules.spreadsheet_workbench.service import SpreadsheetWorkbenchService
from app.modules.structured_extraction.service import StructuredExtractionService


class UnknownToolError(ValueError):
    """Planner 引用了白名单外 Tool 时抛出。"""

    pass


ToolHandler = Callable[[BaseModel], Dict[str, Any]]
SEARCH_RESULT_CONFIRMATION_THRESHOLD = 20
ObservationPolicy = Literal[
    "CONTINUE_PLAN",
    "PLANNER_AFTER_EXECUTION",
    "PLANNER_ON_SIGNAL",
    "FINALIZE",
]


@dataclass(frozen=True)
class ToolDefinition:
    """Tool 的声明式元数据，以及 Registry 调用的 handler。"""

    name: str
    version: str
    description: str
    input_model: Type[BaseModel]
    output_model: Type[BaseModel]
    side_effects: bool
    risk_level: str
    requires_confirmation: bool
    allowed_roles: List[str]
    allowed_skill_ids: List[str]
    writes: List[str]
    failure_strategy: str
    retry_policy: str
    enabled: bool
    expose_to_planner: bool
    adaptive_ready: bool
    handler: ToolHandler
    # 未显式声明的自研或测试 Tool 维持旧行为，只在 handler 主动返回
    # replan_required 时才进入下一轮规划，避免升级后无意增加 LLM 调用。
    observation_policy: ObservationPolicy = "PLANNER_ON_SIGNAL"

    def catalog_item(self) -> Dict[str, Any]:
        """返回可安全暴露给 Tool catalog 接口的元数据。"""

        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
            "side_effects": self.side_effects,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "allowed_roles": self.allowed_roles,
            "allowed_skill_ids": self.allowed_skill_ids,
            "writes": self.writes,
            "failure_strategy": self.failure_strategy,
            "retry_policy": self.retry_policy,
            "enabled": self.enabled,
            "adaptive_ready": self.adaptive_ready,
            "observation_policy": self.observation_policy,
        }


class ToolRegistry:
    """内存态 MVP Tool Registry。

    Registry 是 Tool 名称、输入 schema、确认标记和副作用元数据的运行时强制边界。
    """

    def __init__(self, *, db: Any = None, user_id: str | None = None) -> None:
        """保存运行时上下文，并创建当前请求可用的 Tool 白名单。"""

        self.db = db
        self.user_id = user_id
        self._conversation_id: str | None = None
        self._agent_run_id: str | None = None
        self._tools = _build_mvp_tools(
            db=db,
            user_id=user_id,
            conversation_id_getter=lambda: self._conversation_id,
            agent_run_id_getter=lambda: self._agent_run_id,
        )

    def set_run_context(self, *, conversation_id: str, agent_run_id: str) -> None:
        """为本次 AgentRun 注入会话和运行 ID。

        两个 ID 只保存在本次 Registry；Planner 不能伪造，用于 L1 范围和检索澄清审计。
        """

        self._conversation_id = conversation_id
        self._agent_run_id = agent_run_id

    def set_conversation_id(self, conversation_id: str) -> None:
        """兼容旧测试的会话注入入口。"""

        self._conversation_id = conversation_id

    def list_tools(self, *, planner_only: bool = False) -> List[Dict[str, Any]]:
        """返回白名单 Tool；Planner Catalog 会排除内部系统 Tool。"""

        return [
            tool.catalog_item()
            for tool in self._tools.values()
            if tool.enabled
            and (
                not planner_only
                or (tool.expose_to_planner and tool.adaptive_ready)
            )
        ]

    def get(self, name: str) -> ToolDefinition:
        """获取白名单 Tool；如果 Planner 引用未知 Tool 则拒绝。"""

        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(f"Unknown tool: {name}") from exc
        if not tool.enabled:
            raise UnknownToolError(f"Tool is disabled: {name}")
        return tool

    def invoke(self, name: str, input_json: Dict[str, Any]) -> ToolInvocationRecord:
        """校验输入、调用 Tool handler，并返回结构化调用记录。"""

        tool = self.get(name)
        start = time.perf_counter()
        document_id = str(input_json.get("document_id") or "")
        log_event(
            "tool.invoke.started",
            tool_name=name,
            document_id=document_id or None,
            status="STARTED",
            message="Tool 调用开始",
            input_summary=_tool_input_summary(input_json),
        )
        try:
            tool_input = tool.input_model.model_validate(input_json)
        except ValidationError as exc:
            log_event(
                "tool.invoke.failed",
                level="ERROR",
                tool_name=name,
                document_id=document_id or None,
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code="TOOL_INPUT_VALIDATION_FAILED",
                message=str(exc),
            )
            raise ToolInputValidationError(str(exc)) from exc

        try:
            output = tool.handler(tool_input)
        except Exception as exc:
            log_event(
                "tool.invoke.failed",
                level="ERROR",
                tool_name=name,
                document_id=document_id or None,
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=exc.__class__.__name__,
                message=str(exc),
            )
            raise

        try:
            validated_output = tool.output_model.model_validate(output)
        except ValidationError as exc:
            log_event(
                "tool.invoke.failed",
                level="ERROR",
                tool_name=name,
                document_id=document_id or None,
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code="TOOL_OUTPUT_VALIDATION_FAILED",
                message=str(exc),
            )
            raise ToolOutputValidationError(
                f"Tool output validation failed: {exc}"
            ) from exc
        # output schema 负责验证，不得把模型默认值注入 handler 未返回的业务字段；
        # 否则异步处理中间态会被误投影成“已有空结果”。
        output = validated_output.model_dump(
            exclude_none=True,
            exclude_unset=True,
        )

        status = _tool_invocation_status(output)
        error = output.get("error") if isinstance(output.get("error"), dict) else {}
        log_event(
            "tool.invoke.completed",
            level="ERROR" if status == "FAILED" else "INFO",
            tool_name=name,
            document_id=str(output.get("document_id") or document_id) or None,
            status=status,
            duration_ms=int((time.perf_counter() - start) * 1000),
            error_code=error.get("code"),
            message="Tool 调用完成",
        )
        return ToolInvocationRecord(
            tool_name=name,
            input_json=tool_input.model_dump(),
            output_json=output,
            status=status,
            changeset_id=output.get("changeset_id"),
            operation_plan_id=output.get("operation_plan_id"),
        )


def _tool_invocation_status(output: Dict[str, Any]) -> str:
    """根据 Tool 业务输出确定审计状态，避免失败结果被记录为完成。"""

    if output.get("status") in {"PENDING", "PROCESSING"}:
        return "PENDING"
    if output.get("status") == "WAITING_FOR_ASYNC_JOB":
        return "WAITING_FOR_ASYNC_JOB"
    if output.get("ok") is False or output.get("status") == "FAILED":
        return "FAILED"
    if output.get("status") == "PARTIAL":
        return "PARTIAL"
    return "COMPLETED"


def _tool_input_summary(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """提取安全的 Tool 输入摘要，避免把正文或大对象写入日志。"""

    summary: Dict[str, Any] = {}
    for key in ["document_id", "document_ids", "force_reprocess", "operation_type", "intent"]:
        if key in input_json:
            summary[key] = input_json[key]
    return summary


def _attach_trash_restore_selection(
    *,
    result: Dict[str, Any],
    db: Any,
    user_id: str | None,
    filename: str,
) -> Dict[str, Any]:
    """为明确命中的回收站文件附加恢复选择数据，禁止继续返回正文。

    解析、表格分析和工作台 Tool 可能绕过 ``hybrid-search`` 直接读取
    ``document_id``，也必须复用同一张恢复选择卡。
    """

    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    if error.get("code") != "FILE_TRASHED" or db is None or user_id is None:
        return result
    selection = ExactTrashFilenameLookupService(
        db=db,
        user_id=user_id,
    ).lookup(query=filename)
    if selection:
        result["trash_restore_selection"] = selection
        result["user_message"] = str(selection.get("message") or "文件已删除，请选择是否恢复。")
    else:
        result["user_message"] = str(
            error.get("message") or "文件已删除并保存在回收站中，请先恢复后再读取。"
        )
    return result


def _chunk_build_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建真实 Chunk/Evidence 建索引 handler，正文只在持久化服务内部流转。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """在当前用户所有权边界内建立或复用最新文档版本索引。"""

        if db is None or user_id is None:
            return {
                "ok": False,
                "status": "FAILED",
                "error": {"code": "RUNTIME_CONTEXT_REQUIRED", "message": "原文索引上下文不可用。"},
            }
        return DocumentIndexService(db=db).build_latest_for_user(
            document_id=str(getattr(tool_input, "document_id")),
            user_id=user_id,
        )

    return handler


def _search_handler(
    db: Any,
    user_id: str | None,
    conversation_id_getter: Callable[[], str | None] | None = None,
    agent_run_id_getter: Callable[[], str | None] | None = None,
) -> ToolHandler:
    """创建摘要优先的工作副本文档级检索 handler。

    当 TWO_STAGE_RETRIEVAL_ENABLED=true 时改用两阶段检索服务（基于 document_search_profiles
    GIN 索引 + Chunk fallback + 候选内精查 + 确定性融合）。
    默认启用；关闭开关仅用于紧急回退旧摘要优先检索，且不影响 Tool 契约和审计。
    """

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """在当前用户边界内按最终文件名、分类和持久化摘要检索。"""

        if db is None or user_id is None:
            log_event(
                "retrieval.request.rejected",
                level="ERROR",
                tool_name="hybrid-search",
                status="FAILED",
                error_code="RUNTIME_CONTEXT_REQUIRED",
                message="文件检索缺少数据库或用户运行上下文",
            )
            return {
                "kind": "workspace_file_search",
                "ok": False,
                "query": getattr(tool_input, "query"),
                "results": [],
                "error": {"code": "RUNTIME_CONTEXT_REQUIRED", "message": "检索上下文不可用"},
            }

        settings = get_settings()
        # 新旧检索实现都必须使用唯一共享工作区作为读取权限边界；
        # Document.user_id 仅记录导入审计，不能阻止其他用户查找共享文件。
        from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id

        workspace_id = get_shared_workspace_id(db)
        search_query = str(getattr(tool_input, "query") or "")
        query_fingerprint = hashlib.sha256(
            search_query.strip().lower().encode("utf-8")
        ).hexdigest()[:12]
        requested_document_ids = list(
            getattr(tool_input, "document_ids", []) or []
        )
        log_event(
            "retrieval.request.started",
            tool_name="hybrid-search",
            status="RUNNING",
            workspace_id=workspace_id,
            query_fingerprint=query_fingerprint,
            query_chars=len(search_query),
            requested_document_count=len(requested_document_ids),
            two_stage_enabled=bool(settings.two_stage_retrieval_enabled),
            message="文件检索请求进入受控 Tool",
        )
        # 上传消息引用的是暂存 Document；正式检索必须先映射为共享活动工作副本
        # Document。尚未完成导入的 ID 只交给内部就绪协调，不能扩大到全库检索。
        from app.modules.retrieval.readiness import (
            WorkingCopySearchReadinessService,
        )

        readiness = WorkingCopySearchReadinessService(
            db=db,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        canonical_scope = readiness.canonicalize_document_ids(
            requested_document_ids
        )
        explicit_document_ids = list(canonical_scope.document_ids)
        canonical_scope_condition = {
            "label": "文件范围",
            "value": (
                f"后端已确认的 {len(explicit_document_ids)} 个活动文件"
                if requested_document_ids
                else "当前共享工作区全部活动文件"
            ),
            "condition_type": "scope",
            "status": (
                "APPLIED"
                if not requested_document_ids
                or len(explicit_document_ids) == len(requested_document_ids)
                else "RELAXED"
            ),
            "source": "backend",
        }
        log_event(
            "retrieval.attachment_scope.canonicalized",
            tool_name="hybrid-search",
            status="COMPLETED",
            workspace_id=workspace_id,
            query_fingerprint=query_fingerprint,
            requested_document_count=len(requested_document_ids),
            active_document_count=len(explicit_document_ids),
            unresolved_document_count=len(
                canonical_scope.unresolved_document_ids
            ),
            message="检索附件范围映射完成",
        )
        # 普通召回始终排除回收站；仅当用户明确写出完整文件名且没有活动同名副本时，
        # 才返回待选择的恢复候选。候选不能按版本或哈希自动合并。
        from app.modules.file_lifecycle.trash_lookup import ExactTrashFilenameLookupService

        trash_restore_selection = ExactTrashFilenameLookupService(
            db=db,
            user_id=user_id,
            workspace_id=workspace_id,
        ).lookup(query=search_query)
        log_event(
            "retrieval.trash_lookup.completed",
            tool_name="hybrid-search",
            status="COMPLETED",
            workspace_id=workspace_id,
            query_fingerprint=query_fingerprint,
            restore_selection_found=bool(trash_restore_selection),
            message="回收站精确文件名检查完成",
        )
        if trash_restore_selection:
            log_event(
                "retrieval.request.completed",
                tool_name="hybrid-search",
                status="NEEDS_REVIEW",
                workspace_id=workspace_id,
                query_fingerprint=query_fingerprint,
                result_count=0,
                restore_selection_found=True,
                message="文件检索请求转入回收站恢复选择",
            )
            return {
                "kind": "workspace_file_search",
                "ok": True,
                "query": search_query,
                "total_returned": 0,
                "partial": False,
                "results": [],
                "trash_restore_selection": trash_restore_selection,
                "user_message": str(trash_restore_selection["message"]),
                "_effective_scope": canonical_scope_condition,
            }
        log_event(
            "retrieval.route.selected",
            tool_name="hybrid-search",
            status="COMPLETED",
            workspace_id=workspace_id,
            query_fingerprint=query_fingerprint,
            retrieval_mode=(
                "two_stage"
                if settings.two_stage_retrieval_enabled
                else "summary_fallback"
            ),
            query_chars=len(search_query),
            explicit_document_count=len(explicit_document_ids),
            message="文件检索路由选择完成",
        )
        if not settings.two_stage_retrieval_enabled:
            # 紧急回退只更换检索算法，不能退回按导入用户隔离的旧权限模型。
            from app.modules.chunks.tokenizer import (
                ChineseLexicalTokenizer,
                load_default_business_terms,
            )
            from app.modules.retrieval.phrase_strategy import (
                mark_metadata_results_as_possible,
            )
            from app.modules.retrieval.query_parser import FileSearchQueryParser
            from app.modules.retrieval.scope_resolver import (
                ConversationFileSearchContextService,
                FileSearchScopeResolver,
            )
            from app.modules.retrieval.completeness import SearchCompletenessService

            # 紧急摘要回退也必须报告真实范围与索引缺口，不能因为换了算法就把
            # “结果为空”误报为“文件已找全”。
            fallback_scope = FileSearchScopeResolver(
                session_file_service=ConversationFileSearchContextService(
                    db=db,
                    user_id=user_id,
                ),
            ).resolve(
                query=search_query,
                explicit_attachment_ids=explicit_document_ids,
                conversation_id=(
                    conversation_id_getter() if conversation_id_getter else None
                ),
            )

            fallback_parsed = FileSearchQueryParser(
                tokenizer=ChineseLexicalTokenizer(load_default_business_terms())
            ).parse(search_query)
            result = WorkingCopySummarySearchService(
                db=db,
                user_id=user_id,
                workspace_id=workspace_id,
            ).search(
                query=search_query,
                document_ids=explicit_document_ids,
            )
            # 摘要降级不能将“涉及”类查询表述为原文已确认；只保留候选发现能力。
            result = mark_metadata_results_as_possible(
                result=result,
                parsed_query=fallback_parsed,
            )
            log_event(
                "retrieval.summary_fallback.completed",
                level="WARNING" if not result.get("results") else "INFO",
                tool_name="hybrid-search",
                status="COMPLETED",
                workspace_id=workspace_id,
                query_fingerprint=query_fingerprint,
                result_count=len(list(result.get("results") or [])),
                message="摘要回退检索完成",
            )
            if not result.get("results"):
                from app.modules.retrieval.query_parser import (
                    FileSearchQueryParser,
                )
                from app.modules.chunks.tokenizer import (
                    ChineseLexicalTokenizer,
                    load_default_business_terms,
                )

                prepared = readiness.prepare_after_miss(
                    parsed_query=FileSearchQueryParser(
                        tokenizer=ChineseLexicalTokenizer(
                            load_default_business_terms()
                        )
                    ).parse(search_query),
                    unresolved_document_ids=(
                        canonical_scope.unresolved_document_ids
                    ),
                )
                if prepared is not None:
                    log_event(
                        "retrieval.request.waiting",
                        tool_name="hybrid-search",
                        status="WAITING_FOR_ASYNC_JOB",
                        workspace_id=workspace_id,
                        query_fingerprint=query_fingerprint,
                        dependency_count=len(
                            list(prepared.get("job_ids") or [])
                        )
                        or (1 if prepared.get("job_id") else 0),
                        message="摘要回退未命中，检索进入静默准备等待",
                    )
                    return {
                        **prepared,
                        "_effective_scope": canonical_scope_condition,
                    }
            log_event(
                "retrieval.request.completed",
                tool_name="hybrid-search",
                status="COMPLETED",
                workspace_id=workspace_id,
                query_fingerprint=query_fingerprint,
                result_count=len(list(result.get("results") or [])),
                message="摘要回退文件检索请求完成",
            )
            result = SearchCompletenessService(
                db=db,
                workspace_id=workspace_id,
            ).attach_safely(
                result=result,
                scope=fallback_scope,
                unresolved_document_count=len(
                    canonical_scope.unresolved_document_ids
                ),
            )
            return {
                **result,
                "_effective_scope": canonical_scope_condition,
            }

        # 启用新链路：两阶段检索
        # 文件检索读取唯一共享工作目录；default workspace 只保留用户会话来源。
        from app.modules.retrieval.two_stage_search import TwoStageFileSearchService
        from app.modules.retrieval.query_parser import (
            FileSearchQueryParser,
            exact_short_chinese_phrase,
        )
        from app.modules.retrieval.scope_resolver import (
            ConversationFileSearchContextService,
            FileSearchScopeResolver,
        )

        # 构造查询解析器和范围解析器
        tokenizer = None
        try:
            from app.modules.chunks.tokenizer import (
                ChineseLexicalTokenizer,
                load_default_business_terms,
            )
            tokenizer = ChineseLexicalTokenizer(load_default_business_terms())
        except Exception as exc:
            tokenizer = None
            log_event(
                "retrieval.tokenizer.failed",
                level="ERROR",
                tool_name="hybrid-search",
                status="DEGRADED",
                workspace_id=workspace_id,
                query_fingerprint=query_fingerprint,
                error_code=exc.__class__.__name__,
                exception_traceback=format_exception_traceback(exc),
                message="中文检索分词器初始化失败",
            )

        parser = FileSearchQueryParser(tokenizer=tokenizer)
        parsed = parser.parse(search_query)
        log_event(
            "retrieval.query.parsed",
            level="WARNING" if not parsed.cleaned else "INFO",
            tool_name="hybrid-search",
            status="COMPLETED" if parsed.cleaned else "EMPTY",
            workspace_id=workspace_id,
            query_fingerprint=query_fingerprint,
            query_chars=len(search_query),
            cleaned_query_chars=len(parsed.cleaned),
            query_term_count=len(parsed.terms),
            exact_short_phrase_mode=exact_short_chinese_phrase(parsed.cleaned) is not None,
            relation_mode=parsed.relation_mode,
            required_topic_count=len(
                list(getattr(parsed, "required_topic_terms", []) or [])
            ),
            supporting_topic_count=len(
                list(getattr(parsed, "supporting_topic_terms", []) or [])
            ),
            year=parsed.year,
            has_year=parsed.year is not None or parsed.relative_year is not None,
            has_doc_number=parsed.doc_number is not None,
            message="文件检索查询解析完成",
        )

        conversation_id = conversation_id_getter() if conversation_id_getter else None
        resolver = FileSearchScopeResolver(
            session_file_service=ConversationFileSearchContextService(
                db=db,
                user_id=user_id,
            ),
        )
        scope = resolver.resolve(
            query=search_query,
            explicit_attachment_ids=explicit_document_ids,
            conversation_id=conversation_id,
        )
        resolved_scope_condition = _resolved_search_scope_condition(scope)
        log_event(
            "retrieval.scope.resolved",
            tool_name="hybrid-search",
            status="COMPLETED",
            workspace_id=workspace_id,
            query_fingerprint=query_fingerprint,
            scope_mode=str(getattr(scope, "scope_mode", "global") or "global"),
            strict_document_count=len(
                list(getattr(scope, "strict_document_ids", ()) or ())
            ),
            conversation_document_count=len(
                list(getattr(scope, "conversation_document_ids", ()) or ())
            ),
            include_workspace=bool(getattr(scope, "include_workspace", True)),
            message="文件检索范围解析完成",
        )

        service = TwoStageFileSearchService(
            db=db, user_id=user_id, workspace_id=workspace_id,
            config=settings, tokenizer=tokenizer,
        )
        try:
            # PostgreSQL 中任一检索 SQL 失败都会把当前事务标记为 aborted。
            # 主检索必须运行在 savepoint 内，才能在回滚后继续写 ToolInvocation、
            # AgentRun 和降级结果，不能让可重建索引故障污染业务审计事务。
            with db.begin_nested():
                result = _execute_controlled_file_search(
                    db=db,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    agent_run_id=(
                        agent_run_id_getter() if agent_run_id_getter else None
                    ),
                    tool_input=tool_input,
                    search_query=search_query,
                    parsed=parsed,
                    scope=scope,
                    tokenizer=tokenizer,
                    search_service=service,
                )
        except Exception as exc:
            # 两阶段索引属于可重建派生能力。迁移未完成、扩展不可用或 SQL 超时时，
            # 必须在已回滚的 savepoint 之后降级，不能让 PostgreSQL aborted 状态
            # 继续污染 ToolInvocation 和 AgentRun 审计写入。
            log_event(
                "retrieval.two_stage.failed",
                level="ERROR",
                tool_name="hybrid-search",
                status="DEGRADED",
                workspace_id=workspace_id,
                query_fingerprint=query_fingerprint,
                error_code=exc.__class__.__name__,
                exception_traceback=format_exception_traceback(exc),
                message="两阶段检索失败，尝试摘要级安全回退",
            )
            try:
                with db.begin_nested():
                    result = WorkingCopySummarySearchService(
                        db=db,
                        user_id=user_id,
                        workspace_id=workspace_id,
                    ).search(
                        query=search_query,
                        document_ids=explicit_document_ids,
                    )
                    from app.modules.retrieval.phrase_strategy import (
                        mark_metadata_results_as_possible,
                    )

                    result = mark_metadata_results_as_possible(
                        result=result,
                        parsed_query=parsed,
                    )
                result["partial"] = True
                result["user_message"] = (
                    result.get("user_message")
                    or "正文索引暂不可用，当前结果来自文件名和摘要检索。"
                )
                log_event(
                    "retrieval.summary_fallback.completed",
                    level="WARNING",
                    tool_name="hybrid-search",
                    status="DEGRADED",
                    workspace_id=workspace_id,
                    query_fingerprint=query_fingerprint,
                    result_count=len(list(result.get("results") or [])),
                    message="两阶段失败后的摘要级安全回退完成",
                )
            except Exception as fallback_exc:
                log_event(
                    "retrieval.summary_fallback.failed",
                    level="ERROR",
                    tool_name="hybrid-search",
                    status="FAILED",
                    workspace_id=workspace_id,
                    query_fingerprint=query_fingerprint,
                    error_code=fallback_exc.__class__.__name__,
                    exception_traceback=format_exception_traceback(fallback_exc),
                    message="摘要级检索回退失败",
                )
                log_event(
                    "retrieval.request.completed",
                    level="ERROR",
                    tool_name="hybrid-search",
                    status="FAILED",
                    workspace_id=workspace_id,
                    query_fingerprint=query_fingerprint,
                    result_count=0,
                    error_code="SEARCH_ENGINE_UNAVAILABLE",
                    message="文件检索主链路和安全回退均失败",
                )
                return {
                    "kind": "workspace_file_search",
                    "ok": False,
                    "query": search_query,
                    "total_returned": 0,
                    "partial": True,
                    "results": [],
                    "error": {
                        "code": "SEARCH_ENGINE_UNAVAILABLE",
                        "message": "文件检索暂时不可用，请稍后重试。",
                    },
                    "user_message": "文件检索暂时不可用，请稍后重试。",
                    "_effective_scope": resolved_scope_condition,
                }
        result = _require_large_search_result_confirmation(
            db=db,
            user_id=user_id,
            conversation_id=conversation_id,
            agent_run_id=(
                agent_run_id_getter() if agent_run_id_getter else None
            ),
            search_query=search_query,
            core_phrase=str(parsed.cleaned or "").strip(),
            result=result,
            show_all_results=bool(
                getattr(tool_input, "show_all_results", False)
            ),
        )
        result["kind"] = "workspace_file_search"
        # “是否找全”只能由活动工作副本及其当前索引版本的真实状态得出，不能交给
        # Planner 或模型概括。即使检索结果为空，也必须把未就绪和候选上限说明出来。
        from app.modules.retrieval.completeness import SearchCompletenessService

        result = SearchCompletenessService(
            db=db,
            workspace_id=workspace_id,
        ).attach_safely(
            result=result,
            scope=scope,
            unresolved_document_count=len(canonical_scope.unresolved_document_ids),
        )
        # 只有最终已返回的相关文件才进入集合；它们的源侧物化完全异步，不能推迟
        # 本次搜索回执。用户可见的“可能相关”同样属于最终结果，会被一并物化，
        # 但内部扩大召回候选从未进入此处，不能触发批量复制。
        if result.get("ok") and list(result.get("results") or []):
            from app.modules.retrieval.relevant_file_sets import RelevantFileSetService

            materialization = RelevantFileSetService(db=db, settings=settings).persist_and_enqueue(
                workspace_id=workspace_id,
                user_id=user_id,
                conversation_id=conversation_id,
                agent_run_id=(agent_run_id_getter() if agent_run_id_getter else None),
                query=search_query,
                results=list(result.get("results") or []),
            )
            if materialization is not None:
                result["relevant_file_set_id"] = materialization["relevant_file_set_id"]
        log_event(
            "retrieval.active_scope.completed",
            level="WARNING" if result.get("partial") else "INFO",
            tool_name="hybrid-search",
            status="COMPLETED" if result.get("ok") else "FAILED",
            workspace_id=workspace_id,
            query_fingerprint=query_fingerprint,
            result_count=len(list(result.get("results") or [])),
            partial=bool(result.get("partial")),
            clarification_required=bool(result.get("search_clarification")),
            restore_selection_found=bool(result.get("trash_restore_selection")),
            completeness_status=(
                (result.get("search_completeness") or {}).get("status")
                if isinstance(result.get("search_completeness"), dict)
                else None
            ),
            message="活动工作副本检索完成",
        )
        # managed_files 只用于内部发现。只有普通工作副本检索确实未命中时才
        # 静默准备候选；准备期间不把受管文件名称、路径或内部状态投影给用户。
        if (
            result.get("ok")
            and not result.get("results")
            and not result.get("search_clarification")
            and not result.get("trash_restore_selection")
        ):
            prepared = readiness.prepare_after_miss(
                parsed_query=parsed,
                unresolved_document_ids=canonical_scope.unresolved_document_ids,
            )
            if prepared is not None:
                log_event(
                    "retrieval.request.waiting",
                    tool_name="hybrid-search",
                    status="WAITING_FOR_ASYNC_JOB",
                    workspace_id=workspace_id,
                    query_fingerprint=query_fingerprint,
                    dependency_count=len(
                        list(prepared.get("job_ids") or [])
                    )
                    or (1 if prepared.get("job_id") else 0),
                    message="活动范围未命中，检索进入静默准备等待",
                )
                return {
                    **prepared,
                    "_effective_scope": resolved_scope_condition,
                }
        log_event(
            "retrieval.request.completed",
            level="WARNING" if not result.get("ok") else "INFO",
            tool_name="hybrid-search",
            status="COMPLETED" if result.get("ok") else "FAILED",
            workspace_id=workspace_id,
            query_fingerprint=query_fingerprint,
            result_count=len(list(result.get("results") or [])),
            message="文件检索请求完成",
        )
        return {
            **result,
            "_effective_scope": resolved_scope_condition,
        }

    return handler


def _with_search_binding_projection(handler: ToolHandler) -> ToolHandler:
    """为检索结果补充绑定 ID、实际条件和后续 Planner 安全观察。

    投影只读取后端检索结果中的稳定 ID；澄清、异步等待和未命中结果不会凭空生成文件范围。
    """

    def projected(tool_input: BaseModel) -> Dict[str, Any]:
        """执行真实检索后生成严格、有限的文件 ID 投影。"""

        result = handler(tool_input)
        effective_scope = (
            result.pop("_effective_scope", None)
            if isinstance(result.get("_effective_scope"), dict)
            else None
        )
        document_ids: list[str] = []
        seen: set[str] = set()
        for item in list(result.get("results") or []):
            if not isinstance(item, dict):
                continue
            # “可能相关”仅用于用户浏览候选，不能成为多轮 Planner 后续
            # evidence-answer/read Tool 的授权文件范围；否则摘要或泛词命中会
            # 被错误提升为可回答的文件事实。
            if str(item.get("relevance_tier") or "") == "POSSIBLE":
                continue
            document_id = str(item.get("document_id") or "")
            if document_id and document_id not in seen:
                seen.add(document_id)
                document_ids.append(document_id)
            # 与 EvidenceAnswerInput 的文件范围上限一致，避免绑定后才因数组过大失败。
            if len(document_ids) >= 50:
                break
        result_status, index_status, next_actions = _search_result_status(result)
        return {
            **result,
            "document_ids": document_ids,
            "total_returned": int(
                result.get("total_returned")
                if result.get("total_returned") is not None
                else len(document_ids)
            ),
            "effective_conditions": _effective_search_conditions(
                tool_input=tool_input,
                effective_scope=effective_scope,
            ),
            "index_status": index_status,
            "result_status": result_status,
            "available_next_actions": next_actions,
        }

    return projected


def _search_result_status(
    result: Dict[str, Any],
) -> tuple[str, str, list[str]]:
    """把搜索业务结果收敛为 Planner 可判断的有限状态。"""

    if result.get("kind") == "filesystem_job":
        return "INDEX_PENDING", "INDEX_PENDING", ["WAIT_FOR_INDEX"]
    if isinstance(result.get("search_clarification"), dict):
        return "NEEDS_CLARIFICATION", "READY", ["WAIT_FOR_USER"]
    if isinstance(result.get("trash_restore_selection"), dict):
        return "NEEDS_CONFIRMATION", "READY", ["WAIT_FOR_USER"]
    if list(result.get("query_corrections") or []):
        return "NEEDS_CLARIFICATION", "READY", ["WAIT_FOR_USER", "REFINE_SEARCH"]
    if result.get("ok") is False:
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        code = str(error.get("code") or "SEARCH_FAILED")
        return code, "SEARCH_ENGINE_UNAVAILABLE", ["STOP_WITH_ERROR"]
    if list(result.get("results") or []):
        if int(result.get("supported_count") or 0) == 0 and int(
            result.get("possible_count") or 0
        ) > 0:
            return (
                "POSSIBLE_ONLY",
                "PARTIAL_INDEX" if result.get("partial") else "READY",
                ["FINISH_WITH_CANDIDATES", "REFINE_SEARCH", "CLARIFY"],
            )
        return (
            "MATCHED",
            "PARTIAL_INDEX" if result.get("partial") else "READY",
            [
                "FINISH_WITH_RESULTS",
                "READ_MATCHED_DOCUMENTS",
                "REFINE_SEARCH",
            ],
        )
    completeness = (
        result.get("search_completeness")
        if isinstance(result.get("search_completeness"), dict)
        else {}
    )
    if str(completeness.get("status") or "") == "PROCESSING":
        return "INDEX_PENDING", "INDEX_PENDING", ["WAIT_FOR_INDEX", "REFINE_SEARCH"]
    if int(completeness.get("failed_file_count") or 0) > 0:
        return "INDEX_FAILED", "INDEX_FAILED", ["RETRY_INDEX", "REFINE_SEARCH"]
    if str(completeness.get("status") or "") in {"PARTIAL", "UNVERIFIABLE"}:
        return "ZERO_RESULTS", "PARTIAL_INDEX", ["REFINE_SEARCH", "CLARIFY"]
    if str(completeness.get("status") or "") == "COMPLETE":
        return (
            "NO_MATCHING_EVIDENCE",
            "READY",
            ["REFINE_SEARCH", "CLARIFY"],
        )
    return (
        "ZERO_RESULTS",
        "PARTIAL_INDEX" if result.get("partial") else "READY",
        ["REFINE_SEARCH", "CLARIFY"],
    )


def _effective_search_conditions(
    *,
    tool_input: BaseModel,
    effective_scope: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """根据真实 Tool 输入生成后端确认的查询条件，不把 LLM 自报条件伪装为硬过滤。"""

    query = str(getattr(tool_input, "query", "") or "").strip()
    match_mode = str(getattr(tool_input, "match_mode", "AUTO") or "AUTO")
    document_ids = list(getattr(tool_input, "document_ids", []) or [])
    phrases = [
        str(item).strip()
        for item in list(getattr(tool_input, "phrases", []) or [])
        if str(item).strip()
    ]
    conditions: list[dict[str, str]] = [
        {
            "label": "检索内容",
            "value": query,
            "condition_type": "semantic",
            "status": "APPLIED",
            "source": "backend",
        },
        effective_scope
        or {
            "label": "文件范围",
            "value": (
                f"Planner 请求的 {len(document_ids)} 个文件，等待后端范围确认"
                if document_ids
                else "当前共享工作区全部活动文件"
            ),
            "condition_type": "scope",
            "status": "APPLIED" if not document_ids else "SEMANTIC_ONLY",
            "source": "backend",
        },
        {
            "label": "匹配方式",
            "value": match_mode,
            "condition_type": "relation",
            "status": "APPLIED",
            "source": "backend",
        },
    ]
    if phrases:
        conditions.append(
            {
                "label": "受控短语",
                "value": "、".join(phrases),
                "condition_type": "semantic",
                "status": "APPLIED",
                "source": "backend",
            }
        )
    normalized_query = re.sub(r"\s+", "", query).casefold()
    normalized_phrases = {
        re.sub(r"\s+", "", value).casefold() for value in phrases
    }
    for raw in list(getattr(tool_input, "interpreted_conditions", []) or []):
        item = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw)
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        normalized = re.sub(r"\s+", "", value).casefold()
        applied = bool(
            normalized
            and (
                normalized in normalized_query
                or normalized in normalized_phrases
            )
        )
        conditions.append(
            {
                "label": str(item.get("label") or "查询条件")[:40],
                "value": value[:300],
                "condition_type": str(
                    item.get("condition_type") or "semantic"
                ),
                "status": "SEMANTIC_ONLY" if applied else "UNSUPPORTED",
                "source": "user_and_llm",
            }
        )
    return conditions[:30]


def _resolved_search_scope_condition(scope: Any) -> dict[str, str]:
    """把后端范围解析结果转换为不含 document_id 的实际范围说明。"""

    scope_mode = str(getattr(scope, "scope_mode", "global") or "global")
    strict_count = len(list(getattr(scope, "strict_document_ids", ()) or ()))
    conversation_count = len(
        list(getattr(scope, "conversation_document_ids", ()) or ())
    )
    include_workspace = bool(getattr(scope, "include_workspace", True))
    if strict_count:
        value = f"后端唯一确认的 {strict_count} 个文件"
    elif scope_mode == "conversation" and conversation_count:
        value = f"当前对话已确认的 {conversation_count} 个文件"
    elif include_workspace:
        value = "当前共享工作区全部活动文件"
    else:
        value = "当前对话可访问文件"
    return {
        "label": "文件范围",
        "value": value,
        "condition_type": "scope",
        "status": "APPLIED",
        "source": "backend",
    }


def _require_large_search_result_confirmation(
    *,
    db: Any,
    user_id: str,
    conversation_id: str | None,
    agent_run_id: str | None,
    search_query: str,
    core_phrase: str,
    result: Dict[str, Any],
    show_all_results: bool,
) -> Dict[str, Any]:
    """结果超过页面直接展示阈值时，先持久化“全部展示”确认。

    这里不截断真实检索结果。用户确认后用原查询重新执行，并通过受校验的
    ``show_all_results`` 标记跳过本提示；没有真实会话时无法建立对话确认，
    因此保持完整内部结果。
    """

    files = [
        item for item in result.get("results", []) if isinstance(item, dict)
    ]
    if show_all_results:
        return {**result, "show_all_results": True}
    if (
        len(files) <= SEARCH_RESULT_CONFIRMATION_THRESHOLD
        or not conversation_id
        or isinstance(result.get("search_clarification"), dict)
    ):
        return result

    from app.modules.retrieval.clarification_service import (
        FileSearchClarificationService,
    )

    record = FileSearchClarificationService(db).create(
        conversation_id=conversation_id,
        user_id=user_id,
        agent_run_id=agent_run_id,
        original_query=search_query,
        core_phrase=core_phrase,
        relation_mode="RESULT_LIMIT_CONFIRMATION",
        options=[
            {
                "id": "show-all-results",
                "label": "全部展示",
                "description": f"展示本次找到的全部 {len(files)} 个相关文件。",
                "examples": [],
                "estimated_count": len(files),
            }
        ],
    )
    public = FileSearchClarificationService.public_payload(record)
    return {
        "ok": True,
        "kind": "workspace_file_search",
        "query": search_query,
        "total_returned": len(files),
        "partial": bool(result.get("partial", False)),
        "results": [],
        "search_clarification": public,
        "user_message": str(public["prompt"]),
    }


def _metadata_contains_search_phrase(item: Dict[str, Any], phrase: str) -> bool:
    """确认文件级公开字段连续包含完整主题短语，不接受拆词 OR 分数。"""

    needle = re.sub(r"\s+", "", str(phrase or "").lower())
    if not needle:
        return False
    values = [
        str(item.get("filename") or ""),
        str(item.get("overview") or ""),
        " ".join(str(value) for value in item.get("category_path", []) if value),
    ]
    return any(
        needle in re.sub(r"\s+", "", value.lower())
        for value in values
    )


def _intersect_file_search_results(
    *,
    original_query: str,
    entity_result: Dict[str, Any],
    topic_result: Dict[str, Any],
    topic_phrase: str,
) -> Dict[str, Any]:
    """对“机构实体”和“文件主题”分别召回后取稳定文件交集。

    机构简称可以等价扩展，但主题仍必须独立命中。这样既允许文件名在机构和主题
    之间包含年份等字段，也不会退化成“机构 OR 主题”的宽泛查询。
    """

    entity_items = [
        item for item in entity_result.get("results", []) if isinstance(item, dict)
    ]
    entity_by_id = {
        str(
            item.get("working_copy_id")
            or item.get("managed_file_revision_id")
            or item.get("document_version_id")
            or item.get("document_id")
            or ""
        ): item
        for item in entity_items
        if (
            item.get("working_copy_id")
            or item.get("document_version_id")
            or item.get("document_id")
        )
    }
    topic_items = [
        item for item in topic_result.get("results", []) if isinstance(item, dict)
    ]
    topic_ids = _search_result_ids(topic_result)
    # 主题全局召回可能先达到候选上限。机构候选自身的文件名、摘要或分类已经连续
    # 包含完整主题时，也构成确定性交集证据，不能因为主题候选被截断而误删目标。
    results = [
        item
        for item in topic_items
        if str(
            item.get("working_copy_id")
            or item.get("document_version_id")
            or item.get("document_id")
            or ""
        )
        in entity_by_id
    ]
    results.extend(
        item
        for item_id, item in entity_by_id.items()
        if item_id not in topic_ids
        and _metadata_contains_search_phrase(item, topic_phrase)
    )
    partial = bool(entity_result.get("partial") or topic_result.get("partial"))
    candidate_limit_reached = bool(
        entity_result.get("candidate_limit_reached")
        or topic_result.get("candidate_limit_reached")
    )
    log_event(
        "retrieval.strategy.intersection_completed",
        level="WARNING" if partial or not results else "INFO",
        tool_name="hybrid-search",
        status="DEGRADED" if partial else "COMPLETED",
        entity_result_count=len(entity_by_id),
        topic_result_count=len(topic_ids),
        intersection_result_count=len(results),
        partial=partial,
        message="机构实体与文件主题交集计算完成",
    )
    return {
        "ok": True,
        "kind": "workspace_file_search",
        "query": original_query,
        "total_returned": len(results),
        "partial": partial,
        "candidate_limit_reached": candidate_limit_reached,
        "results": results,
        "user_message": (
            f"找到 {len(results)} 个相关文件。"
            if results
            else (
                "暂未找到相关文件，部分正文索引当前不可用。"
                if partial
                else "未找到相关文件。"
            )
        ),
    }


def _execute_controlled_file_search(
    *,
    db: Any,
    user_id: str,
    conversation_id: str | None,
    agent_run_id: str | None,
    tool_input: BaseModel,
    search_query: str,
    parsed: Any,
    scope: Any,
    tokenizer: Any,
    search_service: Any,
) -> Dict[str, Any]:
    """根据查询关系模式执行精确、同义或待选择检索。

    普通查询的同义扩展只能来自版本化词典。只有服务端续跑 Planner 才能提供显式 phrases；
    AUTO 模式下任何歧义都必须先持久化选择记录。
    """

    from app.modules.retrieval.clarification_service import (
        FileSearchClarificationService,
    )
    from app.modules.retrieval.phrase_strategy import (
        FileSearchPhraseStrategyService,
    )
    from app.modules.retrieval.synonym_service import (
        FileSearchSynonymService,
        expand_scope_entity_phrases,
        split_entity_topic_phrase,
    )

    strategy = FileSearchPhraseStrategyService(
        search_service=search_service,
        tokenizer=tokenizer,
    )
    explicit_mode = str(getattr(tool_input, "match_mode", "AUTO") or "AUTO")
    explicit_phrases = list(getattr(tool_input, "phrases", []) or [])
    if explicit_mode != "AUTO" and explicit_phrases:
        log_event(
            "retrieval.strategy.selected",
            tool_name="hybrid-search",
            status="COMPLETED",
            strategy="confirmed_phrase_scope",
            match_mode=explicit_mode,
            phrase_count=len(explicit_phrases),
            require_body_evidence=bool(
                getattr(tool_input, "require_body_evidence", False)
            ),
            message="采用用户已确认的短语范围执行检索",
        )
        return strategy.search(
            original_query=search_query,
            parsed_query=parsed,
            scope=scope,
            phrases=explicit_phrases,
            require_body_evidence=bool(
                getattr(tool_input, "require_body_evidence", False)
            ),
        )

    core_phrase = str(parsed.cleaned or "").strip()
    synonym_service = FileSearchSynonymService()
    relation_mode = str(getattr(parsed, "relation_mode", "UNSPECIFIED"))
    required_topic_terms = list(
        getattr(parsed, "required_topic_terms", []) or []
    )
    supporting_topic_terms = list(
        getattr(parsed, "supporting_topic_terms", []) or []
    )
    from app.modules.retrieval.event_collection import (
        EventCollectionSearchService,
        resolve_event_collection_request,
    )

    event_collection_request = resolve_event_collection_request(parsed)
    if event_collection_request is not None:
        log_event(
            "retrieval.strategy.selected",
            tool_name="hybrid-search",
            status="COMPLETED",
            strategy="verified_event_collection",
            relation_mode=relation_mode,
            action_phrase_count=len(event_collection_request.action_phrases),
            message="采用年月事件锚点与同目录配套材料检索",
        )
        return EventCollectionSearchService(
            db=db,
            workspace_id=str(search_service.workspace_id),
            phrase_strategy=strategy,
            stage1_service=search_service.stage1,
        ).search(
            original_query=search_query,
            parsed_query=parsed,
            scope=scope,
            request=event_collection_request,
        )
    fact_anchors = list(getattr(parsed, "fact_anchor_phrases", []) or [])
    if (
        bool(getattr(parsed, "is_fact_question", False))
        and fact_anchors
        and relation_mode != "LITERAL"
    ):
        log_event(
            "retrieval.strategy.selected",
            tool_name="hybrid-search",
            status="COMPLETED",
            strategy="verified_fact_anchors",
            relation_mode=relation_mode,
            fact_anchor_count=len(fact_anchors),
            requested_field_count=len(
                list(getattr(parsed, "requested_fact_fields", []) or [])
            ),
            message="采用事实锚点与待回答字段分离检索",
        )
        fact_result = strategy.search_fact_anchors(
            original_query=search_query,
            parsed_query=parsed,
            scope=scope,
            anchors=fact_anchors,
            requested_fields=list(
                getattr(parsed, "requested_fact_fields", []) or []
            ),
        )
        if int(fact_result.get("supported_count") or 0) == 0:
            from app.modules.retrieval.entity_correction import (
                FactEntityCorrectionService,
                attach_entity_corrections,
            )

            try:
                corrections = FactEntityCorrectionService(
                    db=db,
                    workspace_id=str(search_service.workspace_id),
                ).suggest(
                    entity_phrases=list(
                        getattr(parsed, "fact_entity_phrases", []) or []
                    )
                )
            except Exception as exc:
                # 纠错提示只是空结果后的辅助信息。历史投影缺列、SQL 超时或
                # 候选服务异常都必须保留原始检索结果，不能升级成 HTTP 500。
                corrections = []
                log_event(
                    "retrieval.fact_entity_correction.failed",
                    level="WARNING",
                    tool_name="hybrid-search",
                    status="DEGRADED",
                    error_code=exc.__class__.__name__,
                    message="事实人名纠错候选不可用，保留原始检索结果",
                )
            fact_result = attach_entity_corrections(
                result=fact_result,
                corrections=corrections,
            )
        return fact_result
    if relation_mode == "LITERAL" and required_topic_terms and supporting_topic_terms:
        # “涉及劳务费发放”一类问题既要求核心业务主题，也包含容易泛化的
        # 工作动作。由 Tool 用受控正文检索验证两者，不能由 LLM 或文件名
        # 单独断言“涉及”。
        log_event(
            "retrieval.strategy.selected",
            tool_name="hybrid-search",
            status="COMPLETED",
            strategy="topic_tiered_literal_search",
            relation_mode=relation_mode,
            required_topic_count=len(required_topic_terms),
            supporting_topic_count=len(supporting_topic_terms),
            message="采用核心主题与宽泛动作词的正文分级检索",
        )
        return strategy.search_with_topic_tiers(
            original_query=search_query,
            parsed_query=parsed,
            scope=scope,
            exact_phrase=core_phrase,
            required_topic_terms=required_topic_terms,
            supporting_topic_terms=supporting_topic_terms,
        )
    equivalent_mention = synonym_service.find_equivalent_mention(core_phrase)
    if equivalent_mention is not None:
        group, matched_name = equivalent_mention
        if relation_mode != "LITERAL":
            # “计算机学院的工作总结”不是一个必须连续出现的标题短语：
            # 文件名可能是“计算机学院2025年工作总结”。机构与主题分别召回后
            # 取交集，机构允许正式简称等价，主题仍然必须命中。
            topic_phrase = core_phrase.replace(matched_name, " ", 1).strip()
            topic_phrase = re.sub(r"^[的与和及\s]+|[的与和及\s]+$", "", topic_phrase)
            log_event(
                "retrieval.strategy.selected",
                tool_name="hybrid-search",
                status="COMPLETED",
                strategy="equivalent_entity_topic_intersection",
                relation_mode=relation_mode,
                synonym_group_id=group.group_id,
                entity_phrase_count=len(group.phrases),
                has_topic_phrase=bool(topic_phrase),
                message="采用正式机构别名与主题交集检索",
            )
            entity_result = strategy.search(
                original_query=search_query,
                parsed_query=parsed,
                scope=scope,
                phrases=list(group.phrases),
                require_body_evidence=False,
                # 交集必须基于两侧完整文件集合计算，不能先各截取 30 份。
                unbounded_candidates=True,
            )
            if topic_phrase:
                topic_result = strategy.search(
                    original_query=search_query,
                    parsed_query=parsed,
                    scope=scope,
                    phrases=[topic_phrase],
                    require_body_evidence=False,
                    unbounded_candidates=True,
                )
                return _intersect_file_search_results(
                    original_query=search_query,
                    entity_result=entity_result,
                    topic_result=topic_result,
                    topic_phrase=topic_phrase,
                )
            return entity_result

        # 用户明确要求“正文提到完整短语”时继续执行连续短语匹配，只替换
        # 正式机构全称与简称，不能把实体和主题拆开。
        equivalent_phrases = synonym_service.expand_equivalent_mentions(core_phrase)
        log_event(
            "retrieval.strategy.selected",
            tool_name="hybrid-search",
            status="COMPLETED",
            strategy="literal_equivalent_phrase",
            relation_mode=relation_mode,
            synonym_group_id=group.group_id,
            phrase_count=len(equivalent_phrases),
            require_body_evidence=True,
            message="采用正式机构别名的正文连续短语检索",
        )
        return strategy.search(
            original_query=search_query,
            parsed_query=parsed,
            scope=scope,
            phrases=list(equivalent_phrases),
            require_body_evidence=True,
        )
    entity_topic = split_entity_topic_phrase(core_phrase)
    if entity_topic is not None and relation_mode != "LITERAL":
        entity_phrase, topic_phrase = entity_topic
        log_event(
            "retrieval.strategy.selected",
            tool_name="hybrid-search",
            status="COMPLETED",
            strategy="entity_topic_intersection",
            relation_mode=relation_mode,
            entity_phrase_count=len(expand_scope_entity_phrases(entity_phrase)),
            has_topic_phrase=True,
            message="采用机构范围与文件主题组合检索",
        )
        topic_result = strategy.search(
            original_query=search_query,
            parsed_query=parsed,
            scope=scope,
            phrases=[topic_phrase],
            require_body_evidence=False,
            # 只取消交集前候选文件上限；最终批量展示仍由确认门控制。
            unbounded_candidates=True,
        )
        entity_result = strategy.search(
            original_query=search_query,
            parsed_query=parsed,
            scope=scope,
            # 范围词和主题均需命中同一文件。“学校”不能再因 workspace 已限定
            # 而被丢弃，否则学校级查询会混入任意学院的同主题文件。
            phrases=list(expand_scope_entity_phrases(entity_phrase)),
            require_body_evidence=False,
            unbounded_candidates=True,
        )
        return _intersect_file_search_results(
            original_query=search_query,
            entity_result=entity_result,
            topic_result=topic_result,
            topic_phrase=topic_phrase,
        )
    group = synonym_service.find_group(core_phrase)
    expanded_phrases = (
        list(group.phrases) if group is not None else [core_phrase]
    )
    log_event(
        "retrieval.strategy.selected",
        tool_name="hybrid-search",
        status="COMPLETED",
        strategy=(
            "controlled_synonym"
            if group is not None
            else "exact_core_phrase"
        ),
        relation_mode=relation_mode,
        synonym_group_id=group.group_id if group is not None else None,
        phrase_count=len(expanded_phrases),
        message="文件检索短语策略已确定",
    )
    exact = strategy.search(
        original_query=search_query,
        parsed_query=parsed,
        scope=scope,
        phrases=[core_phrase],
        require_body_evidence=relation_mode == "LITERAL",
    )
    if group is None:
        return exact

    expanded = strategy.search(
        original_query=search_query,
        parsed_query=parsed,
        scope=scope,
        phrases=expanded_phrases,
        require_body_evidence=relation_mode == "LITERAL",
    )
    exact_ids = _search_result_ids(exact)
    expanded_ids = _search_result_ids(expanded)

    if relation_mode == "RELATED":
        return expanded
    if relation_mode == "LITERAL" and exact_ids:
        return exact
    if relation_mode == "LITERAL" and not expanded_ids:
        return exact
    if relation_mode == "UNSPECIFIED" and exact_ids == expanded_ids:
        return exact

    # 没有真实会话的单元测试或内部调用不能创建悬空选择记录，保持最窄精确结果。
    if not conversation_id:
        return exact

    options = _build_search_clarification_options(
        strategy=strategy,
        parsed=parsed,
        scope=scope,
        search_query=search_query,
        core_phrase=core_phrase,
        expanded_phrases=expanded_phrases,
        broad_topics=list(group.broad_topics),
        exact_count=len(exact_ids),
        expanded_count=len(expanded_ids),
        literal=relation_mode == "LITERAL",
    )
    record = FileSearchClarificationService(db).create(
        conversation_id=conversation_id,
        user_id=user_id,
        agent_run_id=agent_run_id,
        original_query=search_query,
        core_phrase=core_phrase,
        relation_mode=relation_mode,
        options=options,
    )
    public = FileSearchClarificationService.public_payload(record)
    return {
        "ok": True,
        "kind": "workspace_file_search",
        "query": search_query,
        "total_returned": 0,
        "partial": bool(exact.get("partial") or expanded.get("partial")),
        "results": [],
        "search_clarification": public,
        "user_message": (
            f"“{core_phrase}”存在不同的查找范围，请选择后继续。"
        ),
    }


def _build_search_clarification_options(
    *,
    strategy: Any,
    parsed: Any,
    scope: Any,
    search_query: str,
    core_phrase: str,
    expanded_phrases: list[str],
    broad_topics: list[str],
    exact_count: int,
    expanded_count: int,
    literal: bool,
) -> list[dict[str, Any]]:
    """构造服务端允许的选择项，并以有上限的预检结果提供数量提示。"""

    options: list[dict[str, Any]] = [
        {
            "id": "exact",
            "label": f"只查“{core_phrase}”",
            "description": "只返回连续出现该完整短语的文件。",
            "examples": [core_phrase],
            "estimated_count": exact_count,
            "phrases": [core_phrase],
            "match_mode": "LITERAL" if literal else "RELATED",
            "require_body_evidence": literal,
            "display_content": f"只按“{core_phrase}”继续查找",
        },
        {
            "id": "synonyms",
            "label": "包含相近表达",
            "description": "按完整同义短语扩大范围，不拆成任意词 OR。",
            "examples": expanded_phrases,
            "estimated_count": expanded_count,
            "phrases": expanded_phrases,
            "match_mode": "RELATED",
            "require_body_evidence": literal,
            "display_content": f"按“{core_phrase}”及相近表达继续查找",
        },
    ]
    for index, topic in enumerate(broad_topics[:2], start=1):
        broad_result = strategy.search(
            original_query=search_query,
            parsed_query=parsed,
            scope=scope,
            phrases=[topic],
            require_body_evidence=False,
        )
        options.append(
            {
                "id": f"broad-{index}",
                "label": f"只查“{topic}”相关内容",
                "description": "这是更宽泛的主题范围，只有选择后才会执行。",
                "examples": [topic],
                "estimated_count": len(_search_result_ids(broad_result)),
                "phrases": [topic],
                "match_mode": "BROAD",
                "require_body_evidence": False,
                "display_content": f"只按“{topic}”主题继续查找",
            }
        )
    options.append(
        {
            "id": "custom",
            "label": "使用其他关键词",
            "description": "输入你希望连续匹配的其他短语。",
            "examples": [],
            "estimated_count": None,
            "phrases": [],
            "match_mode": "LITERAL",
            "require_body_evidence": True,
            "display_content": "使用自定义短语继续查找",
        }
    )
    return options


def _search_result_ids(payload: Dict[str, Any]) -> set[str]:
    """提取结果集合用于判断是否存在实质歧义。"""

    return {
        str(
            item.get("working_copy_id")
            or item.get("managed_file_revision_id")
            or item.get("document_version_id")
            or item.get("document_id")
            or ""
        )
        for item in payload.get("results", [])
        if isinstance(item, dict)
        and (
            item.get("working_copy_id")
            or item.get("managed_file_revision_id")
            or item.get("document_version_id")
            or item.get("document_id")
        )
    }


def _evidence_answer_handler(
    db: Any,
    user_id: str | None,
    conversation_id_getter: Callable[[], str | None] | None,
    agent_run_id_getter: Callable[[], str | None] | None,
) -> ToolHandler:
    """创建阶段五真实证据回答 handler，替换无证据占位实现。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """在当前用户、会话和 AgentRun 边界内执行证据回答闭环。"""

        conversation_id = conversation_id_getter() if conversation_id_getter else None
        agent_run_id = agent_run_id_getter() if agent_run_id_getter else None
        if db is None or not user_id or not conversation_id:
            return {
                "ok": False,
                "kind": "evidence_answer",
                "status": "FAILED",
                "answer": "证据回答缺少数据库、用户或会话上下文。",
                "references": [],
                "error": {
                    "code": "EVIDENCE_CONTEXT_UNAVAILABLE",
                    "message": "证据回答缺少数据库、用户或会话上下文。",
                },
            }
        return EvidenceAnswerService(
            db=db,
            user_id=user_id,
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
        ).answer(
            question=str(getattr(tool_input, "question")),
            document_ids=list(getattr(tool_input, "document_ids")),
            answer_mode=str(getattr(tool_input, "answer_mode")),
            document_selection_clarification_id=getattr(
                tool_input,
                "document_selection_clarification_id",
                None,
            ),
        )

    return handler


def _document_insights_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建读取 document_insights 的 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """按当前用户读取已持久化的文件基础洞察。"""

        document_ids = list(getattr(tool_input, "document_ids"))
        if db is None or user_id is None:
            return {
                "ok": True,
                "documents": [
                    {
                        "document_id": document_id,
                        "ingest_status": "UNKNOWN",
                        "keywords": [],
                        "labels": [],
                        "summary": "",
                    }
                    for document_id in document_ids
                ],
            }

        documents, shared_copy_by_document_id = _documents_visible_to_file_agent(
            db=db,
            user_id=user_id,
            document_ids=document_ids,
        )
        for document in documents:
            if document.id in shared_copy_by_document_id:
                continue
            lifecycle = FileExtractionRepository(db, user_id).resolve_original_file_for_document(document)
            if not lifecycle.get("ok") and (lifecycle.get("error") or {}).get("code") == "FILE_TRASHED":
                return _attach_trash_restore_selection(
                    result={
                        "ok": False,
                        "status": "FAILED",
                        "error": lifecycle.get("error") or {},
                    },
                    db=db,
                    user_id=user_id,
                    filename=str(document.original_filename or ""),
                )
        insights = {
            insight.document_id: insight
            for insight in (
                db.query(DocumentInsight)
                .filter(DocumentInsight.document_id.in_([document.id for document in documents]))
                .all()
                if documents
                else []
            )
        }
        return {
            "ok": True,
            "documents": [
                {
                    "document_id": document.id,
                    "filename": (
                        shared_copy_by_document_id[document.id].filename
                        if document.id in shared_copy_by_document_id
                        else document.original_filename
                    ),
                    "content_type": document.content_type,
                    "ingest_status": document.ingest_status,
                    "keywords": (insights.get(document.id).keywords_json if insights.get(document.id) else []),
                    "labels": (insights.get(document.id).labels_json if insights.get(document.id) else []),
                    "summary": (insights.get(document.id).summary if insights.get(document.id) else ""),
                }
                for document in documents
            ],
        }

    return handler


def _document_classifications_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建读取当前版本最新成功分类证据的 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """按当前用户读取文件最近一次分类建议。"""

        document_ids = list(getattr(tool_input, "document_ids"))
        if db is None or user_id is None:
            return {"ok": True, "documents": []}

        documents, shared_copy_by_document_id = _documents_visible_to_file_agent(
            db=db,
            user_id=user_id,
            document_ids=document_ids,
        )
        for document in documents:
            if document.id in shared_copy_by_document_id:
                continue
            lifecycle = FileExtractionRepository(db, user_id).resolve_original_file_for_document(document)
            if not lifecycle.get("ok") and (lifecycle.get("error") or {}).get("code") == "FILE_TRASHED":
                return _attach_trash_restore_selection(
                    result={
                        "ok": False,
                        "status": "FAILED",
                        "error": lifecycle.get("error") or {},
                    },
                    db=db,
                    user_id=user_id,
                    filename=str(document.original_filename or ""),
                )
        if not documents:
            return {"ok": True, "documents": []}
        shared_document_ids = [
            document.id
            for document in documents
            if document.id in shared_copy_by_document_id
        ]
        owned_document_ids = [
            document.id
            for document in documents
            if document.id not in shared_copy_by_document_id
        ]
        classification_by_document_id: dict[str, dict[str, Any]] = {}
        if shared_document_ids:
            shared_workspace_id = shared_copy_by_document_id[
                shared_document_ids[0]
            ].workspace_id
            for item in CurrentClassificationEvidenceReader(
                db=db,
                # 共享文件已由 _documents_visible_to_file_agent 按 ACTIVE 工作副本授权；
                # Reader 只固定唯一共享 workspace，不能按历史导入者再次隔离。
                user_id=None,
                workspace_id=shared_workspace_id,
            ).read(document_ids=shared_document_ids):
                classification_by_document_id[str(item["document_id"])] = item
        if owned_document_ids:
            for item in CurrentClassificationEvidenceReader(
                db=db,
                # 用户上传文件继续按 Document.user_id 授权，不能借共享范围放宽所有权。
                user_id=user_id,
            ).read(document_ids=owned_document_ids):
                classification_by_document_id[str(item["document_id"])] = item
        return {
            "ok": True,
            "version_scope": "CURRENT_WORKING_COPY",
            # 最终按输入顺序投影，避免混合共享/私有文件时分类卡与文件名错配。
            "documents": [
                classification_by_document_id[document.id]
                for document in documents
                if document.id in classification_by_document_id
            ],
        }

    return handler


def _documents_visible_to_file_agent(
    *,
    db: Any,
    user_id: str,
    document_ids: list[str],
) -> tuple[list[Document], dict[str, WorkingCopy]]:
    """解析只读 Tool 可访问的上传文件与共享活动工作副本。

    用户自己的上传 Document 仍按 ``Document.user_id`` 授权；系统导入后的正式文件
    则按唯一共享工作区和 ACTIVE 工作副本授权。返回顺序与 Tool 输入一致，避免
    自然语言多文件回答因数据库查询顺序变化而错配。
    """

    requested = list(dict.fromkeys(str(value) for value in document_ids if str(value)))
    if not requested:
        return [], {}
    from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id

    shared_workspace_id = get_shared_workspace_id(db)
    shared_copies = (
        db.query(WorkingCopy)
        .filter(
            WorkingCopy.workspace_id == shared_workspace_id,
            WorkingCopy.status == "ACTIVE",
            WorkingCopy.document_id.in_(requested),
        )
        .order_by(WorkingCopy.updated_at.desc(), WorkingCopy.id.desc())
        .all()
    )
    shared_copy_by_document_id: dict[str, WorkingCopy] = {}
    for working_copy in shared_copies:
        shared_copy_by_document_id.setdefault(
            str(working_copy.document_id),
            working_copy,
        )
    owned_ids = {
        str(value)
        for (value,) in (
            db.query(Document.id)
            .filter(
                Document.id.in_(requested),
                Document.user_id == user_id,
            )
            .all()
        )
    }
    authorized_ids = owned_ids | set(shared_copy_by_document_id)
    documents_by_id = {
        str(document.id): document
        for document in (
            db.query(Document)
            .filter(Document.id.in_(authorized_ids))
            .all()
        )
    }
    return (
        [
            documents_by_id[document_id]
            for document_id in requested
            if document_id in documents_by_id
        ],
        shared_copy_by_document_id,
    )


def _intent_summary_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """记录 LLM 已完成意图理解但不需要文件工具的结果。"""

    return {
        "ok": True,
        "intent": getattr(tool_input, "intent"),
        "user_goal": getattr(tool_input, "user_goal"),
    }


def _capability_suggestion_handler(
    db: Any,
    user_id: str | None,
    agent_run_id_getter: Callable[[], str | None] | None,
) -> ToolHandler:
    """创建内部能力建议记录 handler；该 Tool 不进入 Planner Catalog。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """经后端校验和去重后写入管理员建议清单。"""

        if db is None or user_id is None:
            return {
                "ok": True,
                "kind": "capability_suggestions_recorded",
                "recorded_ids": [],
                "recorded_count": 0,
                "rejected_count": len(getattr(tool_input, "suggestions", [])),
            }
        agent_run_id = (
            agent_run_id_getter() if agent_run_id_getter is not None else None
        )
        if not agent_run_id:
            return {
                "ok": False,
                "status": "FAILED",
                "error": {
                    "code": "AGENT_RUN_CONTEXT_REQUIRED",
                    "message": "能力建议缺少 AgentRun 审计上下文。",
                    "retryable": False,
                    "user_action_required": False,
                },
            }
        return CapabilitySuggestionService(db).record(
            payload=CapabilitySuggestionRecordInput.model_validate(
                tool_input.model_dump()
            ),
            user_id=user_id,
            agent_run_id=agent_run_id,
        )

    return handler


def _agent_capabilities_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """读取固定能力清单，避免 LLM 编造系统能力。"""

    detail_level = getattr(tool_input, "detail_level", "brief")
    return load_agent_capabilities(detail_level="full" if detail_level == "full" else "brief")


def _classification_taxonomy_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """读取固定分类目录，避免 LLM 编造分类体系。"""

    return read_default_taxonomy_catalog(
        detail_level=getattr(tool_input, "detail_level", "brief"),
        max_depth=int(getattr(tool_input, "max_depth", 2)),
    )


def _confirmed_action_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建确认后真实执行工作副本 OperationPlan 的请求级 handler。

    Tool 只能接收计划 ID 和确认文本；目标工作副本、相对路径和 before/after 快照必须从
    后端持久化 OperationPlan 重新读取。缺少数据库或用户上下文时返回失败，绝不能用
    `EXECUTED` 占位掩盖未发生的物理动作。
    """

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """校验当前用户所有权和确认状态后调用统一工作副本执行服务。"""

        operation_plan_id = str(getattr(tool_input, "operation_plan_id"))
        if db is None or user_id is None:
            return {
                "ok": False,
                "operation_plan_id": operation_plan_id,
                "status": "FAILED",
                "error": {
                    "code": "RUNTIME_CONTEXT_REQUIRED",
                    "message": "确认文件操作缺少请求级数据库或用户上下文。",
                },
            }
        current_user = db.get(User, user_id)
        if current_user is None:
            return {
                "ok": False,
                "operation_plan_id": operation_plan_id,
                "status": "FAILED",
                "error": {
                    "code": "USER_NOT_FOUND",
                    "message": "当前用户不存在，不能执行文件操作。",
                },
            }
        try:
            response = OperationPlanService(db).confirm_plan(
                plan_id=operation_plan_id,
                request=OperationConfirmRequest(
                    confirmation=str(getattr(tool_input, "confirmation_text")),
                ),
                current_user=current_user,
            )
        except HTTPException as exc:
            # HTTP 入口和 Agent Tool 共用业务服务，但 Tool 必须把可预期业务拒绝归一为结构化结果。
            return {
                "ok": False,
                "operation_plan_id": operation_plan_id,
                "status": "FAILED",
                "error": {
                    "code": f"OPERATION_PLAN_{exc.status_code}",
                    "message": str(exc.detail),
                },
            }
        return {
            "ok": response.status in {"EXECUTED", "PARTIAL"},
            "operation_plan_id": response.id,
            "status": response.status,
            "changeset_id": response.changeset_id,
            "result": response.result,
        }

    return handler


def _feedback_handler(user_id: str | None = None) -> ToolHandler:
    """创建反馈记录 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """记录用户反馈；managed-file-query 反馈写入 Skill 样本文件。"""

        target_type = str(getattr(tool_input, "target_type")).upper()
        target_id = str(getattr(tool_input, "target_id"))
        if target_type == "SKILL" and target_id == MANAGED_FILE_QUERY_SKILL_ID:
            sample = record_managed_file_query_feedback_sample(
                user_id=user_id,
                feedback_type=str(getattr(tool_input, "feedback_type")),
                comment=str(getattr(tool_input, "comment", "")),
                context_json=getattr(tool_input, "context_json", None),
            )
            return {
                "ok": True,
                "target_type": target_type,
                "target_id": target_id,
                "sample": sample,
            }

        return {
            "ok": True,
            "target_type": target_type,
            "target_id": target_id,
        }

    return handler


def _managed_root_list_handler(db: Any) -> ToolHandler:
    """创建受管目录列表 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """返回安全的受管逻辑目录列表，不暴露容器路径。"""

        if db is None:
            return {"ok": False, "error": {"code": "DB_REQUIRED", "message": "读取受管目录需要数据库会话。"}}
        enabled_only = bool(getattr(tool_input, "enabled_only", True))
        sync_configured_managed_roots(db, scan=False)
        db.commit()
        roots = ManagedFileRepository(db).list_roots()
        if enabled_only:
            roots = [root for root in roots if root.enabled]
        return {
            "ok": True,
            "roots": [ManagedFileService.to_root_response(root).model_dump() for root in roots],
        }

    return handler


def _managed_file_list_handler(db: Any) -> ToolHandler:
    """创建受管文件列表 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """按逻辑目录、扩展名和文件名过滤受管文件。"""

        if db is None:
            return {"ok": False, "error": {"code": "DB_REQUIRED", "message": "读取受管文件需要数据库会话。"}}
        scope = resolve_managed_file_query_scope(
            root_key=getattr(tool_input, "root_key", None),
            path_prefix=getattr(tool_input, "path_prefix", None),
        )
        sync_configured_managed_roots(
            db,
            root_key=scope.root_key,
            scan=False,
        )
        db.commit()
        rows = []
        if not scope.unresolved_root_key:
            rows = ManagedFileRepository(db).list_files(
                root_key=scope.root_key,
                root_keys=scope.configured_root_keys if scope.root_key is None else None,
                path_prefix=scope.path_prefix,
                extension=getattr(tool_input, "extension", None),
                filename_contains=getattr(tool_input, "filename_contains", None),
                category_path=getattr(tool_input, "category_path", None),
                classification_mode=getattr(tool_input, "classification_mode", None),
                status=getattr(tool_input, "status", None),
                limit=int(getattr(tool_input, "limit", 50)),
                offset=int(getattr(tool_input, "offset", 0)),
            )
        root_display_name = "全部受管目录" if scope.root_key is None else None
        if scope.root_key:
            # root_key 继续承担安全定位职责；普通用户回执优先使用业务展示名称。
            resolved_root = ManagedFileRepository(db).get_root_by_key(scope.root_key)
            if resolved_root is not None:
                root_display_name = resolved_root.display_name
        # 返回查询条件用于空结果回执，避免 response 节点无法说明是哪一个受管目录没有文件。
        query = {
            "root_key": scope.root_key,
            "root_display_name": root_display_name,
            "path_prefix": scope.path_prefix,
            "requested_root_key": getattr(tool_input, "root_key", None),
            "unresolved_root_key": scope.unresolved_root_key,
            "extension": getattr(tool_input, "extension", None),
            "filename_contains": getattr(tool_input, "filename_contains", None),
            "category_path": getattr(tool_input, "category_path", None),
            "classification_mode": getattr(tool_input, "classification_mode", None),
            "status": getattr(tool_input, "status", None),
        }
        return {
            "ok": True,
            "query": query,
            "files": [
                ManagedFileService.to_file_response(file=file, root=root).model_dump(mode="json")
                for file, root in rows
            ],
        }

    return handler


def _managed_file_search_handler(db: Any) -> ToolHandler:
    """创建受管文件搜索 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """按文件名关键词执行轻量搜索。"""

        if db is None:
            return {"ok": False, "error": {"code": "DB_REQUIRED", "message": "搜索受管文件需要数据库会话。"}}
        scope = resolve_managed_file_query_scope(
            root_key=getattr(tool_input, "root_key", None),
            path_prefix=getattr(tool_input, "path_prefix", None),
        )
        sync_configured_managed_roots(
            db,
            root_key=scope.root_key,
            scan=False,
        )
        db.commit()
        rows = []
        if not scope.unresolved_root_key:
            rows = ManagedFileRepository(db).list_files(
                root_key=scope.root_key,
                root_keys=scope.configured_root_keys if scope.root_key is None else None,
                path_prefix=scope.path_prefix,
                filename_contains=getattr(tool_input, "query"),
                status="ACTIVE",
                limit=int(getattr(tool_input, "limit", 50)),
                offset=0,
            )
        return {
            "ok": True,
            "files": [
                ManagedFileService.to_file_response(file=file, root=root).model_dump(mode="json")
                for file, root in rows
            ],
        }

    return handler


def _generate_rename_suggestions_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建仅作用于工作副本的重命名建议 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """读取正文并生成待确认计划，不在此阶段修改源文件。"""

        if db is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "DB_REQUIRED", "message": "生成重命名计划需要数据库会话。"}}
        if user_id is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "AUTH_REQUIRED", "message": "生成重命名计划需要当前用户。"}}
        document_ids = list(getattr(tool_input, "document_ids", []) or [])
        if document_ids:
            return UploadedRenameSuggestionService(db=db, user_id=user_id).generate_plan(
                conversation_id=str(getattr(tool_input, "conversation_id")),
                agent_run_id=str(getattr(tool_input, "agent_run_id")),
                document_ids=document_ids,
                limit=int(getattr(tool_input, "limit", 500)),
            )
        candidates = sorted({
            str(value).replace("\\", "/").strip("/")
            for value in list(getattr(tool_input, "path_candidates", []) or [])
            if str(value).strip("/")
        })
        if len(candidates) > 1:
            return _working_copy_scope_error(
                code="AMBIGUOUS_MANAGED_PATH",
                message="受管目录范围存在多个候选，请提供完整相对目录后再重命名。",
            )
        scope = resolve_managed_file_query_scope(
            root_key=getattr(tool_input, "root_key", None),
            path_prefix=candidates[0] if candidates else getattr(tool_input, "path_prefix", None),
        )
        if scope.unresolved_root_key:
            return _working_copy_scope_error(
                code="MANAGED_ROOT_NOT_FOUND",
                message="受管原始目录无法唯一解析，请提供完整逻辑目录。",
            )
        sync_configured_managed_roots(db, root_key=scope.root_key, scan=False)
        db.commit()
        rows = ManagedFileRepository(db).list_files(
            root_key=scope.root_key,
            root_keys=scope.configured_root_keys if scope.root_key is None else None,
            path_prefix=scope.path_prefix,
            extension=getattr(tool_input, "extension", None),
            filename_contains=getattr(tool_input, "filename_contains", None),
            status="ACTIVE",
            limit=int(getattr(tool_input, "limit", 500)),
            offset=0,
        )
        if not rows:
            return _working_copy_scope_error(
                code="MANAGED_FILE_SCOPE_EMPTY",
                message="指定受管原始目录范围内没有找到文件。",
            )
        from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id

        shared_workspace_id = get_shared_workspace_id(db)
        managed_file_ids = [managed_file.id for managed_file, _root in rows]
        working_copies = (
            db.query(WorkingCopy)
            .join(Document, Document.id == WorkingCopy.document_id)
            .filter(
                WorkingCopy.managed_file_id.in_(managed_file_ids),
                WorkingCopy.workspace_id == shared_workspace_id,
                WorkingCopy.status == "ACTIVE",
                Document.user_id == user_id,
            )
            .all()
        )
        copy_by_managed_file = {working_copy.managed_file_id: working_copy for working_copy in working_copies}
        pending_managed_file_ids = [value for value in managed_file_ids if value not in copy_by_managed_file]
        if pending_managed_file_ids:
            result = _working_copy_scope_error(
                code="WORKING_COPY_NOT_READY",
                message="所选原始文件仍在异步导入工作副本，请稍后重试。",
            )
            result["status"] = "WAITING_FOR_ASYNC_JOB"
            result["pending_count"] = len(pending_managed_file_ids)
            return result
        return UploadedRenameSuggestionService(db=db, user_id=user_id).generate_plan(
            conversation_id=str(getattr(tool_input, "conversation_id")),
            agent_run_id=str(getattr(tool_input, "agent_run_id")),
            document_ids=[copy_by_managed_file[value].document_id for value in managed_file_ids],
            limit=int(getattr(tool_input, "limit", 500)),
        )

    return handler


def _working_copy_scope_error(*, code: str, message: str) -> Dict[str, Any]:
    """构造受管原始目录到工作副本解析阶段的安全失败结果。"""

    return {
        "ok": False,
        "kind": "rename_plan",
        "source_kind": "working_copy",
        "storage_scope": "working_copy",
        "status": "FAILED",
        "error": {"code": code, "message": message},
        "suggestions": [],
        "extraction_results": [],
    }


def _resolve_rename_reviews_handler(db: Any, user_id: str | None) -> ToolHandler:
    """处理上传文件低置信度命名更正，旧受管原件链路仍保持退役。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """为当前会话最新上传文件创建延后工作副本重命名计划。"""

        if db is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "DB_REQUIRED", "message": "处理重命名更正需要数据库会话。"}}
        if user_id is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "AUTH_REQUIRED", "message": "处理重命名更正需要当前用户。"}}
        return UploadedRenameReviewResolutionService(db=db, user_id=user_id).resolve(
            conversation_id=str(getattr(tool_input, "conversation_id")),
            agent_run_id=str(getattr(tool_input, "agent_run_id")),
            message=str(getattr(tool_input, "message")),
        )

    return handler


def _working_copy_action_plan_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建自然语言文件动作的请求级 Tool handler，只生成计划不执行。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """把受信任会话标识和后端附件范围交给计划服务。"""

        if db is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "DB_REQUIRED", "message": "创建文件操作计划需要数据库会话。"}}
        if user_id is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "AUTH_REQUIRED", "message": "创建文件操作计划需要当前用户。"}}
        return ConversationalWorkingCopyPlanService(db, user_id).prepare(
            action=str(getattr(tool_input, "action")),
            message=str(getattr(tool_input, "message")),
            document_ids=list(getattr(tool_input, "document_ids", []) or []),
            conversation_id=str(getattr(tool_input, "conversation_id")),
            agent_run_id=str(getattr(tool_input, "agent_run_id")),
        )

    return handler


def _classification_decision_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建自然语言分类决定的请求级 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """把原话和后端附件范围交给统一正式分类服务。"""

        if db is None:
            return {
                "ok": False,
                "status": "FAILED",
                "error": {
                    "code": "DB_REQUIRED",
                    "message": "确认分类需要数据库会话。",
                },
            }
        if user_id is None:
            return {
                "ok": False,
                "status": "FAILED",
                "error": {
                    "code": "AUTH_REQUIRED",
                    "message": "确认分类需要当前用户。",
                },
            }
        return ConversationalClassificationDecisionService(db, user_id).execute(
            action=str(getattr(tool_input, "action")),
            message=str(getattr(tool_input, "message")),
            document_ids=list(getattr(tool_input, "document_ids", []) or []),
            conversation_id=str(getattr(tool_input, "conversation_id")),
            agent_run_id=str(getattr(tool_input, "agent_run_id")),
        )

    return handler


def _managed_file_read_document_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建读取受管文件正文的 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """定位唯一受管文件，复制为当前用户快照，再复用文档解析链路。"""

        if db is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "DB_REQUIRED", "message": "读取受管文件需要数据库会话。"}}
        if user_id is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "AUTH_REQUIRED", "message": "读取受管文件需要当前用户。"}}

        scope = resolve_managed_file_query_scope(
            root_key=getattr(tool_input, "root_key", None),
            path_prefix=getattr(tool_input, "path_prefix", None) or getattr(tool_input, "relative_path", None),
        )
        # 文件读取只能消费 worker 已建立的索引；Tool 调用不得同步遍历受管原始目录。
        sync_configured_managed_roots(db, root_key=scope.root_key, scan=False)
        db.flush()
        if scope.unresolved_root_key:
            return {
                "ok": False,
                "status": "FAILED",
                "error": {"code": "MANAGED_ROOT_NOT_FOUND", "message": "未找到对应的受管目录。"},
            }

        repository = ManagedFileRepository(db)
        max_batch_size = 20
        rows = repository.list_files(
            root_key=scope.root_key,
            root_keys=scope.configured_root_keys if scope.root_key is None else None,
            path_prefix=scope.path_prefix,
            extension=getattr(tool_input, "extension", None),
            filename_contains=getattr(tool_input, "filename_contains", None),
            status="ACTIVE",
            limit=max_batch_size + 1,
            offset=0,
        )
        relative_path = getattr(tool_input, "relative_path", None)
        if relative_path:
            rows = [(file, root) for file, root in rows if file.relative_path == relative_path]
        if not rows:
            return {
                "ok": False,
                "status": "FAILED",
                "error": {"code": "MANAGED_FILE_NOT_FOUND", "message": "未找到匹配的受管文件。"},
            }
        if len(rows) > max_batch_size:
            return {
                "ok": False,
                "status": "FAILED",
                "error": {
                    "code": "MANAGED_FILE_BATCH_TOO_LARGE",
                    "message": f"匹配到超过 {max_batch_size} 个受管文件，请补充更具体的目录或文件名。",
                    "candidates": [
                        ManagedFileService.to_file_response(file=file, root=root).model_dump(mode="json")
                        for file, root in rows[:max_batch_size]
                    ],
                },
            }

        snapshot_service = ManagedFileSnapshotService(db=db, user_id=user_id)
        extraction_results = []
        for managed_file, root in rows:
            try:
                with db.begin_nested():
                    result = _snapshot_and_extract_managed_file(
                        db=db,
                        user_id=user_id,
                        managed_file=managed_file,
                        root=root,
                        force_reprocess=bool(getattr(tool_input, "force_reprocess", False)),
                        snapshot_service=snapshot_service,
                    )
            except Exception as exc:
                result = _failed_managed_file_snapshot_output(
                    managed_file=managed_file,
                    root=root,
                    error_code=exc.__class__.__name__,
                    error_message=str(exc) or "受管文件快照处理失败。",
                )
            extraction_results.append(result)
        if len(extraction_results) == 1:
            return extraction_results[0]
        completed_count = len([item for item in extraction_results if item.get("status") == "COMPLETED"])
        failed_count = len(extraction_results) - completed_count
        batch_status = (
            "COMPLETED"
            if failed_count == 0
            else "FAILED"
            if completed_count == 0
            else "PARTIAL"
        )
        return {
            "ok": completed_count > 0,
            "status": batch_status,
            "matched_count": len(extraction_results),
            "completed_count": completed_count,
            "failed_count": failed_count,
            "extraction_results": extraction_results,
            "source": "managed-file-read-document",
        }

    return handler


def _managed_file_classification_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建受管目录批量分类入口，并复用受控快照与全文解析实现。"""

    read_handler = _managed_file_read_document_handler(db, user_id)

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """返回标准解析结果，后续统一由 Graph 全文分类服务消费。"""

        if db is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "DB_REQUIRED", "message": "受管文件分类需要数据库会话。"}}
        if user_id is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "AUTH_REQUIRED", "message": "受管文件分类需要当前用户。"}}
        scope = resolve_managed_file_query_scope(
            root_key=getattr(tool_input, "root_key", None),
            path_prefix=getattr(tool_input, "path_prefix", None),
        )
        sync_configured_managed_roots(db, root_key=scope.root_key, scan=False)
        db.flush()
        if scope.unresolved_root_key:
            return {
                "ok": False,
                "status": "FAILED",
                "error": {"code": "MANAGED_ROOT_NOT_FOUND", "message": "未找到对应的受管目录。"},
            }
        sync_limit = get_settings().managed_file_classification_sync_limit
        repository = ManagedFileRepository(db)
        preview_rows = repository.list_files(
            root_key=scope.root_key,
            root_keys=scope.configured_root_keys if scope.root_key is None else None,
            path_prefix=scope.path_prefix,
            extension=getattr(tool_input, "extension", None),
            filename_contains=getattr(tool_input, "filename_contains", None),
            status="ACTIVE",
            limit=sync_limit + 1,
            offset=0,
        )
        if not preview_rows:
            return {
                "ok": False,
                "status": "FAILED",
                "error": {"code": "MANAGED_FILE_NOT_FOUND", "message": "未找到匹配的受管文件。"},
            }
        if len(preview_rows) > sync_limit:
            conversation_id = str(getattr(tool_input, "conversation_id", None) or "")
            agent_run_id = str(getattr(tool_input, "agent_run_id", None) or "")
            if not conversation_id or not agent_run_id:
                return {
                    "ok": False,
                    "status": "FAILED",
                    "error": {
                        "code": "ASYNC_JOB_CONTEXT_REQUIRED",
                        "message": "大批量受管文件分类缺少 AgentRun 上下文。",
                    },
                }
            distinct_root_ids = {root.id for _managed_file, root in preview_rows}
            job = FilesystemJobQueue(db).create_job(
                job_type="CLASSIFY_MANAGED_FILES",
                root_id=next(iter(distinct_root_ids)) if len(distinct_root_ids) == 1 else None,
                created_by=user_id,
                payload={
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "agent_run_id": agent_run_id,
                    "root_key": scope.root_key,
                    "configured_root_keys": scope.configured_root_keys,
                    "path_prefix": scope.path_prefix,
                    "extension": getattr(tool_input, "extension", None),
                    "filename_contains": getattr(tool_input, "filename_contains", None),
                    "recursive": bool(getattr(tool_input, "recursive", True)),
                    "force_reprocess": bool(getattr(tool_input, "force_reprocess", False)),
                },
            )
            job.progress_total = repository.count_files(
                root_key=scope.root_key,
                root_keys=scope.configured_root_keys if scope.root_key is None else None,
                path_prefix=scope.path_prefix,
                extension=getattr(tool_input, "extension", None),
                filename_contains=getattr(tool_input, "filename_contains", None),
                status="ACTIVE",
            )
            db.flush()
            return {
                "ok": True,
                "status": "PENDING",
                "kind": "filesystem_job",
                "async_job": True,
                "job_id": job.id,
                "async_job_id": job.id,
                "job_type": job.job_type,
                "matched_count": job.progress_total,
                "source": "classify-managed-files",
            }

        read_input = ManagedFileReadDocumentInput(
            root_key=getattr(tool_input, "root_key", None),
            path_prefix=getattr(tool_input, "path_prefix", None),
            extension=getattr(tool_input, "extension", None),
            filename_contains=getattr(tool_input, "filename_contains", None),
            force_reprocess=bool(getattr(tool_input, "force_reprocess", False)),
            scan_before_read=False,
        )
        output = read_handler(read_input)
        output["source"] = "classify-managed-files"
        output["classification_requested"] = True
        output["classification_force_reprocess"] = bool(
            getattr(tool_input, "force_reprocess", False)
        )
        for item in output.get("extraction_results", []):
            if isinstance(item, dict):
                item["source"] = "classify-managed-files"
                item["classification_requested"] = True
                item["classification_force_reprocess"] = bool(
                    getattr(tool_input, "force_reprocess", False)
                )
        return output

    return handler


def _mcp_filesystem_list_handler() -> ToolHandler:
    """创建 Filesystem MCP 实时目录列举 handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """通过 MCP 列出受管目录，不触发数据库扫描。"""

        try:
            runner, bridge = get_mcp_filesystem()
            path = bridge.resolve_relative_path(getattr(tool_input, "path_prefix", None))
            result = bridge.call_sync(
                runner,
                "list_directory_with_sizes",
                {
                    "path": path,
                    "sortBy": getattr(tool_input, "sort_by", "name"),
                },
            )
            result["query"] = {
                "path_prefix": getattr(tool_input, "path_prefix", None),
                "sort_by": getattr(tool_input, "sort_by", "name"),
            }
            return result
        except MCPFilesystemError as exc:
            return _mcp_filesystem_error(tool_name="mcp-filesystem-list", error=exc)

    return handler


def _snapshot_and_extract_managed_file(
    *,
    db: Any,
    user_id: str,
    managed_file: Any,
    root: Any,
    force_reprocess: bool,
    snapshot_service: ManagedFileSnapshotService,
) -> Dict[str, Any]:
    """创建或复用一个受管文件快照，并执行正文解析。"""

    managed_payload = ManagedFileService.to_file_response(file=managed_file, root=root).model_dump(mode="json")
    resolution = snapshot_service.resolve(managed_file=managed_file, root=root)
    extraction_input = DocumentToolInput(
        document_id=resolution.document.id,
        force_reprocess=force_reprocess,
    )
    try:
        output = _extract_document_text_handler(db, user_id)(extraction_input)
    except Exception:
        if resolution.snapshot_status == "CREATED":
            snapshot_service.cleanup_created_snapshot(document=resolution.document)
        raise
    output["managed_file"] = managed_payload
    output["source"] = "managed-file-read-document"
    output["source_kind"] = "managed_file"
    output["managed_file_id"] = managed_file.id
    output["root_key"] = root.root_key
    output["relative_path"] = managed_file.relative_path
    output["snapshot_id"] = resolution.snapshot.id
    output["snapshot_status"] = resolution.snapshot_status
    output["source_sha256"] = resolution.source_sha256
    return output


def _failed_managed_file_snapshot_output(
    *,
    managed_file: Any,
    root: Any,
    error_code: str,
    error_message: str,
) -> Dict[str, Any]:
    """构造不影响同批其他文件的受管快照失败结果。"""

    output = _failed_extraction_output(
        document_id="",
        error={"code": error_code, "message": error_message},
    )
    output["extraction_run_id"] = f"failed-managed-{managed_file.id}"
    output["managed_file"] = ManagedFileService.to_file_response(file=managed_file, root=root).model_dump(mode="json")
    output["source"] = "managed-file-read-document"
    output["source_kind"] = "managed_file"
    output["managed_file_id"] = managed_file.id
    output["root_key"] = root.root_key
    output["relative_path"] = managed_file.relative_path
    output["snapshot_status"] = "FAILED"
    return output


def _mcp_filesystem_search_handler() -> ToolHandler:
    """创建 Filesystem MCP 实时文件搜索 handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """通过 MCP 搜索受管目录，不触发数据库扫描。"""

        try:
            runner, bridge = get_mcp_filesystem()
            path = bridge.resolve_relative_path(getattr(tool_input, "path_prefix", None))
            result = bridge.call_sync(
                runner,
                "search_files",
                {
                    "path": path,
                    "pattern": getattr(tool_input, "query"),
                    "excludePatterns": list(getattr(tool_input, "exclude_patterns", [])),
                },
            )
            result["query"] = {
                "query": getattr(tool_input, "query"),
                "path_prefix": getattr(tool_input, "path_prefix", None),
            }
            return result
        except MCPFilesystemError as exc:
            return _mcp_filesystem_error(tool_name="mcp-filesystem-search", error=exc)

    return handler


def _mcp_filesystem_info_handler() -> ToolHandler:
    """创建 Filesystem MCP 路径元数据读取 handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """通过 MCP 读取受管路径元数据。"""

        try:
            runner, bridge = get_mcp_filesystem()
            path = bridge.resolve_relative_path(getattr(tool_input, "path"))
            result = bridge.call_sync(runner, "get_file_info", {"path": path})
            result["query"] = {"path": getattr(tool_input, "path")}
            return result
        except MCPFilesystemError as exc:
            return _mcp_filesystem_error(tool_name="mcp-filesystem-info", error=exc)

    return handler


def _mcp_filesystem_error(*, tool_name: str, error: MCPFilesystemError) -> Dict[str, Any]:
    """把 MCP 桥接异常转换成 Tool 结构化失败结果。"""

    return {
        "ok": False,
        "status": "FAILED",
        "tool_name": tool_name,
        "error": {
            "code": error.__class__.__name__,
            "message": str(error),
            "retryable": False,
            "user_action_required": False,
        },
    }


def _managed_root_scan_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建受管目录扫描任务 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """创建异步扫描任务，供后续 worker 领取执行。"""

        if db is None:
            return {"ok": False, "error": {"code": "DB_REQUIRED", "message": "创建扫描任务需要数据库会话。"}}
        root = ManagedFileRepository(db).get_root_by_key(getattr(tool_input, "root_key"))
        if root is None or not root.enabled:
            return {"ok": False, "status": "FAILED", "error": {"code": "ROOT_NOT_FOUND", "message": "受管目录不存在。"}}
        job = FilesystemJobQueue(db).create_job(
            job_type="SCAN_MANAGED_ROOT",
            root_id=root.id,
            created_by=user_id,
            payload={"root_key": root.root_key},
        )
        db.flush()
        return {
            "ok": True,
            "job_id": job.id,
            "root_id": root.id,
            "root_key": root.root_key,
            "status": job.status,
        }

    return handler


def _read_original_file_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建读取原始文件元信息的 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """返回当前用户文件的安全元信息，不返回本地路径和二进制内容。"""

        if db is None:
            return {"ok": False, "error": {"code": "DB_REQUIRED", "message": "读取原始文件需要数据库会话。"}}
        result = FileExtractionRepository(db, user_id).get_original_file_metadata(
            getattr(tool_input, "document_id")
        )
        if result.get("ok"):
            result["kind"] = "original_file_metadata"
        return result

    return handler


def _extract_document_text_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建解析原始文件文本的 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """解析当前用户文件，并把页面文本写入数据库。"""

        document_id = str(getattr(tool_input, "document_id"))
        start = time.perf_counter()
        if db is None:
            log_event(
                "file.extract.failed",
                level="ERROR",
                document_id=document_id,
                status="FAILED",
                duration_ms=0,
                error_code="DB_REQUIRED",
                message="解析文件需要数据库会话。",
            )
            return {"ok": False, "error": {"code": "DB_REQUIRED", "message": "解析文件需要数据库会话。"}}
        repository = FileExtractionRepository(db, user_id)
        document = repository.get_document_for_current_user(document_id)
        if document is None:
            error = {"code": "DOCUMENT_NOT_FOUND", "message": "文件不存在或不属于当前用户。"}
            log_event(
                "file.extract.failed",
                level="ERROR",
                document_id=document_id,
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=error.get("code"),
                message=error.get("message"),
            )
            return _failed_extraction_output(document_id=document_id, error=error)
        # 即使已有成功的 document_pages，也不能在工作副本进入回收站后复用历史正文。
        lifecycle = repository.resolve_original_file_for_document(document)
        if (
            not lifecycle.get("ok")
            and (lifecycle.get("error") or {}).get("code") == "FILE_TRASHED"
        ):
            error = lifecycle.get("error") or {}
            log_event(
                "file.extract.failed",
                level="WARNING" if error.get("code") == "FILE_TRASHED" else "ERROR",
                document_id=document_id,
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=error.get("code"),
                message=error.get("message"),
            )
            return _attach_trash_restore_selection(
                result=_failed_extraction_output(document_id=document_id, error=error),
                db=db,
                user_id=user_id,
                filename=str(document.original_filename or ""),
            )

        force_reprocess = bool(getattr(tool_input, "force_reprocess", False))
        force_reconvert = bool(getattr(tool_input, "force_reconvert", False))
        # 显式重新转换不能复用旧页面，否则 Tool 会报告执行成功但根本不读取新派生件。
        force_reprocess = force_reprocess or force_reconvert
        document_version = repository.get_current_document_version(document=document)
        readable_source_resolver = ReadableDocumentSourceResolver(db=db)
        expected_parser_config_hash = readable_source_resolver.expected_parser_config_hash(
            document=document,
            document_version=document_version,
        )
        reusable = (
            None
            if force_reprocess
            else repository.get_latest_successful_extraction(
                document_id=document.id,
                document_version_id=document_version.id if document_version else None,
                parser_config_hash=expected_parser_config_hash,
            )
        )
        if reusable is not None:
            run = reusable["run"]
            index_result = (
                DocumentIndexService(db=db).build_latest_for_user(document_id=document.id, user_id=user_id)
                if user_id is not None
                else {"ok": False, "status": "FAILED", "chunk_count": 0, "evidence_count": 0}
            )
            persisted_metadata = (
                dict(reusable["pages"][0].metadata_json or {})
                if reusable["pages"]
                else {}
            )
            log_event(
                "file.extract.completed",
                document_id=document.id,
                status="REUSED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                message="复用已有文件解析结果",
                extractor=run.extractor,
                page_count=len(reusable["pages"]),
            )
            return {
                "ok": True,
                "document_id": document.id,
                "extraction_run_id": run.id,
                "status": "COMPLETED",
                "extractor": run.extractor,
                "reused": True,
                "read_quality": _read_quality_from_persisted_pages(pages=reusable["pages"]),
                "read_profile": _read_profile_from_persisted_pages(extractor=run.extractor, pages=reusable["pages"]),
                "structured_element_count": len(reusable.get("elements", [])),
                "conversion_artifact_id": persisted_metadata.get("conversion_artifact_id"),
                "conversion_artifact_type": persisted_metadata.get("conversion_artifact_type"),
                "conversion_reused": None,
                "conversion_source_format": persisted_metadata.get("source_format"),
                "conversion_parsed_format": persisted_metadata.get("parsed_format"),
                "conversion_converter": persisted_metadata.get("converter"),
                "conversion_converter_version": persisted_metadata.get("converter_version"),
                "conversion_config_hash": persisted_metadata.get("conversion_config_hash"),
                "search_status": "READY" if index_result.get("ok") else "NEEDS_REVIEW",
                "chunk_count": int(index_result.get("chunk_count") or 0),
                "evidence_count": int(index_result.get("evidence_count") or 0),
                "pages": [
                    {
                        "page_number": page.page_number,
                        "sheet_name": page.sheet_name,
                        "text_preview": page.text_content[:300],
                        "char_count": len(page.text_content),
                        "metadata": page.metadata_json,
                    }
                    for page in reusable["pages"]
                ],
                "error": None,
            }

        resolved = repository.resolve_original_file_for_document(document)
        if not resolved["ok"]:
            error = resolved.get("error") or {}
            log_event(
                "file.extract.failed",
                level="ERROR",
                document_id=document.id,
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=error.get("code"),
                message=error.get("message"),
            )
            return _attach_trash_restore_selection(
                result=_failed_extraction_output(document_id=document.id, error=error),
                db=db,
                user_id=user_id,
                filename=str(document.original_filename or ""),
            )

        readable_source = readable_source_resolver.resolve(
            document=document,
            document_version=document_version,
            original_path=resolved["file_path"],
            force_reconvert=force_reconvert,
        )
        extraction = extract_document_text(
            file_path=readable_source.parse_path,
            filename=readable_source.parse_filename,
            content_type=readable_source.parse_content_type,
        )
        extraction = apply_readable_source_metadata(extraction, source=readable_source)
        run = repository.create_extraction_run(
            document_id=document.id,
            document_version_id=document_version.id if document_version else None,
            extractor=extraction["extractor"],
            parser_name=extraction.get("parser_name", ""),
            parser_version=extraction.get("parser_version", ""),
            parser_config_hash=extraction.get("parser_config_hash", ""),
        )
        if extraction["ok"]:
            repository.complete_extraction_run(
                run=run,
                pages=extraction["pages"],
                elements=extraction.get("elements", []),
            )
        else:
            repository.fail_extraction_run(run=run, error_message=extraction["error"]["message"])
        index_result = (
            DocumentIndexService(db=db).build_latest_for_user(document_id=document.id, user_id=user_id)
            if extraction["ok"] and user_id is not None
            else {"ok": False, "status": "FAILED", "chunk_count": 0, "evidence_count": 0}
        )
        extraction_status = "COMPLETED" if extraction["ok"] else "FAILED"
        event_name = "file.extract.completed" if extraction["ok"] else "file.extract.failed"
        error = extraction.get("error") or {}
        for warning in extraction.get("warnings", []):
            log_event(
                "file.parse.fallback",
                level="WARNING",
                document_id=document.id,
                status="COMPLETED" if extraction["ok"] else "FAILED",
                error_code=warning.get("code"),
                message=warning.get("message"),
                extractor=extraction["extractor"],
            )
        log_event(
            event_name,
            level="ERROR" if not extraction["ok"] else "INFO",
            document_id=document.id,
            status=extraction_status,
            duration_ms=int((time.perf_counter() - start) * 1000),
            error_code=error.get("code"),
            message=error.get("message") or "文件解析完成",
            extractor=extraction["extractor"],
            page_count=len(extraction["pages"]),
        )
        if extraction["extractor"] == "ocr":
            log_event(
                "file.ocr.completed" if extraction["ok"] else "file.ocr.failed",
                level="ERROR" if not extraction["ok"] else "INFO",
                document_id=document.id,
                status=extraction_status,
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=error.get("code"),
                message=error.get("message") or "OCR 处理完成",
            )
        return {
            "ok": extraction["ok"],
            "document_id": document.id,
            "extraction_run_id": run.id,
            "status": extraction["status"],
            "extractor": extraction["extractor"],
            "reused": False,
            "read_quality": extraction.get("read_quality"),
            "read_profile": extraction.get("read_profile"),
            "structured_element_count": len(extraction.get("elements", [])),
            "conversion_artifact_id": extraction.get("conversion_artifact_id"),
            "conversion_artifact_type": extraction.get("conversion_artifact_type"),
            "conversion_reused": extraction.get("conversion_reused"),
            "conversion_source_format": extraction.get("conversion_source_format"),
            "conversion_parsed_format": extraction.get("conversion_parsed_format"),
            "conversion_converter": extraction.get("conversion_converter"),
            "conversion_converter_version": extraction.get("conversion_converter_version"),
            "conversion_config_hash": extraction.get("conversion_config_hash"),
            "search_status": "READY" if index_result.get("ok") else "NEEDS_REVIEW",
            "chunk_count": int(index_result.get("chunk_count") or 0),
            "evidence_count": int(index_result.get("evidence_count") or 0),
            "warnings": extraction.get("warnings", []),
            "pages": [
                {
                    "page_number": page.get("page_number"),
                    "sheet_name": page.get("sheet_name"),
                    "text_preview": page.get("text", "")[:300],
                    "char_count": len(page.get("text", "")),
                    "metadata": page.get("metadata", {}),
                }
                for page in extraction["pages"]
            ],
            "error": extraction.get("error"),
        }

    return handler


def _structured_image_extraction_handler(
    db: Any,
    user_id: str | None,
    conversation_id_getter: Callable[[], str | None] | None,
    agent_run_id_getter: Callable[[], str | None] | None,
) -> ToolHandler:
    """创建动态图片结构化抽取异步任务 handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """只接受 document_id 与严格动态 Schema，路径由后端仓库解析。"""

        document_id = str(getattr(tool_input, "document_id", ""))
        if db is None or user_id is None:
            return {
                "kind": "structured_image_extraction",
                "ok": False,
                "status": "FAILED",
                "document_id": document_id,
                "error": {
                    "code": "RUNTIME_CONTEXT_REQUIRED",
                    "message": "图片结构化抽取上下文不可用。",
                    "retryable": False,
                    "user_action_required": False,
                },
                "record_count": 0,
                "field_count": 0,
                "review_count": 0,
                "missing_required_field_count": 0,
                "retryable": False,
                "recommended_retry_strategy": "NONE",
                "low_confidence_field_keys": [],
                "field_schema": [],
                "records": [],
                "review_items": [],
                "original_unchanged": True,
            }
        return StructuredExtractionService(
            db=db,
            user_id=user_id,
            conversation_id=(conversation_id_getter() if conversation_id_getter else None),
            agent_run_id=(agent_run_id_getter() if agent_run_id_getter else None),
        ).enqueue(StructuredImageExtractionInput.model_validate(tool_input.model_dump()))

    return handler


def _failed_extraction_output(*, document_id: str, error: Dict[str, Any]) -> Dict[str, Any]:
    """生成标准解析失败输出，确保前端能展示逐文件失败原因。"""

    return {
        "ok": False,
        "document_id": document_id,
        "extraction_run_id": f"failed-{document_id}",
        "status": "FAILED",
        "extractor": "unknown",
        "reused": False,
        "read_quality": "FAILED",
        "read_profile": {
            "file_type": "unknown",
            "page_count": 0,
            "sheet_count": 0,
            "char_count": 0,
            "has_text": False,
            "requires_ocr": False,
            "ocr_used": False,
        },
        "pages": [],
        "error": error,
    }


def _read_quality_from_persisted_pages(*, pages: list[Any]) -> str:
    """从已持久化页面推导读取质量，优先复用页面 metadata。"""

    for page in pages:
        quality = (page.metadata_json or {}).get("read_quality")
        if quality:
            return str(quality)
    return "GOOD" if any(page.text_content for page in pages) else "PARTIAL"


def _read_profile_from_persisted_pages(*, extractor: str, pages: list[Any]) -> Dict[str, Any]:
    """从已持久化页面反推读取 Profile，用于复用解析结果。"""

    char_count = sum(len(page.text_content or "") for page in pages)
    sheet_count = len([page for page in pages if page.sheet_name])
    quality = _read_quality_from_persisted_pages(pages=pages)
    return {
        "file_type": _file_type_from_extractor_name(extractor),
        "page_count": len(pages),
        "sheet_count": sheet_count,
        "char_count": char_count,
        "has_text": char_count > 0,
        "requires_ocr": quality == "OCR_NEEDED",
        "ocr_used": any(bool((page.metadata_json or {}).get("ocr_fallback")) for page in pages) or "ocr" in extractor,
    }


def _file_type_from_extractor_name(extractor: str) -> str:
    """把解析器名称归一为读取 Profile 的文件类型。"""

    if extractor == "plain-text":
        return "text"
    if extractor in {"csv", "excel"}:
        return "spreadsheet"
    if extractor.startswith("doc"):
        return "document"
    if extractor.startswith("pdf"):
        return "pdf"
    if extractor in {"ocr", "paddleocr_cpu", "llm_ocr_remote"}:
        return "image"
    return "unknown"


def _tool(
    name: str,
    description: str,
    input_model: Type[BaseModel],
    side_effects: bool,
    requires_confirmation: bool,
    writes: List[str],
    handler: ToolHandler,
    *,
    output_model: Type[BaseModel] = GenericToolOutput,
    version: str = "1",
    risk_level: str | None = None,
    allowed_skill_ids: List[str] | None = None,
    retry_policy: str = "never",
    enabled: bool = True,
    expose_to_planner: bool = True,
    adaptive_ready: bool = False,
    observation_policy: ObservationPolicy = "PLANNER_ON_SIGNAL",
) -> ToolDefinition:
    """使用 MVP Tool 的共享默认值构造一个 ToolDefinition。"""

    return ToolDefinition(
        name=name,
        version=version,
        description=description,
        input_model=input_model,
        output_model=output_model,
        side_effects=side_effects,
        risk_level=risk_level or ("high" if requires_confirmation else "low"),
        requires_confirmation=requires_confirmation,
        allowed_roles=["user", "ops", "admin"],
        allowed_skill_ids=list(allowed_skill_ids or []),
        writes=writes,
        failure_strategy="return structured error and record invocation",
        retry_policy=retry_policy,
        enabled=enabled,
        expose_to_planner=expose_to_planner,
        adaptive_ready=adaptive_ready,
        observation_policy=observation_policy,
        handler=handler,
    )

def _attach_spreadsheet_conversion_metadata(
    *,
    result: Dict[str, Any],
    source: ReadableDocumentSource,
) -> Dict[str, Any]:
    """把受控派生件事实加入表格 Tool 输出，不暴露任何服务器路径。"""

    projected = dict(result)
    projected.update(
        {
            "conversion_artifact_id": source.artifact_id,
            "conversion_artifact_type": source.artifact_type,
            "conversion_reused": source.reused if source.converted else None,
            "conversion_source_format": source.source_format,
            "conversion_parsed_format": source.parsed_format,
            "conversion_converter": source.converter_name,
            "conversion_converter_version": source.converter_version,
            "conversion_config_hash": source.converter_config_hash,
            "original_unchanged": True,
        }
    )
    return projected


def _spreadsheet_conversion_failure(
    *,
    kind: str,
    document_id: str,
    source: ReadableDocumentSource,
) -> Dict[str, Any]:
    """将 XLS 持久化转换失败投影为稳定 Tool 错误，禁止回退到文件名事实。"""

    error = dict(source.conversion_error or {})
    return {
        "kind": kind,
        "ok": False,
        "status": "FAILED",
        "document_id": document_id,
        "original_unchanged": True,
        "error": {
            "code": str(error.get("code") or "XLS_CONVERSION_FAILED"),
            "message": str(error.get("message") or "无法生成可读取的 XLSX 派生件。"),
            "retryable": bool(error.get("retryable", True)),
            "user_action_required": False,
        },
    }


def _analyze_spreadsheet_handler(
    db: Any,
    user_id: str | None,
    conversation_id_getter: Callable[[], str | None] | None = None,
    agent_run_id_getter: Callable[[], str | None] | None = None,
) -> ToolHandler:
    """通过文件权限仓库定位原件，再调用只读表格分析服务。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        if db is None:
            return {
                "kind": "spreadsheet_analysis",
                "ok": False,
                "status": "FAILED",
                "error": {
                    "code": "DATABASE_SESSION_REQUIRED",
                    "message": "表格分析需要数据库会话。",
                    "retryable": False,
                    "user_action_required": False,
                },
            }

        document_id = str(getattr(tool_input, "document_id"))
        repository = FileExtractionRepository(db, user_id)
        resolved = repository.resolve_original_file(document_id)
        if not resolved.get("ok"):
            error = resolved.get("error") or {
                "code": "FILE_RESOLUTION_FAILED",
                "message": "无法定位已授权的原始文件。",
            }
            result = {
                "kind": "spreadsheet_analysis",
                "ok": False,
                "status": "FAILED",
                "document_id": document_id,
                "error": error,
            }
            return _attach_trash_restore_selection(
                result=result,
                db=db,
                user_id=user_id,
                filename=str(error.get("filename") or document_id),
            )

        document = resolved["document"]
        document_version = repository.get_current_document_version(document=document)
        readable_source = ReadableDocumentSourceResolver(db=db).resolve(
            document=document,
            document_version=document_version,
            original_path=resolved["file_path"],
            purpose="spreadsheet-analysis",
        )
        if readable_source.conversion_error and not readable_source.converted:
            return _spreadsheet_conversion_failure(
                kind="spreadsheet_analysis",
                document_id=document_id,
                source=readable_source,
            )
        question = str(getattr(tool_input, "question"))
        result = SpreadsheetAnalysisService().analyze(
            document_id=str(document.id),
            filename=str(document.original_filename),
            file_path=readable_source.parse_path,
            question=question,
        )
        result = _attach_spreadsheet_conversion_metadata(result=result, source=readable_source)
        conversation_id = conversation_id_getter() if conversation_id_getter else None
        if result.get("ok") and result.get("status") == "COMPLETED" and conversation_id and user_id:
            result["qa_answer_id"] = EvidenceAnswerService(
                db=db,
                user_id=user_id,
                conversation_id=conversation_id,
                agent_run_id=agent_run_id_getter() if agent_run_id_getter else None,
            ).persist_deterministic_calculation(
                question=question,
                document_id=str(document.id),
                answer_text=format_spreadsheet_analysis_response([result]),
                calculation_result=result,
            )
        return result

    return handler


def _spreadsheet_workbench_handler(db: Any, user_id: str | None, *, action: str) -> ToolHandler:
    """创建表格工作台只读 Tool handler；不接受任何文件路径参数。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """解析当前用户原件后执行 Profile 或校验。"""

        if db is None:
            return {
                "kind": f"spreadsheet_{action}",
                "ok": False,
                "status": "FAILED",
                "error": {
                    "code": "DATABASE_SESSION_REQUIRED",
                    "message": "表格工作台需要数据库会话。",
                    "retryable": False,
                    "user_action_required": False,
                },
            }

        document_id = str(getattr(tool_input, "document_id"))
        repository = FileExtractionRepository(db, user_id)
        resolved = repository.resolve_original_file(document_id)
        if not resolved.get("ok"):
            error = resolved.get("error") or {
                "code": "FILE_RESOLUTION_FAILED",
                "message": "无法定位已授权的原始文件。",
            }
            result = {
                "kind": f"spreadsheet_{action}",
                "ok": False,
                "status": "FAILED",
                "document_id": document_id,
                "error": error,
            }
            return _attach_trash_restore_selection(
                result=result,
                db=db,
                user_id=user_id,
                filename=str(error.get("filename") or document_id),
            )

        document = resolved["document"]
        document_version = repository.get_current_document_version(document=document)
        readable_source = ReadableDocumentSourceResolver(db=db).resolve(
            document=document,
            document_version=document_version,
            original_path=resolved["file_path"],
            purpose=f"spreadsheet-{action}",
        )
        if readable_source.conversion_error and not readable_source.converted:
            return _spreadsheet_conversion_failure(
                kind=f"spreadsheet_{action}",
                document_id=document_id,
                source=readable_source,
            )
        service = SpreadsheetWorkbenchService()
        kwargs = {
            "document_id": str(document.id),
            "filename": str(document.original_filename),
            "file_path": readable_source.parse_path,
            "original_file_type": Path(document.original_filename).suffix.lower(),
        }
        if action == "profile":
            result = service.profile(**kwargs)
        else:
            result = service.validate(**kwargs)
        return _attach_spreadsheet_conversion_metadata(result=result, source=readable_source)

    return handler


def _build_mvp_tools(
    *,
    db: Any = None,
    user_id: str | None = None,
    conversation_id_getter: Callable[[], str | None] | None = None,
    agent_run_id_getter: Callable[[], str | None] | None = None,
) -> Dict[str, ToolDefinition]:
    """创建当前已经接入真实业务能力的 Tool 目录。

    历史契约中的无副作用占位 Tool 不再注册，避免 Planner 或维护人员把结构化空结果误认为真实执行。
    """

    tools = [
        _tool(
            "extract-image-structured-data",
            "Extract user-requested dynamic fields from an authorized image or scanned PDF with persisted evidence.",
            StructuredImageExtractionInput,
            True,
            False,
            [
                "structured_extraction_runs",
                "structured_extraction_fields",
                "document_extraction_runs",
                "document_pages",
                "document_elements",
                "filesystem_jobs",
                "change_sets",
                "change_items",
            ],
            _structured_image_extraction_handler(
                db,
                user_id,
                conversation_id_getter,
                agent_run_id_getter,
            ),
            output_model=StructuredExtractionToolOutput,
            adaptive_ready=(
                get_settings().structured_extraction_enabled
                and get_settings().pp_structure_enabled
            ),
            observation_policy="PLANNER_AFTER_EXECUTION",
            risk_level="medium",
            retry_policy="one_targeted_retry",
            allowed_skill_ids=["image-structured-extraction"],
        ),
        _tool("chunk-build", "Build chunks and evidence spans.", DocumentToolInput, True, False, ["document_chunks", "evidence_spans"], _chunk_build_handler(db, user_id)),
        _tool("read-document-insights", "Read deterministic ingest insights for uploaded documents.", DocumentInsightsReadInput, False, False, [], _document_insights_handler(db, user_id), output_model=DocumentInsightsToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("read-document-classifications", "Read latest persisted classification suggestions for uploaded documents.", DocumentClassificationsReadInput, False, False, [], _document_classifications_handler(db, user_id), output_model=DocumentClassificationsToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("read-original-file", "Read safe metadata for an uploaded original file.", DocumentToolInput, False, False, [], _read_original_file_handler(db, user_id), output_model=OriginalFileMetadataToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("extract-document-text", "Extract text from uploaded files and persist document pages.", DocumentToolInput, True, False, ["document_extraction_runs", "document_pages"], _extract_document_text_handler(db, user_id), output_model=DocumentExtractionToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("intent-summary", "Record LLM-understood user intent without side effects.", IntentSummaryInput, False, False, [], _intent_summary_handler, output_model=IntentSummaryToolOutput, adaptive_ready=True),
        _tool(
            "capability-suggestion-record",
            "Persist a validated capability gap for administrator review.",
            CapabilitySuggestionRecordInput,
            True,
            False,
            ["capability_suggestions"],
            _capability_suggestion_handler(
                db,
                user_id,
                agent_run_id_getter,
            ),
            expose_to_planner=False,
        ),
        _tool("read-agent-capabilities", "Read fixed File Agent capability catalog.", AgentCapabilitiesReadInput, False, False, [], _agent_capabilities_handler, output_model=AgentCapabilitiesToolOutput, adaptive_ready=True),
        _tool("read-classification-taxonomy", "Read fixed classification taxonomy catalog.", ClassificationTaxonomyReadInput, False, False, [], _classification_taxonomy_handler, output_model=ClassificationTaxonomyToolOutput, adaptive_ready=True),
        _tool(
            "hybrid-search",
            "Run summary-first workspace retrieval.",
            SearchToolInput,
            False,
            False,
            [],
            _with_search_binding_projection(
                _search_handler(
                    db,
                    user_id,
                    conversation_id_getter,
                    agent_run_id_getter,
                )
            ),
            output_model=WorkspaceFileSearchToolOutput,
            adaptive_ready=True,
            observation_policy="PLANNER_AFTER_EXECUTION",
        ),
        _tool(
            "evidence-answer",
            "Answer from current active working-copy evidence and persist validated references.",
            EvidenceAnswerInput,
            True,
            False,
            ["qa_answers", "answer_references"],
            _evidence_answer_handler(
                db,
                user_id,
                conversation_id_getter,
                agent_run_id_getter,
            ),
            output_model=EvidenceAnswerToolOutput,
            adaptive_ready=True,
            observation_policy="PLANNER_AFTER_EXECUTION",
        ),
        _tool("confirmed-file-action", "Execute confirmed operation plan.", ConfirmedFileActionInput, True, True, ["change_items"], _confirmed_action_handler(db, user_id)),
        _tool("feedback-record", "Record user feedback.", FeedbackRecordInput, True, False, ["feedback", "skill_feedback_samples"], _feedback_handler(user_id)),
        _tool("managed-root-list", "List server managed logical roots.", ManagedRootListInput, True, False, ["managed_roots"], _managed_root_list_handler(db)),
        _tool("managed-file-list", "List server managed files by logical metadata filters.", ManagedFileListInput, True, False, ["managed_roots", "managed_files", "filesystem_scan_runs"], _managed_file_list_handler(db), output_model=ManagedFileCollectionToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("managed-file-search", "Search server managed files by filename keyword.", ManagedFileSearchInput, True, False, ["managed_roots", "managed_files", "filesystem_scan_runs"], _managed_file_search_handler(db), output_model=ManagedFileCollectionToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("managed-file-read-document", "Read one server managed file by logical filters, snapshot it as a document, and extract text.", ManagedFileReadDocumentInput, True, False, ["documents", "file_objects", "document_extraction_runs", "document_pages"], _managed_file_read_document_handler(db, user_id), output_model=ManagedFileReadToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("classify-managed-files", "Snapshot, extract and classify files selected from a server managed directory.", ManagedFileClassificationInput, True, False, ["documents", "file_objects", "document_extraction_runs", "document_pages", "document_classification_runs", "document_category_suggestions", "change_sets", "change_items"], _managed_file_classification_handler(db, user_id), output_model=ManagedFileReadToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("generate-rename-suggestions", "Resolve uploaded attachments or managed-original scope to working copies, then persist controlled rename suggestions without changing original files.", GenerateRenameSuggestionsInput, True, False, ["document_pages", "operation_plans"], _generate_rename_suggestions_handler(db, user_id), output_model=OperationPlanToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("resolve-rename-reviews", "Resolve pending rename reviews from explicit user corrections and immediately execute a confirmed OperationPlan.", ResolveRenameReviewsInput, True, False, ["operation_plans", "operation_confirmations", "change_sets", "change_items"], _resolve_rename_reviews_handler(db, user_id)),
        _tool("classification-decision", "Accept, reject, or correct one backend-resolved classification suggestion and persist the formal shared-file relation.", ClassificationDecisionInput, True, False, ["document_category_feedback", "document_categories", "document_category_confirmation_sources", "change_sets", "change_items", "classification_graph_outbox"], _classification_decision_handler(db, user_id), output_model=ClassificationDecisionToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("working-copy-action-plan-create", "Create a controlled working-copy OperationPlan from conversation context without executing it.", WorkingCopyActionPlanInput, True, False, ["operation_plans", "working_copy_path_records"], _working_copy_action_plan_handler(db, user_id), output_model=OperationPlanToolOutput, adaptive_ready=True, observation_policy="PLANNER_AFTER_EXECUTION"),
        _tool("managed-root-scan", "Create an async scan job for a managed logical root.", ManagedRootScanInput, True, False, ["filesystem_jobs", "filesystem_job_events"], _managed_root_scan_handler(db, user_id)),
        _tool("mcp-filesystem-list", "List files and directories in the server managed filesystem root without database scan.", MCPFilesystemListInput, False, False, [], _mcp_filesystem_list_handler()),
        _tool("mcp-filesystem-search", "Search files and directories in the server managed filesystem root without database scan.", MCPFilesystemSearchInput, False, False, [], _mcp_filesystem_search_handler()),
        _tool("mcp-filesystem-info", "Read metadata for one server managed filesystem path without database scan.", MCPFilesystemInfoInput, False, False, [], _mcp_filesystem_info_handler()),
        _tool(
            "analyze-spreadsheet",
            "Analyze an uploaded XLS/XLSX/XLSM/CSV/TSV spreadsheet through a validated read-only query plan.",
            SpreadsheetAnalysisInput,
            True,
            False,
            ["document_artifacts"],
            _analyze_spreadsheet_handler(
                db,
                user_id,
                conversation_id_getter,
                agent_run_id_getter,
            ),
            output_model=SpreadsheetToolOutput,
            adaptive_ready=True,
        ),
        _tool(
            "profile-spreadsheet",
            "Read spreadsheet workbook, sheet and column schema without modifying the original file.",
            SpreadsheetDocumentInput,
            True,
            False,
            ["document_artifacts"],
            _spreadsheet_workbench_handler(db, user_id, action="profile"),
            output_model=SpreadsheetToolOutput,
            adaptive_ready=True,
        ),
        _tool(
            "validate-spreadsheet",
            "Scan spreadsheet formula errors and structural warnings without modifying the original file.",
            SpreadsheetDocumentInput,
            True,
            False,
            ["document_artifacts"],
            _spreadsheet_workbench_handler(db, user_id, action="validation"),
            output_model=SpreadsheetToolOutput,
            adaptive_ready=True,
        ),
    ]
    return {tool.name: tool for tool in tools}
