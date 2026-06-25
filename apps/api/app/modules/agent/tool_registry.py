"""File Agent Runtime 的 Tool 白名单与分发层。

Planner 输出永远不能直接调用 Tool handler，必须经过这里的 Registry。
这样未知 Tool 和非法输入会在副作用发生前被拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Type

from pydantic import BaseModel, ValidationError

from app.modules.agent.state import ToolInvocationRecord
from app.modules.agent.tool_schemas import (
    ChangeReportInput,
    ConfirmedFileActionInput,
    DocumentLineageReadInput,
    DocumentToolInput,
    EvidenceAnswerInput,
    FeedbackRecordInput,
    JobStatusReadInput,
    OperationPlanCreateInput,
    SearchToolInput,
    ToolInputValidationError,
)


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

    def __init__(self) -> None:
        self._tools = _build_mvp_tools()

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
        try:
            tool_input = tool.input_model.model_validate(input_json)
        except ValidationError as exc:
            raise ToolInputValidationError(str(exc)) from exc

        output = tool.handler(tool_input)
        return ToolInvocationRecord(
            tool_name=name,
            input_json=tool_input.model_dump(),
            output_json=output,
            status="COMPLETED",
            changeset_id=output.get("changeset_id"),
            operation_plan_id=output.get("operation_plan_id"),
        )


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


def _build_mvp_tools() -> Dict[str, ToolDefinition]:
    """创建 agent.md 要求的完整 MVP Tool 目录。"""

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
