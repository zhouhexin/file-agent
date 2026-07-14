"""File Agent MVP 运行时 ORM 模型。

本文件只包含当前持久化闭环需要的表：用户、工作区、会话、消息、AgentRun 和 ToolInvocation。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def new_uuid() -> str:
    """生成字符串 UUID，兼容 SQLite 测试库和 PostgreSQL 目标库。"""

    return str(uuid4())


def utcnow() -> datetime:
    """生成带时区的 UTC 时间，统一审计时间字段。"""

    return datetime.now(timezone.utc)


class User(Base):
    """系统用户表的最小 ORM 模型。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    display_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    default_workspace_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Workspace(Base):
    """默认工作区表的最小 ORM 模型。"""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Conversation(Base):
    """会话表。

    当前阶段允许占位 conversation 自动创建，后续接入认证和 workspace 后再收紧权限。
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    messages: Mapped[List["Message"]] = relationship(back_populates="conversation")
    agent_runs: Mapped[List["AgentRun"]] = relationship(back_populates="conversation")


class Message(Base):
    """会话消息表。"""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    attachments_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    agent_runs: Mapped[List["AgentRun"]] = relationship(back_populates="message")


class Document(Base):
    """用户上传文件的业务文档记录。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=True, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="UPLOADED")
    ingest_status: Mapped[str] = mapped_column(String(40), nullable=False, default="UPLOADED")
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_message_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    locked_conversation_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    file_objects: Mapped[List["FileObject"]] = relationship(back_populates="document")
    insights: Mapped[List["DocumentInsight"]] = relationship(back_populates="document")
    extraction_runs: Mapped[List["DocumentExtractionRun"]] = relationship(back_populates="document")
    pages: Mapped[List["DocumentPage"]] = relationship(back_populates="document")
    elements: Mapped[List["DocumentElement"]] = relationship(back_populates="document")


class FileObject(Base):
    """文件对象表，记录原始文件在存储系统中的位置。"""

    __tablename__ = "file_objects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    storage_backend: Mapped[str] = mapped_column(String(40), nullable=False, default="local")
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="file_objects")


class DocumentInsight(Base):
    """文件固定 ingest 产生的可复用基础洞察。"""

    __tablename__ = "document_insights"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, unique=True, index=True)
    keywords_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    labels_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="insights")


class DocumentExtractionRun(Base):
    """文件解析 Tool 的一次运行记录。"""

    __tablename__ = "document_extraction_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="RUNNING")
    extractor: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    parser_name: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    parser_config_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="extraction_runs")
    pages: Mapped[List["DocumentPage"]] = relationship(back_populates="extraction_run")
    elements: Mapped[List["DocumentElement"]] = relationship(back_populates="extraction_run")


class DocumentPage(Base):
    """文件解析后的页、sheet 或文本片段。"""

    __tablename__ = "document_pages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    extraction_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("document_extraction_runs.id"), nullable=False, index=True)
    page_number: Mapped[Optional[int]] = mapped_column(nullable=True)
    sheet_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    text_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="pages")
    extraction_run: Mapped[DocumentExtractionRun] = relationship(back_populates="pages")


class DocumentElement(Base):
    """Docling 等结构化解析器生成的可定位文档元素。"""

    __tablename__ = "document_elements"
    __table_args__ = (
        UniqueConstraint("extraction_run_id", "element_index", name="uq_document_elements_run_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    extraction_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("document_extraction_runs.id"),
        nullable=False,
        index=True,
    )
    element_index: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False, default="text")
    text_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    page_number: Mapped[Optional[int]] = mapped_column(nullable=True)
    bbox_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    content_layer: Mapped[str] = mapped_column(String(80), nullable=False, default="body")
    parent_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="elements")
    extraction_run: Mapped[DocumentExtractionRun] = relationship(back_populates="elements")


class DocumentClassificationRun(Base):
    """一次文件分类建议生成运行。

    当前保存规则分类结果的来源和版本，后续 ChangeSet、重处理和用户确认会引用该运行。
    """

    __tablename__ = "document_classification_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    taxonomy_key: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    taxonomy_version: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    classifier_version: Mapped[str] = mapped_column(String(80), nullable=False, default="taxonomy-rule-v1")
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="rule")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="COMPLETED")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DocumentCategorySuggestion(Base):
    """文件分类建议表。

    建议结果是 SUGGESTED 状态，不等同于用户确认后的正式 document_categories。
    """

    __tablename__ = "document_category_suggestions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    classification_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("document_classification_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_path_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    taxonomy_key: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    taxonomy_version: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="SUGGESTED")
    evidence_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="rule")
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DocumentCategoryFeedback(Base):
    """用户对分类建议的反馈记录。

    本阶段只建立持久化边界，前端确认/拒绝入口后续再接入。
    """

    __tablename__ = "document_category_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    suggestion_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("document_category_suggestions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentRun(Base):
    """AgentRun 表，记录 LangGraph 一次运行的审计状态。"""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(36), ForeignKey("messages.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    intent: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="RECEIVED")
    selected_skills_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    plan_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    graph_state_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    changeset_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("change_sets.id"), nullable=True, index=True)
    final_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="agent_runs")
    message: Mapped[Message] = relationship(back_populates="agent_runs")
    tool_invocations: Mapped[List["ToolInvocation"]] = relationship(back_populates="agent_run")


