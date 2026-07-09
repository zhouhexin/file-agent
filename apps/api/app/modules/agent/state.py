"""LangGraph Agent Runtime 的共享状态对象。

图状态必须显式定义，Planner 输出和 Tool 结果只能通过命名字段流转，
不能把任意 LLM 生成内容直接透传给 Tool。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentGraphState(TypedDict, total=False):
    """在 Agent Runtime 各节点之间传递的 LangGraph 状态。"""

    agent_run_id: str
    conversation_id: str
    user_id: str
    message_id: str
    message: str
    attachments: List[Dict[str, Any]]
    context_documents: List[Dict[str, Any]]
    user_intent_plan: Dict[str, Any]
    planner_mode: str
    status: str
    intent: Optional[str]
    slots: Dict[str, Any]
    selected_skills: List[str]
    tool_plan: Dict[str, Any]
    tool_results: List[Dict[str, Any]]
    tool_invocations: List[Dict[str, Any]]
    result_summary: Dict[str, Any]
    document_results: List[Dict[str, Any]]
    changeset_id: Optional[str]
    operation_plan_id: Optional[str]
    final_response: Optional[str]
    errors: List[str]


class ToolInvocationRecord(BaseModel):
    """一次 Tool 调用的内存态记录。

    后续数据库表会映射到这个结构；现在先保持结构化，可以避免 Tool 输出退化成不可审计的自由文本。
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    tool_name: str
    input_json: Dict[str, Any]
    output_json: Dict[str, Any]
    status: str
    changeset_id: Optional[str] = None
    operation_plan_id: Optional[str] = None


class AgentRunResult(BaseModel):
    """内存态 Agent Runtime 对外返回的运行结果。"""

    agent_run_id: str
    conversation_id: str
    user_id: str
    message_id: str
    intent: Optional[str]
    status: str
    selected_skills: List[str]
    tool_plan: Dict[str, Any]
    tool_results: List[Dict[str, Any]]
    tool_invocations: List[ToolInvocationRecord]

    document_results: List[Dict[str, Any]] = Field(default_factory=list)

    changeset_id: Optional[str] = None
    operation_plan_id: Optional[str] = None
    final_response: Optional[str] = None
    errors: List[str] = Field(default_factory=list)
