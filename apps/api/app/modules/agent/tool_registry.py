"""File Agent Runtime 的 Tool 白名单与分发层。

Planner 输出永远不能直接调用 Tool handler，必须经过这里的 Registry。
这样未知 Tool 和非法输入会在副作用发生前被拒绝。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Type

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.logging import log_event
from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentInsight,
    User,
    WorkingCopy,
)
from app.modules.agent.capabilities.service import load_agent_capabilities
from app.modules.agent.mcp_filesystem_bridge import MCPFilesystemError, get_mcp_filesystem
from app.modules.agent.state import ToolInvocationRecord
from app.modules.agent.tool_schemas import (
    AgentCapabilitiesReadInput,
    ChangeReportInput,
    ClassificationTaxonomyReadInput,
    ConfirmedFileActionInput,
    DocumentClassificationsReadInput,
    DocumentInsightsReadInput,
    DocumentLineageReadInput,
    DocumentToolInput,
    EvidenceAnswerInput,
    FeedbackRecordInput,
    GenerateRenameSuggestionsInput,
    IntentSummaryInput,
    JobStatusReadInput,
    ManagedFileClassificationInput,
    ManagedFileListInput,
    ManagedFileReadDocumentInput,
    ManagedFileSearchInput,
    ManagedRootListInput,
    ManagedRootScanInput,
    MCPFilesystemInfoInput,
    MCPFilesystemListInput,
    MCPFilesystemSearchInput,
    OperationPlanCreateInput,
    ResolveRenameReviewsInput,
    SearchToolInput,
    SpreadsheetAnalysisInput,
    SpreadsheetDocumentInput,
    ToolInputValidationError,
)
from app.modules.classification.taxonomy_service import read_default_taxonomy_catalog
from app.modules.chunks.service import DocumentIndexService
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.extractors import extract_document_text, extraction_config_hash
from app.modules.files.readable_source import ReadableDocumentSourceResolver, apply_readable_source_metadata
from app.modules.file_rename.uploaded_suggestion_service import UploadedRenameSuggestionService
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
from app.modules.spreadsheet_workbench.service import SpreadsheetWorkbenchService


class UnknownToolError(ValueError):
    """Planner 引用了白名单外 Tool 时抛出。"""

    pass


ToolHandler = Callable[[BaseModel], Dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    """Tool 的声明式元数据，以及 Registry 调用的 handler。"""

    name: str
    description: str
    input_model: Type[BaseModel]
    side_effects: bool
    requires_confirmation: bool
    allowed_roles: List[str]
    writes: List[str]
    failure_strategy: str
    handler: ToolHandler

    def catalog_item(self) -> Dict[str, Any]:
        """返回可安全暴露给 Tool catalog 接口的元数据。"""

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": {"type": "object"},
            "side_effects": self.side_effects,
            "requires_confirmation": self.requires_confirmation,
            "allowed_roles": self.allowed_roles,
            "writes": self.writes,
            "failure_strategy": self.failure_strategy,
        }


class ToolRegistry:
    """内存态 MVP Tool Registry。

    Registry 是 Tool 名称、输入 schema、确认标记和副作用元数据的运行时强制边界。
    """

    def __init__(self, *, db: Any = None, user_id: str | None = None) -> None:
        """保存运行时上下文，并创建当前请求可用的 Tool 白名单。"""

        self.db = db
        self.user_id = user_id
        self._tools = _build_mvp_tools(db=db, user_id=user_id)

    def list_tools(self) -> List[Dict[str, Any]]:
        """返回全部白名单 Tool，供管理和调试查看。"""

        return [tool.catalog_item() for tool in self._tools.values()]

    def get(self, name: str) -> ToolDefinition:
        """获取白名单 Tool；如果 Planner 引用未知 Tool 则拒绝。"""

        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(f"Unknown tool: {name}") from exc

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

    if output.get("status") == "PENDING":
        return "PENDING"
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


def _document_handler(tool_name: str) -> ToolHandler:
    """为文档范围内的副作用 Tool 创建占位 handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """返回结构化占位输出，不触碰真实文件。"""

        document_id = getattr(tool_input, "document_id")
        return {
            "ok": True,
            "tool_name": tool_name,
            "document_id": document_id,
            # "changeset_id": f"changeset-{document_id}",
            "changeset_id": None,
            "summary": f"{tool_name} completed for {document_id}",
        }

    return handler


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


