"""File Agent MVP 统一 ORM 模型。

本文件集中声明文件版本、解析、索引、Agent 审计、工作副本和高风险操作等持久化事实；运行时服务对象
和文件正文不得写入 AgentGraphState 代替这些表。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType

from app.db.base import Base


def new_uuid() -> str:
    """生成字符串 UUID，兼容 SQLite 测试库和 PostgreSQL 目标库。"""

    return str(uuid4())


def utcnow() -> datetime:
    """生成带时区的 UTC 时间，统一审计时间字段。"""

    return datetime.now(timezone.utc)


class Vector1536(UserDefinedType):
    """声明 PostgreSQL ``vector(1536)`` 扩展列，同时允许 SQLite 测试使用 JSON 变体。

    第三阶段默认不写入向量；保留该类型只是为了让后续独立 GPU provider 可以异步回填，不能据此
    在应用进程启动模型推理。
    """

    cache_ok = True

    def get_col_spec(self, **_: object) -> str:
        """返回 pgvector 的固定维度声明，避免运行时任意修改索引维度。"""

        return "vector(1536)"


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
    artifacts: Mapped[List["DocumentArtifact"]] = relationship(back_populates="document")
    insights: Mapped[List["DocumentInsight"]] = relationship(back_populates="document")
    extraction_runs: Mapped[List["DocumentExtractionRun"]] = relationship(back_populates="document")
    pages: Mapped[List["DocumentPage"]] = relationship(back_populates="document")
    elements: Mapped[List["DocumentElement"]] = relationship(back_populates="document")
    index_runs: Mapped[List["DocumentIndexRun"]] = relationship(back_populates="document")
    chunks: Mapped[List["DocumentChunk"]] = relationship(back_populates="document")
    versions: Mapped[List["DocumentVersion"]] = relationship(
        back_populates="document",
        foreign_keys="DocumentVersion.document_id",
    )


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


class DocumentVersion(Base):
    """文档内容版本。

    上传暂存和工作副本共用版本表，但文件只能由 StorageService 通过相对路径定位；
    重命名或移动不得创建新版本。
    """

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_versions_document_number"),
        Index(
            "ix_document_versions_filename_trgm",
            "filename",
            postgresql_using="gin",
            postgresql_ops={"filename": "gin_trgm_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    working_copy_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("working_copies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    storage_tier: Mapped[str] = mapped_column(String(40), nullable=False, default="UPLOAD", index=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="UPLOAD", index=True)
    source_managed_file_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("managed_files.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    operation_plan_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("operation_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="versions", foreign_keys=[document_id])


class DocumentArtifact(Base):
    """由原始文档生成、可跨解析运行复用的文件派生件。"""

    __tablename__ = "document_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "artifact_type",
            "source_sha256",
            "converter_config_hash",
            name="uq_document_artifacts_source_config",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    storage_backend: Mapped[str] = mapped_column(String(40), nullable=False, default="local")
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    converter_name: Mapped[str] = mapped_column(String(80), nullable=False)
    converter_version: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    converter_config_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="artifacts")


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
    document_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=True, index=True
    )
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


class DocumentSummary(Base):
    """面向文档概览、文档级召回和问答路由的持久化普通文档摘要。

    摘要只用于缩小检索范围，不能替代 ``document_pages`` 原文证据。
    """

    __tablename__ = "document_summaries"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "extraction_run_id",
            "input_sha256",
            "model_provider",
            "model_name",
            "prompt_version",
            "schema_version",
            name="uq_document_summaries_cache_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    extraction_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_extraction_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    coverage_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model_provider: Mapped[str] = mapped_column(String(80), nullable=False, default="deterministic")
    model_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="COMPLETED", index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DocumentClassificationSummary(Base):
    """分类候选召回使用的结构化主题摘要，不代表正式分类关系。"""

    __tablename__ = "document_classification_summaries"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "extraction_run_id",
            "input_sha256",
            "model_provider",
            "model_name",
            "prompt_version",
            "schema_version",
            name="uq_document_classification_summaries_cache_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    extraction_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_extraction_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model_provider: Mapped[str] = mapped_column(String(80), nullable=False, default="deterministic")
    model_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="COMPLETED", index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


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


class DocumentIndexRun(Base):
    """一个文档内容版本在固定切分与分词配置下的原文索引运行。

    幂等键包含文档版本、解析运行和配置指纹；重命名、移动不会改变这些事实，因此不得重复建索引。
    """

    __tablename__ = "document_index_runs"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id",
            "extraction_run_id",
            "config_hash",
            name="uq_document_index_runs_version_extraction_config",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    extraction_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_extraction_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    index_version: Mapped[str] = mapped_column(String(80), nullable=False, default="chunk-index-v1")
    tokenizer: Mapped[str] = mapped_column(String(40), nullable=False, default="jieba")
    tokenizer_version: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="RUNNING", index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_status: Mapped[str] = mapped_column(String(40), nullable=False, default="DISABLED", index=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="index_runs")


class DocumentChunk(Base):
    """绑定不可变内容版本和真实定位信息的原文检索块。"""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("index_run_id", "chunk_index", name="uq_document_chunks_run_index"),
        Index("ix_document_chunks_search_vector_gin", "search_vector", postgresql_using="gin"),
        Index(
            "ix_document_chunks_search_text_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    index_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_index_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    extraction_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_extraction_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_type: Mapped[str] = mapped_column(String(40), nullable=False, default="text")
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    search_vector: Mapped[Optional[str]] = mapped_column(
        TSVECTOR().with_variant(Text(), "sqlite"), nullable=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    location_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sheet_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cell_range: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    element_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    embedding: Mapped[Optional[list]] = mapped_column(
        Vector1536().with_variant(JSON(), "sqlite"), nullable=True
    )
    embedding_status: Mapped[str] = mapped_column(String(40), nullable=False, default="DISABLED", index=True)
    embedding_provider: Mapped[str] = mapped_column(String(80), nullable=False, default="disabled")
    embedding_model: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    document: Mapped[Document] = relationship(back_populates="chunks")


class EvidenceSpan(Base):
    """从 Chunk 原文截取且带真实页码或单元格范围的可引用证据。"""

    __tablename__ = "evidence_spans"
    __table_args__ = (
        UniqueConstraint("chunk_id", "span_index", name="uq_evidence_spans_chunk_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    chunk_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    extraction_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_extraction_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    span_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_type: Mapped[str] = mapped_column(String(40), nullable=False, default="text_quote")
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sheet_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cell_range: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="document_chunk")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class QAAnswer(Base):
    """阶段五证据回答的持久化边界；阶段三只建表，不生成猜测性回答。"""

    __tablename__ = "qa_answers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_run_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="COMPLETED", index=True)
    retrieval_trace_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AnswerReference(Base):
    """回答与已校验证据之间的稳定引用关系。"""

    __tablename__ = "answer_references"
    __table_args__ = (
        UniqueConstraint("qa_answer_id", "reference_index", name="uq_answer_references_answer_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    qa_answer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("qa_answers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_span_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("evidence_spans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reference_index: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


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
    classification_summary_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("document_classification_summaries.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    classification_basis: Mapped[str] = mapped_column(
        String(40), nullable=False, default="FULL_TEXT", index=True
    )
    summary_status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="DISABLED", index=True
    )
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
    document_version_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", index=True)
    category_id: Mapped[str] = mapped_column(String(255), nullable=False, default="", index=True)
    category_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_path_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    taxonomy_key: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    taxonomy_version: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="SUGGESTED")
    evidence_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    candidate_scores_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    semantic_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
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
    corrected_category_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    corrected_category_path_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    supersedes_feedback_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("document_category_feedback.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, index=True)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GraphProjectionRun(Base):
    """Neo4j 可重建投影的一次运行记录。"""

    __tablename__ = "graph_projection_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    projection_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    scope_type: Mapped[str] = mapped_column(String(40), nullable=False, default="ALL")
    scope_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    projection_version: Mapped[str] = mapped_column(String(80), nullable=False, default="graph-v2")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="RUNNING", index=True)
    nodes_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relationships_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


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
    archive_write_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    allowed_operations_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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
    content_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    file_identity: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="DEPLOYED_FILE", index=True)
    source_upload_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=True, unique=True, index=True
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ACTIVE", index=True)
    last_seen_scan_run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class WorkingCopyRoot(Base):
    """工作副本目录映射。

    每个工作区和受管原始目录只有一个工作副本根，防止导入任务越过工作区边界。
    """

    __tablename__ = "working_copy_roots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "managed_root_id", name="uq_working_copy_roots_workspace_managed"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    managed_root_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("managed_roots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    root_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    relative_storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="INITIALIZING", index=True)
    last_imported_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class WorkingCopy(Base):
    """Agent 可操作的工作副本。

    `managed_file_id` 始终非空，保证任何增删改都能追溯到不可变原始文件。
    """

    __tablename__ = "working_copies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    working_copy_root_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("working_copy_roots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    managed_file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("managed_files.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    current_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    relative_path_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    extension: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    imported_source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    is_primary_import: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="IMPORTING", index=True)
    sync_status: Mapped[str] = mapped_column(String(40), nullable=False, default="SYNCED", index=True)
    last_operation_plan_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("operation_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class WorkingCopyPathRecord(Base):
    """工作副本不可变路径审计记录。"""

    __tablename__ = "working_copy_path_records"
    __table_args__ = (
        UniqueConstraint("working_copy_id", "sequence_number", name="uq_working_copy_path_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    working_copy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("working_copies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    before_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    after_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    before_filename: Mapped[str] = mapped_column(Text, nullable=False)
    after_filename: Mapped[str] = mapped_column(Text, nullable=False)
    document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_plan_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("operation_plans.id"), nullable=True, index=True)
    operation_confirmation_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("operation_confirmations.id"), nullable=True, index=True)
    agent_run_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("agent_runs.id"), nullable=True, index=True)
    tool_invocation_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tool_invocations.id"), nullable=True, index=True)
    changeset_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("change_sets.id"), nullable=True, index=True)
    change_item_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("change_items.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PLANNED", index=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    executed_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class TrashEntry(Base):
    """可恢复的工作副本删除或版本替换记录。"""

    __tablename__ = "trash_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False, index=True)
    working_copy_id: Mapped[str] = mapped_column(String(36), ForeignKey("working_copies.id"), nullable=False, index=True)
    document_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("document_versions.id"), nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    original_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    trash_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ACTIVE", index=True)
    operation_plan_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("operation_plans.id"), nullable=True, index=True)
    deleted_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    restored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    purged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ManagedFileEvent(Base):
    """watcher 记录的轻量文件系统事件；回调自身不得执行批量业务逻辑。"""

    __tablename__ = "managed_file_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    root_id: Mapped[str] = mapped_column(String(36), ForeignKey("managed_roots.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    target_relative_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    observed_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    observed_mtime: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    origin: Mapped[str] = mapped_column(String(40), nullable=False, default="EXTERNAL", index=True)
    deduplication_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PENDING", index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


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
    rename_batch_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("file_rename_batches.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    rename_batch_item_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("file_rename_batch_items.id", ondelete="CASCADE"),
        nullable=True,
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


class FileRenameBatch(Base):
    """一次对话确定的不可混用文件重命名范围。"""

    __tablename__ = "file_rename_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_plan_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("operation_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ANALYZING", index=True)
    scope_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ready_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    needs_review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class FileRenameBatchItem(Base):
    """重命名批次中的单个文件、建议名称和用户决策。"""

    __tablename__ = "file_rename_batch_items"
    __table_args__ = (
        UniqueConstraint("rename_batch_id", "managed_file_id", name="uq_file_rename_batch_file"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    rename_batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_rename_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    managed_file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("managed_files.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    root_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    original_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    proposed_relative_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    proposed_filename: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="NEEDS_REVIEW", index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    decision_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class UploadArchiveRecord(Base):
    """上传附件从暂存区归档为原始文件的状态机。"""

    __tablename__ = "upload_archive_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    upload_document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    managed_root_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("managed_roots.id", ondelete="SET NULL"), nullable=True, index=True
    )
    managed_file_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("managed_files.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    archive_relative_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="DUPLICATE_CHECK_PENDING", index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    filesystem_job_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("filesystem_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    changeset_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("change_sets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    risk_assessment_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class UploadDuplicateReview(Base):
    """重复上传的逐文件对话确认记录。

    该确认不是 OperationPlan，但必须绑定确定的上传版本和用户，不能让 LLM 猜测对象。
    """

    __tablename__ = "upload_duplicate_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    upload_document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="CHECKING", index=True)
    decision: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    selected_existing_working_copy_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("working_copies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notification_message_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    confirmation_message_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    duplicate_check_job_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("filesystem_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class UploadDuplicateCandidate(Base):
    """精确重复或近似重复候选；跨用户候选对外必须脱敏。"""

    __tablename__ = "upload_duplicate_candidates"
    __table_args__ = (
        UniqueConstraint(
            "duplicate_review_id",
            "candidate_managed_file_id",
            "candidate_working_copy_id",
            "match_type",
            name="uq_upload_duplicate_candidate_target",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    duplicate_review_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("upload_duplicate_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_managed_file_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("managed_files.id", ondelete="SET NULL"), nullable=True, index=True
    )
    candidate_working_copy_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("working_copies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    match_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    match_scope: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    similarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    match_evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    user_visible_summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FilesystemJob(Base):
    """文件系统异步任务表。

    扫描和未来确认后的文件操作都先进入任务队列，聊天请求不直接遍历目录。
    """

    __tablename__ = "filesystem_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    queue_name: Mapped[str] = mapped_column(String(40), nullable=False, default="RECONCILE", index=True)
    deduplication_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, unique=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    root_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("managed_roots.id", ondelete="CASCADE"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PENDING", index=True)
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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