class ToolInvocation(Base):
    """ToolInvocation 表，记录每一次白名单 Tool 调用。"""

    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id"), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    input_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    changeset_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    operation_plan_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    agent_run: Mapped[AgentRun] = relationship(back_populates="tool_invocations")


class OperationPlan(Base):
    """高风险文件操作的待确认计划。"""

    __tablename__ = "operation_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_run_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="WAITING_CONFIRMATION")
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    plan_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class OperationConfirmation(Base):
    """用户确认 OperationPlan 的审计记录。"""

    __tablename__ = "operation_confirmations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    operation_plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("operation_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    confirmation_text: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ChangeSet(Base):
    """一次 AgentRun 产生的结构化变更集。

    ChangeSet 是文件智能体的审计结果，不是普通日志；它记录本次运行真实产生的解析、
    分类建议和失败结果。
    """

    __tablename__ = "change_sets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=True, index=True)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    agent_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="COMPLETED")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ChangeItem(Base):
    """ChangeSet 中的一条文件级变更明细。"""

    __tablename__ = "change_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    changeset_id: Mapped[str] = mapped_column(String(36), ForeignKey("change_sets.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    target_document_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("documents.id"), nullable=True, index=True)
    change_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    before_value_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    after_value_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    execution_status: Mapped[str] = mapped_column(String(40), nullable=False, default="COMPLETED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ManagedRoot(Base):
    """服务器受管目录表。

    该表只保存部署层已经授权的逻辑目录，业务层通过 root_key 访问目录，
    不把宿主机任意路径暴露给用户或 LLM。
    """

    __tablename__ = "managed_roots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    root_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    container_path: Mapped[str] = mapped_column(String(500), nullable=False)
    classification_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="NONE")
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    read_only: Mapped[bool] = mapped_column(default=True, nullable=False)
    allowed_operations_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ManagedFile(Base):
    """受管目录扫描得到的文件元数据。

    P0 只记录元数据，不读取正文、不移动、不删除、不覆盖真实文件。
    """

    __tablename__ = "managed_files"
    __table_args__ = (
        UniqueConstraint("root_id", "relative_path_hash", name="uq_managed_files_root_relative_path_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    root_id: Mapped[str] = mapped_column(String(36), ForeignKey("managed_roots.id", ondelete="CASCADE"), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    category_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    extension: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    modified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ACTIVE", index=True)
    last_seen_scan_run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ManagedFileSnapshot(Base):
    """用户读取受管文件时生成的不可变内容快照关系。"""

    __tablename__ = "managed_file_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "managed_file_id",
            "source_sha256",
            name="uq_managed_file_snapshots_user_file_sha256",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    managed_file_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("managed_files.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_modified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class FileRenameReviewItem(Base):
    """缺少自动重命名信息、等待用户更正名称的受管文件。"""

    __tablename__ = "file_rename_review_items"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "managed_file_id", name="uq_file_rename_review_run_file"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    managed_file_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("managed_files.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    root_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    original_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="NEEDS_REVIEW", index=True)
    review_context_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    decision_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class FilesystemJob(Base):
    """文件系统异步任务表。

    扫描和未来确认后的文件操作都先进入任务队列，聊天请求不直接遍历目录。
    """

    __tablename__ = "filesystem_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    root_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("managed_roots.id", ondelete="CASCADE"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PENDING", index=True)
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    locked_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class FilesystemJobEvent(Base):
    """文件系统任务过程事件表，用于记录扫描进度和错误。"""

    __tablename__ = "filesystem_job_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("filesystem_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="INFO")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FilesystemScanRun(Base):
    """单次受管目录扫描汇总表。"""

    __tablename__ = "filesystem_scan_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    root_id: Mapped[str] = mapped_column(String(36), ForeignKey("managed_roots.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("filesystem_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="RUNNING")
    files_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
