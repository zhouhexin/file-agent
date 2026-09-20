"""新增 Demo 写入的最小 LangGraph dispatch 与审计边界，不接入旧 Planner。"""

from typing import TypedDict
from uuid import uuid5, NAMESPACE_URL

from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import IntegrityError

from app.core.logging import log_event
from app.db.models import ChangeSet, ChangeItem, ToolInvocation, utcnow
from app.modules.agent.repository import AgentRunRepository
from app.modules.conversations.repository import ConversationRepository
from .schemas import DatasetCreate, DatasetAppend
from .service import StructuredDatasetService


class DispatchState(TypedDict):
    """图状态只有轻量运行结果，正文、服务与输入候选留在请求作用域。"""
    status: str


def execute_dataset_write(*, db, user, operation, payload=None, dataset_id=None):
    """固定白名单、schema 和事务保存点保护业务写入；失败也记录真实审计。"""
    schemas = {"create": DatasetCreate, "append": DatasetAppend, "export": None}
    if operation not in schemas:
        raise ValueError("未知数据集写操作")
    if schemas[operation] is not None:
        payload = schemas[operation].model_validate(payload)
    conversation_repo = ConversationRepository(db)
    # 按用户复用内部审计会话，不为每次追加产生新的聊天会话。
    conversation_id = str(uuid5(NAMESPACE_URL, f"file-agent:dataset-audit:{user.id}"))
    try:
        with db.begin_nested():
            conversation = conversation_repo.ensure_conversation(conversation_id, user.id)
    except IntegrityError:
        # 同用户并发首用时只复用已提交会话，不回滚其他业务事务。
        conversation = conversation_repo.ensure_conversation(conversation_id, user.id)
    conversation.workspace_id = user.default_workspace_id
    message = conversation_repo.create_user_message(conversation_id, user.id,
        f"共享表格 Demo：{operation}", attachments=[])
    message.role = "SYSTEM_AUDIT"
    run = AgentRunRepository(db).create_run(conversation_id, message.id, user.id)
    run.intent = "STRUCTURED_DATASET"
    run.status = "RUNNING_TOOL"
    tool_name = f"structured_dataset_{operation}"
    input_summary = {"dataset_id": dataset_id}
    if operation == "append":
        input_summary.update(document_id=str(payload.document_id),
            document_version_id=str(payload.document_version_id), record_count=len(payload.records))
    invocation = ToolInvocation(agent_run_id=run.id, tool_name=tool_name,
        input_json=input_summary, output_json={}, status="RUNNING")
    db.add(invocation)
    db.flush()
    service = StructuredDatasetService(db, user, run)
    result = {}
    error = None

    def dispatch(state: DispatchState):
        """所有业务副作用集中在此节点；固定 handler 不执行客户端代码。"""
        nonlocal result
        handlers = {"create": lambda: service.create(payload),
                    "append": lambda: service.append(dataset_id, payload),
                    "export": lambda: service.export(dataset_id)}
        result = handlers[operation]()
        return {"status": "COMPLETED"}

    graph = StateGraph(DispatchState)
    graph.add_node("tool-dispatch", dispatch)
    graph.add_edge(START, "tool-dispatch")
    graph.add_edge("tool-dispatch", END)
    try:
        with db.begin_nested():
            graph.compile().invoke({"status": "RUNNING_TOOL"})
            safe_result = {key: result[key] for key in (
                "dataset_id", "revision", "record_count", "image_count", "appended_revision",
                "review_field_count", "export_id", "reused", "original_unchanged",
            ) if key in result}
            changeset_id = result.get("changeset_id")
            if not changeset_id:
                changeset = ChangeSet(agent_run_id=run.id, conversation_id=conversation_id,
                    user_id=user.id, status="COMPLETED", summary=f"共享表格 {operation} 已完成")
                db.add(changeset)
                db.flush()
                changeset_id = changeset.id
                db.add(ChangeItem(changeset_id=changeset.id, target_type="DATASET_EXPORT" if operation == "export" else "DATASET",
                    target_id=result.get("export_id", result["dataset_id"]),
                    change_type=("EXPORT_REUSED" if result.get("reused") else "EXPORT_CREATED") if operation == "export"
                        else "STRUCTURED_DATASET_REUSED" if result.get("reused")
                        else f"STRUCTURED_DATASET_{operation.upper()}",
                    after_value_json=safe_result, source="workbuddy-dataset-demo", execution_status="COMPLETED"))
            run.changeset_id = changeset_id
            invocation.changeset_id = changeset_id
            invocation.output_json = safe_result
    except HTTPException as exc:
        error = exc
    except IntegrityError:
        error = HTTPException(409, detail={"code": "DATASET_CONCURRENT_WRITE", "message": "并发写入冲突，请使用原提交键重试。"})
    except Exception as exc:
        log_event(
            "structured_dataset.write.exception",
            level="ERROR",
            agent_run_id=run.id,
            user_id=user.id,
            tool_name=tool_name,
            status="FAILED",
            error_code="DATASET_WRITE_FAILED",
            exception_type=type(exc).__name__,
        )
        error = HTTPException(500, detail={"code": "DATASET_WRITE_FAILED", "message": "本次操作失败，已保存记录不受影响，可重试。"})
    run.status = invocation.status = "FAILED" if error else "COMPLETED"
    run.graph_state_json = {"status": run.status}
    run.final_response = "表格操作失败。" if error else "共享表格操作已完成。"
    if error:
        invocation.output_json = {"error": error.detail}
        run.error_message = str(error.detail.get("code", "DATASET_WRITE_FAILED")) if isinstance(error.detail, dict) else "DATASET_WRITE_FAILED"
    invocation.finished_at = utcnow()
    db.commit()
    log_event("structured_dataset.write", agent_run_id=run.id, user_id=user.id,
              tool_name=tool_name, status=run.status, error_code=run.error_message)
    if error:
        raise error
    return result
