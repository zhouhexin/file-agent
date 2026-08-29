"""主分类目录和首次落位复核清单的公开响应结构。"""

from __future__ import annotations

from datetime import datetime

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
    """当前 taxonomy 分类树及待复核虚拟节点。"""

    taxonomy_key: str
    taxonomy_version: str
    total_active_files: int
    classified_file_count: int
    needs_review_file_count: int
    nodes: list[OrganizationTreeNodeResponse] = Field(default_factory=list)


class OrganizationFileItemResponse(BaseModel):
    """分类目录列表中的单个安全文件投影。"""

    working_copy_id: str
    document_id: str
    document_version_id: str
    filename: str
    relative_path: str
    size_bytes: int
    primary_category_id: str | None = None
    primary_category_path: list[str] = Field(default_factory=list)
    primary_category_status: str | None = None
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
    files: list[OrganizationFileItemResponse] = Field(default_factory=list)
