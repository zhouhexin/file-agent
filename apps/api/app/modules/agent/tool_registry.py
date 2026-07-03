"""File Agent Runtime 的 Tool 白名单与分发层。

Planner 输出永远不能直接调用 Tool handler，必须经过这里的 Registry。
这样未知 Tool 和非法输入会在副作用发生前被拒绝。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Type

from pydantic import BaseModel, ValidationError

from app.core.logging import log_event
from app.db.models import Document, DocumentCategorySuggestion, DocumentClassificationRun, DocumentInsight
from app.modules.agent.capabilities.service import load_agent_capabilities
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
    IntentSummaryInput,
    JobStatusReadInput,
    OperationPlanCreateInput,
    SearchToolInput,
    ToolInputValidationError,
)
from app.modules.classification.taxonomy_service import read_default_taxonomy_catalog
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.extractors import extract_document_text


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

    if output.get("ok") is False or output.get("status") == "FAILED":
        return "FAILED"
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
            "changeset_id": f"changeset-{document_id}",
            "summary": f"{tool_name} completed for {document_id}",
        }

    return handler


def _search_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """在真实混合检索接入前返回空检索结果。"""

    return {
        "ok": True,
        "results": [],
        "query": getattr(tool_input, "query"),
    }


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
        "changeset_id": getattr(tool_input, "changeset_id") or "changeset-memory",
        "document_id": getattr(tool_input, "document_id"),
        "items": [],
    }


def _operation_plan_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """创建 PLANNED 状态的 OperationPlan 占位结果，不执行动作。"""

    return {
        "ok": True,
        "operation_plan_id": "operation-plan-memory",
        "status": "PLANNED",
        "operation_type": getattr(tool_input, "operation_type"),
    }


def _confirmed_action_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """返回已确认 OperationPlan 的执行占位结果。"""

    return {
        "ok": True,
        "operation_plan_id": getattr(tool_input, "operation_plan_id"),
        "status": "EXECUTED",
        "changeset_id": "changeset-confirmed-action",
    }


def _feedback_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """返回反馈持久化的占位结果。"""

    return {
        "ok": True,
        "target_type": getattr(tool_input, "target_type"),
        "target_id": getattr(tool_input, "target_id"),
    }


def _job_status_handler(tool_input: BaseModel) -> Dict[str, Any]:
    """返回异步任务状态占位结果。"""

    return {"ok": True, "job_id": getattr(tool_input, "job_id"), "status": "PENDING"}


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
        resolved = repository.resolve_original_file(document_id)
        if not resolved["ok"]:
            error = resolved.get("error") or {}
            log_event(
                "file.extract.failed",
                level="ERROR",
                document_id=document_id,
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=error.get("code"),
                message=error.get("message"),
            )
            return resolved

        document = resolved["document"]
        force_reprocess = bool(getattr(tool_input, "force_reprocess", False))
        reusable = None if force_reprocess else repository.get_latest_successful_extraction(document_id=document.id)
        if reusable is not None:
            run = reusable["run"]
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
                "pages": [
                    {
                        "page_number": page.page_number,
                        "sheet_name": page.sheet_name,
                        "text_preview": page.text_content[:300],
                        "char_count": len(page.text_content),
                    }
                    for page in reusable["pages"]
                ],
                "error": None,
            }

        extraction = extract_document_text(
            file_path=resolved["file_path"],
            filename=document.original_filename,
            content_type=document.content_type,
        )
        run = repository.create_extraction_run(document_id=document.id, extractor=extraction["extractor"])
        if extraction["ok"]:
            repository.complete_extraction_run(run=run, pages=extraction["pages"])
        else:
            repository.fail_extraction_run(run=run, error_message=extraction["error"]["message"])
        extraction_status = "COMPLETED" if extraction["ok"] else "FAILED"
        event_name = "file.extract.completed" if extraction["ok"] else "file.extract.failed"
        error = extraction.get("error") or {}
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
            "pages": [
                {
                    "page_number": page.get("page_number"),
                    "sheet_name": page.get("sheet_name"),
                    "text_preview": page.get("text", "")[:300],
                    "char_count": len(page.get("text", "")),
                }
                for page in extraction["pages"]
            ],
            "error": extraction.get("error"),
        }

    return handler


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


def _build_mvp_tools(*, db: Any = None, user_id: str | None = None) -> Dict[str, ToolDefinition]:
    """创建 AGENTS.md 要求的完整 MVP Tool 目录。"""

    tools = [
        _tool("document-register-upload", "Register uploaded file as a document.", DocumentToolInput, True, False, ["documents", "document_versions"], _document_handler("document-register-upload")),
        _tool("security-scan", "Scan file metadata and MIME risk.", DocumentToolInput, True, False, ["processing_events"], _document_handler("security-scan")),
        _tool("document-convert", "Extract document text and structure through adapters.", DocumentToolInput, True, False, ["document_pages", "artifacts", "change_items"], _document_handler("document-convert")),
        _tool("table-extract", "Extract spreadsheet sheets and cells.", DocumentToolInput, True, False, ["document_pages", "artifacts"], _document_handler("table-extract")),
        _tool("artifact-write", "Write derivative artifact records.", DocumentToolInput, True, False, ["artifacts"], _document_handler("artifact-write")),
        _tool("chunk-build", "Build chunks and evidence spans.", DocumentToolInput, True, False, ["document_chunks", "evidence_spans"], _document_handler("chunk-build")),
        _tool("embedding-generate", "Generate and store embeddings.", DocumentToolInput, True, False, ["document_chunks.embedding"], _document_handler("embedding-generate")),
        _tool("metadata-extract", "Extract metadata candidates.", DocumentToolInput, True, False, ["documents.metadata"], _document_handler("metadata-extract")),
        _tool("multi-label-classify", "Generate multi-label classifications with evidence.", DocumentToolInput, True, False, ["document_categories"], _document_handler("multi-label-classify")),
        _tool("read-document-insights", "Read deterministic ingest insights for uploaded documents.", DocumentInsightsReadInput, False, False, [], _document_insights_handler(db, user_id)),
        _tool("read-document-classifications", "Read latest persisted classification suggestions for uploaded documents.", DocumentClassificationsReadInput, False, False, [], _document_classifications_handler(db, user_id)),
        _tool("read-original-file", "Read safe metadata for an uploaded original file.", DocumentToolInput, False, False, [], _read_original_file_handler(db, user_id)),
        _tool("extract-document-text", "Extract text from uploaded files and persist document pages.", DocumentToolInput, True, False, ["document_extraction_runs", "document_pages"], _extract_document_text_handler(db, user_id)),
        _tool("intent-summary", "Record LLM-understood user intent without side effects.", IntentSummaryInput, False, False, [], _intent_summary_handler),
        _tool("read-agent-capabilities", "Read fixed File Agent capability catalog.", AgentCapabilitiesReadInput, False, False, [], _agent_capabilities_handler),
        _tool("read-classification-taxonomy", "Read fixed classification taxonomy catalog.", ClassificationTaxonomyReadInput, False, False, [], _classification_taxonomy_handler),
        _tool("hybrid-search", "Run workspace hybrid retrieval.", SearchToolInput, False, False, [], _search_handler),
        _tool("evidence-answer", "Answer from retrieved evidence.", EvidenceAnswerInput, True, False, ["qa_answers", "answer_references"], _evidence_answer_handler),
        _tool("change-report", "Build per-file receipt from changes.", ChangeReportInput, True, False, ["change_sets"], _change_report_handler),
        _tool("operation-plan-create", "Create high-risk operation plan.", OperationPlanCreateInput, True, False, ["operation_plans"], _operation_plan_handler),
        _tool("confirmed-file-action", "Execute confirmed operation plan.", ConfirmedFileActionInput, True, True, ["change_items"], _confirmed_action_handler),
        _tool("feedback-record", "Record user feedback.", FeedbackRecordInput, True, False, ["feedback"], _feedback_handler),
        _tool("job-status-read", "Read processing job status.", JobStatusReadInput, False, False, [], _job_status_handler),
        _tool("document-lineage-read", "Read document lineage.", DocumentLineageReadInput, False, False, [], _lineage_handler),
    ]
    return {tool.name: tool for tool in tools}
