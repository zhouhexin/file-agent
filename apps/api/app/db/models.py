"""File Agent MVP 运行时 ORM 模型。

本文件只包含当前持久化闭环需要的表：用户、工作区、会话、消息、AgentRun 和 ToolInvocation。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
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
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    display_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
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
    changeset_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    operation_plan_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    agent_run: Mapped[AgentRun] = relationship(back_populates="tool_invocations")
