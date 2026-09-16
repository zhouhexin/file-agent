"""主分类目录和首次落位复核清单的公开响应结构。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class OrganizationTreeNodeResponse(BaseModel):
    """一个可展开分类节点；计数只覆盖当前活动工作副本。"""

    category_id: str
    name: str
    category_path: list[str] = Field(default_factory=list)
    direct_file_count: int = 0
    subtree_file_count: int = 0
    is_virtual: bool = False
    children: list["OrganizationTreeNodeResponse"] = Field(default_factory=list)


class OrganizationTreeResponse(BaseModel):
    """schema v2 的当前 taxonomy 分类树及 OTHER 聚合计数。"""

    schema_version: int = 2
    taxonomy_key: str
    taxonomy_version: str
    total_active_files: int
    classified_file_count: int
    business_classified_file_count: int
    other_file_count: int
    # 兼容一个发布版本的旧客户端；schema v2 UI 不应使用该字段表达复核语义。
    needs_review_file_count: int = 0
    public_access_token: str | None = None
    nodes: list[OrganizationTreeNodeResponse] = Field(default_factory=list)


class OrganizationPrimaryResponse(BaseModel):
    """文件当前已生效或待执行的主分类投影，不伪造历史 fallback。"""

    category_id: str
    category_path: list[str] = Field(default_factory=list)
    status: str | None = None
    placement_operation_id: str | None = None


class OrganizationFileItemResponse(BaseModel):
    """分类目录列表中的单个安全文件投影。"""

    working_copy_id: str
    document_id: str
    document_version_id: str
    filename: str
    relative_path: str
    size_bytes: int
    preview_url: str | None = None
    download_url: str | None = None
    primary_category_id: str | None = None
    primary_category_path: list[str] = Field(default_factory=list)
    primary_category_status: str | None = None
    classification_outcome: str = "OTHER"
    placement_status: str = "PENDING"
    effective_primary: OrganizationPrimaryResponse | None = None
    pending_primary: OrganizationPrimaryResponse | None = None
    legacy_location: bool = False
    organization_decision: str | None = None
    organization_reason_codes: list[str] = Field(default_factory=list)
    updated_at: datetime


class OrganizationFilePageResponse(BaseModel):
    """服务端分页后的分类文件清单。"""

    page: int
    page_size: int
    total: int
    total_pages: int
    category_id: str | None = None
    scope: str
    review_only: bool
    deprecated_compatibility: bool = False
    public_access_token: str | None = None
    files: list[OrganizationFileItemResponse] = Field(default_factory=list)


class WorkingCopyClassificationItemResponse(BaseModel):
    """单个当前分类建议及其建议角色、正式角色和原文依据。"""

    suggestion_id: str | None = None
    category_id: str
    name: str
    category_path: list[str] = Field(default_factory=list)
    rank: int | None = None
    confidence: float | None = None
    status: str | None = None
    suggestion_status: str | None = None
    suggested_role: str | None = None
    effective_role: str | None = None
    effective_status: str | None = None
    source: str | None = None
    evidence_items: list[dict[str, Any]] = Field(default_factory=list)


class WorkingCopyClassificationsResponse(BaseModel):
    """WorkBuddy 按稳定工作副本 ID 读取的完整当前分类事实。"""

    working_copy_id: str
    document_id: str
    document_version_id: str | None = None
    filename: str
    classification_run_id: str | None = None
    taxonomy_key: str | None = None
    taxonomy_version: str | None = None
    classifier_version: str | None = None
    classification_basis: str | None = None
    summary_status: str | None = None
    classification_outcome: str = ""
    classification_quality: str = ""
    selection_basis: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    status: str
    error_code: str | None = None
    categories: list[WorkingCopyClassificationItemResponse] = Field(default_factory=list)
