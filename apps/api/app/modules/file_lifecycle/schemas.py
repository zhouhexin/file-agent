"""三层文件生命周期 API schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DuplicateCandidateResponse(BaseModel):
    """对当前用户安全可见的重复候选摘要。"""

    id: str
    match_type: str
    match_scope: str
    similarity_score: float
    summary: dict[str, Any]
    existing_working_copy_id: str | None = None
    existing_document_id: str | None = None


class DuplicateReviewResponse(BaseModel):
    """重复上传确认卡数据。"""

    id: str
    upload_document_version_id: str
    document_id: str
    filename: str
    status: str
    decision: str | None
    expires_at: datetime
    candidates: list[DuplicateCandidateResponse]
    allowed_decisions: list[str]
    duplicate_check_job_id: str | None


class DuplicateDecisionRequest(BaseModel):
    """用户对确定重复确认记录作出的显式决策。"""

    model_config = ConfigDict(extra="forbid")

    duplicate_review_id: str = Field(min_length=1)
    decision: Literal["CONTINUE_UPLOAD", "USE_EXISTING_FILE", "CANCEL_UPLOAD"]
    selected_existing_working_copy_id: str | None = None

    @model_validator(mode="after")
    def validate_existing_selection(self) -> "DuplicateDecisionRequest":
        """使用已有文件时必须同时提交候选工作副本 ID。"""

        if self.decision == "USE_EXISTING_FILE" and not self.selected_existing_working_copy_id:
            raise ValueError("USE_EXISTING_FILE 必须选择已有工作副本")
        if self.decision != "USE_EXISTING_FILE" and self.selected_existing_working_copy_id:
            raise ValueError("只有 USE_EXISTING_FILE 可以提交已有工作副本")
        return self


class DuplicateDecisionResponse(BaseModel):
    """重复上传决策持久化结果。"""

    review: DuplicateReviewResponse
    archive_status: str
    filesystem_job_id: str | None
    selected_existing_document_id: str | None = None


class ArchiveStatusResponse(BaseModel):
    """上传附件归档和后续导入状态。"""

    upload_document_version_id: str
    status: str
    managed_file_id: str | None
    working_copy_id: str | None
    filesystem_job_id: str | None
    error_code: str | None
    error_message: str | None


class WorkingCopyResponse(BaseModel):
    """工作副本安全响应，不包含服务器绝对路径。"""

    id: str
    workspace_id: str
    managed_file_id: str
    document_id: str
    current_version_id: str | None
    root_key: str
    relative_path: str
    filename: str
    extension: str
    size_bytes: int
    content_sha256: str
    status: str
    sync_status: str
    created_at: datetime
    updated_at: datetime


class WorkingCopyLineageResponse(BaseModel):
    """工作副本到原始文件和当前版本的追溯关系。"""

    working_copy: WorkingCopyResponse
    managed_root_key: str
    managed_file_relative_path: str
    managed_file_source_type: str
    managed_file_status: str
    imported_source_sha256: str


class DocumentVersionResponse(BaseModel):
    """工作副本版本响应。"""

    id: str
    version_number: int
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    source_type: str
    created_at: datetime


class WorkingCopyPathRecordResponse(BaseModel):
    """工作副本路径审计响应。"""

    id: str
    sequence_number: int
    operation_type: str
    before_relative_path: str
    after_relative_path: str
    before_filename: str
    after_filename: str
    document_version_id: str
    content_sha256: str
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class TrashEntryResponse(BaseModel):
    """回收站条目响应。"""

    id: str
    working_copy_id: str
    document_version_id: str
    entry_type: str
    original_relative_path: str
    status: str
    deleted_at: datetime
    retention_until: datetime
    restored_at: datetime | None


class RestorePlanRequest(BaseModel):
    """从回收站创建恢复计划的请求。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=36)