def _embedding_generate_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """明确拒绝阶段三的真实向量推理，避免占位 Tool 伪造已生成 embedding。"""

    settings = get_settings()
    code = "EMBEDDING_DISABLED" if not settings.embedding_enabled else "EMBEDDING_PROVIDER_NOT_IMPLEMENTED"
    message = (
        "当前部署使用 CPU 词法检索，embedding 已关闭。"
        if code == "EMBEDDING_DISABLED"
        else "向量 provider 尚未接入，未写入任何 embedding。"
    )
    return {
        "ok": False,
        "status": "FAILED",
        "document_id": str(getattr(tool_input, "document_id")),
        "error": {"code": code, "message": message},
    }


def _search_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建摘要优先的工作副本文档级检索 handler。

    当前实现完成文档级候选召回；原文 Chunk 级混合检索接入后仍必须保留这层路由，
    且 evidence-answer 不得把摘要当成最终事实证据。
    """

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """在当前用户边界内按最终文件名、分类和持久化摘要检索。"""

        if db is None or user_id is None:
            return {
                "kind": "workspace_file_search",
                "ok": False,
                "query": getattr(tool_input, "query"),
                "results": [],
                "error": {"code": "RUNTIME_CONTEXT_REQUIRED", "message": "检索上下文不可用"},
            }
        return WorkingCopySummarySearchService(db=db, user_id=user_id).search(
            query=getattr(tool_input, "query"),
            document_ids=list(getattr(tool_input, "document_ids", [])),
        )

    return handler


def _evidence_answer_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """用稳定 schema 返回无证据回答占位结果。"""

    return {
        "ok": True,
        "answer": "No evidence has been indexed yet.",
        "references": [],
        "question": getattr(tool_input, "question"),
    }


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

        documents = (
            db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
            if document_ids
            else []
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
                    "filename": document.original_filename,
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
    """创建读取历史分类建议的 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """按当前用户读取文件最近一次分类建议。"""

        document_ids = list(getattr(tool_input, "document_ids"))
        if db is None or user_id is None:
            return {"ok": True, "documents": []}

        documents = (
            db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
            if document_ids
            else []
        )
        document_lookup = {document.id: document for document in documents}
        if not document_lookup:
            return {"ok": True, "documents": []}

        runs = (
            db.query(DocumentClassificationRun)
            .filter(DocumentClassificationRun.document_id.in_(document_lookup.keys()))
            .order_by(DocumentClassificationRun.created_at.desc(), DocumentClassificationRun.id.desc())
            .all()
        )
        latest_run_by_document_id: Dict[str, DocumentClassificationRun] = {}
        for run in runs:
            latest_run_by_document_id.setdefault(run.document_id, run)

        run_ids = [run.id for run in latest_run_by_document_id.values()]
        suggestions = (
            db.query(DocumentCategorySuggestion)
            .filter(DocumentCategorySuggestion.classification_run_id.in_(run_ids))
            .order_by(DocumentCategorySuggestion.rank.asc(), DocumentCategorySuggestion.confidence.desc())
            .all()
            if run_ids
            else []
        )
        suggestions_by_run_id: Dict[str, list[DocumentCategorySuggestion]] = {}
        for suggestion in suggestions:
            suggestions_by_run_id.setdefault(suggestion.classification_run_id, []).append(suggestion)

        return {
            "ok": True,
            "documents": [
                {
                    "document_id": document_id,
                    "filename": document_lookup[document_id].original_filename,
                    "categories": [
                        {
                            "name": suggestion.category_name,
                            "confidence": suggestion.confidence,
                            "status": suggestion.status,
                            "source": suggestion.source,
                            "evidence": suggestion.evidence_json,
                        }
                        for suggestion in suggestions_by_run_id.get(run.id, [])
                    ],
                }
                for document_id, run in latest_run_by_document_id.items()
            ],
        }

    return handler


def _intent_summary_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """记录 LLM 已完成意图理解但不需要文件工具的结果。"""

    return {
        "ok": True,
        "intent": getattr(tool_input, "intent"),
        "user_goal": getattr(tool_input, "user_goal"),
    }


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


def _change_report_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """返回 ChangeSet 回执的占位结构。"""

    return {
        "ok": True,
        "changeset_id": None,
        "document_id": getattr(tool_input, "document_id"),
        "items": [],
        "receipt_status": "NOT_PERSISTED",
    }


def _operation_plan_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """创建 PLANNED 状态的 OperationPlan 占位结果，不执行动作。"""

    return {
        "ok": True,
        "operation_plan_id": "operation-plan-memory",
        "status": "PLANNED",
        "operation_type": getattr(tool_input, "operation_type"),
    }


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


def _job_status_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """返回异步任务状态占位结果。"""

    return {"ok": True, "job_id": getattr(tool_input, "job_id"), "status": "PENDING"}


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
        # 返回查询条件用于空结果回执，避免 response 节点无法说明是哪一个受管目录没有文件。
        query = {
            "root_key": scope.root_key,
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
        user = db.get(User, user_id)
        if user is None or not user.default_workspace_id:
            return _working_copy_scope_error(
                code="USER_WORKSPACE_REQUIRED",
                message="当前用户缺少默认工作区。",
            )
        managed_file_ids = [managed_file.id for managed_file, _root in rows]
        working_copies = (
            db.query(WorkingCopy)
            .join(Document, Document.id == WorkingCopy.document_id)
            .filter(
                WorkingCopy.managed_file_id.in_(managed_file_ids),
                WorkingCopy.workspace_id == user.default_workspace_id,
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
    """拒绝旧受管原始文件待复核链路，避免重新创建已退役计划。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """提示重新选择工作副本生成计划，不执行历史原地重命名。"""

        if db is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "DB_REQUIRED", "message": "处理重命名更正需要数据库会话。"}}
        if user_id is None:
            return {"ok": False, "status": "FAILED", "error": {"code": "AUTH_REQUIRED", "message": "处理重命名更正需要当前用户。"}}
        return {
            "ok": False,
            "kind": "rename_review_resolution",
            "status": "FAILED",
            "error": {
                "code": "LEGACY_RENAME_REVIEW_RETIRED",
                "message": "旧待复核项已失效，请重新选择文件生成工作副本重命名计划。",
            },
        }

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


def _lineage_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """返回文档 lineage 占位结果。"""

    return {"ok": True, "document_id": getattr(tool_input, "document_id"), "lineage": []}


def _read_original_file_handler(db: Any, user_id: str | None) -> ToolHandler:
    """创建读取原始文件元信息的 Tool handler。"""

    def handler(tool_input: BaseModel) -> Dict[str, Any]:
        """返回当前用户文件的安全元信息，不返回本地路径和二进制内容。"""

        if db is None:
            return {"ok": False, "error": {"code": "DB_REQUIRED", "message": "读取原始文件需要数据库会话。"}}
        return FileExtractionRepository(db, user_id).get_original_file_metadata(getattr(tool_input, "document_id"))

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

        force_reprocess = bool(getattr(tool_input, "force_reprocess", False))
        force_reconvert = bool(getattr(tool_input, "force_reconvert", False))
        readable_source_resolver = ReadableDocumentSourceResolver(db=db)
        expected_parser_config_hash = readable_source_resolver.expected_parser_config_hash(document=document)
        reusable = (
            None
            if force_reprocess
            else repository.get_latest_successful_extraction(
                document_id=document.id,
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
                "conversion_reused": None,
                "conversion_source_format": persisted_metadata.get("source_format"),
                "conversion_parsed_format": persisted_metadata.get("parsed_format"),
                "conversion_converter": persisted_metadata.get("converter"),
                "conversion_converter_version": persisted_metadata.get("converter_version"),
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
            return _failed_extraction_output(document_id=document.id, error=error)

        readable_source = readable_source_resolver.resolve(
            document=document,
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
            "conversion_reused": extraction.get("conversion_reused"),
            "conversion_source_format": extraction.get("conversion_source_format"),
            "conversion_parsed_format": extraction.get("conversion_parsed_format"),
            "conversion_converter": extraction.get("conversion_converter"),
            "conversion_converter_version": extraction.get("conversion_converter_version"),
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
) -> ToolDefinition:
    """使用 MVP Tool 的共享默认值构造一个 ToolDefinition。"""

    return ToolDefinition(
        name=name,
        description=description,
        input_model=input_model,
        side_effects=side_effects,
        requires_confirmation=requires_confirmation,
        allowed_roles=["user", "ops", "admin"],
        writes=writes,
        failure_strategy="return structured error and record invocation",
        handler=handler,
    )

def _analyze_spreadsheet_handler(db: Any, user_id: str | None) -> ToolHandler:
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
            return {
                "kind": "spreadsheet_analysis",
                "ok": False,
                "status": "FAILED",
                "document_id": document_id,
                "error": resolved.get("error") or {
                    "code": "FILE_RESOLUTION_FAILED",
                    "message": "无法定位已授权的原始文件。",
                    "retryable": False,
                    "user_action_required": False,
                },
            }

        document = resolved["document"]
        return SpreadsheetAnalysisService().analyze(
            document_id=str(document.id),
            filename=str(document.original_filename),
            file_path=resolved["file_path"],
            question=str(getattr(tool_input, "question")),
        )

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
            return {
                "kind": f"spreadsheet_{action}",
                "ok": False,
                "status": "FAILED",
                "document_id": document_id,
                "error": resolved.get("error") or {
                    "code": "FILE_RESOLUTION_FAILED",
                    "message": "无法定位已授权的原始文件。",
                    "retryable": False,
                    "user_action_required": False,
                },
            }

        document = resolved["document"]
        service = SpreadsheetWorkbenchService()
        kwargs = {
            "document_id": str(document.id),
            "filename": str(document.original_filename),
            "file_path": resolved["file_path"],
        }
        if action == "profile":
            return service.profile(**kwargs)
        return service.validate(**kwargs)

    return handler


def _build_mvp_tools(*, db: Any = None, user_id: str | None = None) -> Dict[str, ToolDefinition]:
    """创建 AGENTS.md 要求的完整 MVP Tool 目录。"""

    tools = [
        _tool("document-register-upload", "Register uploaded file as a document.", DocumentToolInput, True, False, ["documents", "document_versions"], _document_handler("document-register-upload")),
        _tool("security-scan", "Scan file metadata and MIME risk.", DocumentToolInput, True, False, ["processing_events"], _document_handler("security-scan")),
        _tool("document-convert", "Extract document text and structure through adapters.", DocumentToolInput, True, False, ["document_pages", "artifacts", "change_items"], _document_handler("document-convert")),
        _tool("table-extract", "Extract spreadsheet sheets and cells.", DocumentToolInput, True, False, ["document_pages", "artifacts"], _document_handler("table-extract")),
        _tool("artifact-write", "Write derivative artifact records.", DocumentToolInput, True, False, ["artifacts"], _document_handler("artifact-write")),
        _tool("chunk-build", "Build chunks and evidence spans.", DocumentToolInput, True, False, ["document_chunks", "evidence_spans"], _chunk_build_handler(db, user_id)),
        _tool("embedding-generate", "Generate and store embeddings.", DocumentToolInput, True, False, ["document_chunks.embedding"], _embedding_generate_handler),
        _tool("metadata-extract", "Extract metadata candidates.", DocumentToolInput, True, False, ["documents.metadata"], _document_handler("metadata-extract")),
        _tool("multi-label-classify", "Generate multi-label classifications with evidence.", DocumentToolInput, True, False, ["document_categories"], _document_handler("multi-label-classify")),
        _tool("read-document-insights", "Read deterministic ingest insights for uploaded documents.", DocumentInsightsReadInput, False, False, [], _document_insights_handler(db, user_id)),
        _tool("read-document-classifications", "Read latest persisted classification suggestions for uploaded documents.", DocumentClassificationsReadInput, False, False, [], _document_classifications_handler(db, user_id)),
        _tool("read-original-file", "Read safe metadata for an uploaded original file.", DocumentToolInput, False, False, [], _read_original_file_handler(db, user_id)),
        _tool("extract-document-text", "Extract text from uploaded files and persist document pages.", DocumentToolInput, True, False, ["document_extraction_runs", "document_pages"], _extract_document_text_handler(db, user_id)),
        _tool("intent-summary", "Record LLM-understood user intent without side effects.", IntentSummaryInput, False, False, [], _intent_summary_handler),
        _tool("read-agent-capabilities", "Read fixed File Agent capability catalog.", AgentCapabilitiesReadInput, False, False, [], _agent_capabilities_handler),
        _tool("read-classification-taxonomy", "Read fixed classification taxonomy catalog.", ClassificationTaxonomyReadInput, False, False, [], _classification_taxonomy_handler),
        _tool("hybrid-search", "Run summary-first workspace retrieval.", SearchToolInput, False, False, [], _search_handler(db, user_id)),
        _tool("evidence-answer", "Answer from retrieved evidence.", EvidenceAnswerInput, True, False, ["qa_answers", "answer_references"], _evidence_answer_handler),
        _tool("change-report", "Build per-file receipt from changes.", ChangeReportInput, True, False, ["change_sets"], _change_report_handler),
        _tool("operation-plan-create", "Create high-risk operation plan.", OperationPlanCreateInput, True, False, ["operation_plans"], _operation_plan_handler),
        _tool("confirmed-file-action", "Execute confirmed operation plan.", ConfirmedFileActionInput, True, True, ["change_items"], _confirmed_action_handler(db, user_id)),
        _tool("feedback-record", "Record user feedback.", FeedbackRecordInput, True, False, ["feedback", "skill_feedback_samples"], _feedback_handler(user_id)),
        _tool("job-status-read", "Read processing job status.", JobStatusReadInput, False, False, [], _job_status_handler),
        _tool("document-lineage-read", "Read document lineage.", DocumentLineageReadInput, False, False, [], _lineage_handler),
        _tool("managed-root-list", "List server managed logical roots.", ManagedRootListInput, True, False, ["managed_roots"], _managed_root_list_handler(db)),
        _tool("managed-file-list", "List server managed files by logical metadata filters.", ManagedFileListInput, True, False, ["managed_roots", "managed_files", "filesystem_scan_runs"], _managed_file_list_handler(db)),
        _tool("managed-file-search", "Search server managed files by filename keyword.", ManagedFileSearchInput, True, False, ["managed_roots", "managed_files", "filesystem_scan_runs"], _managed_file_search_handler(db)),
        _tool("managed-file-read-document", "Read one server managed file by logical filters, snapshot it as a document, and extract text.", ManagedFileReadDocumentInput, True, False, ["documents", "file_objects", "document_extraction_runs", "document_pages"], _managed_file_read_document_handler(db, user_id)),
        _tool("classify-managed-files", "Snapshot, extract and classify files selected from a server managed directory.", ManagedFileClassificationInput, True, False, ["documents", "file_objects", "document_extraction_runs", "document_pages", "document_classification_runs", "document_category_suggestions", "change_sets", "change_items"], _managed_file_classification_handler(db, user_id)),
        _tool("generate-rename-suggestions", "Resolve uploaded attachments or managed-original scope to working copies, then persist controlled rename suggestions without changing original files.", GenerateRenameSuggestionsInput, True, False, ["document_pages", "operation_plans"], _generate_rename_suggestions_handler(db, user_id)),
        _tool("resolve-rename-reviews", "Resolve pending rename reviews from explicit user corrections and immediately execute a confirmed OperationPlan.", ResolveRenameReviewsInput, True, False, ["operation_plans", "operation_confirmations", "change_sets", "change_items"], _resolve_rename_reviews_handler(db, user_id)),
        _tool("managed-root-scan", "Create an async scan job for a managed logical root.", ManagedRootScanInput, True, False, ["filesystem_jobs", "filesystem_job_events"], _managed_root_scan_handler(db, user_id)),
        _tool("mcp-filesystem-list", "List files and directories in the server managed filesystem root without database scan.", MCPFilesystemListInput, False, False, [], _mcp_filesystem_list_handler()),
        _tool("mcp-filesystem-search", "Search files and directories in the server managed filesystem root without database scan.", MCPFilesystemSearchInput, False, False, [], _mcp_filesystem_search_handler()),
        _tool("mcp-filesystem-info", "Read metadata for one server managed filesystem path without database scan.", MCPFilesystemInfoInput, False, False, [], _mcp_filesystem_info_handler()),
        _tool(
            "analyze-spreadsheet",
            "Analyze an uploaded XLS/XLSX/XLSM/CSV/TSV spreadsheet through a validated read-only query plan.",
            SpreadsheetAnalysisInput,
            False,
            False,
            [],
            _analyze_spreadsheet_handler(db, user_id),
        ),
        _tool(
            "profile-spreadsheet",
            "Read spreadsheet workbook, sheet and column schema without modifying the original file.",
            SpreadsheetDocumentInput,
            False,
            False,
            [],
            _spreadsheet_workbench_handler(db, user_id, action="profile"),
        ),
        _tool(
            "validate-spreadsheet",
            "Scan spreadsheet formula errors and structural warnings without modifying the original file.",
            SpreadsheetDocumentInput,
            False,
            False,
            [],
            _spreadsheet_workbench_handler(db, user_id, action="validation"),
        ),
    ]
    return {tool.name: tool for tool in tools}
