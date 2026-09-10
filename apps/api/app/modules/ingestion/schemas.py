"""批量导入 API 的严格输入输出 Schema。

调用方只能提交逻辑根引用和 POSIX 相对路径，不能把宿主机绝对路径传给后端。Schema 在进入
Repository 前冻结批次策略和清单快照，后续 LLM、MCP 或 worker 都不得自行扩展成员范围。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_LOGICAL_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _validate_logical_key(value: str, *, field_label: str) -> str:
    """校验不会被解释为路径或命令的逻辑标识。"""

    normalized = value.strip()
    if not _LOGICAL_KEY_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_label} 只能包含字母、数字、点、下划线和短横线")
    return normalized


def _normalize_relative_path(value: str, *, field_label: str) -> str:
    """把 MCP 相对路径规范为 POSIX 表达，并拒绝越界或平台绝对路径。"""

    normalized = value.strip()
    if not normalized or "\x00" in normalized or "\\" in normalized:
        raise ValueError(f"{field_label} 必须是使用正斜杠的非空相对路径")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        raise ValueError(f"{field_label} 不能是绝对路径或包含上级目录")
    # ``a/./b`` 需要收敛为稳定清单键，避免同一文件以多种文本形式重复登记。
    stable_parts = [part for part in path.parts if part != "."]
    return "/".join(stable_parts) or "."


class IngestBatchCreateRequest(BaseModel):
    """创建尚未 seal 的批次及冻结导入策略。"""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=200)
    source_root_ref: str = Field(min_length=1, max_length=100)
    relative_directory: str = Field(min_length=1, max_length=4096)
    recursive: bool = True
    ingest_policy: Literal["AUTO_ORGANIZE"] = "AUTO_ORGANIZE"
    placement_mode: Literal["BY_CATEGORY", "NEUTRAL"] = "BY_CATEGORY"
    rule_profile: Literal["content_based", "legacy_school_materials"] = "content_based"
    user_request: str | None = Field(default=None, max_length=20_000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=36)

    @field_validator("client_id", "source_root_ref")
    @classmethod
    def validate_logical_keys(cls, value: str, info) -> str:
        """限制客户端和根引用为逻辑标识，防止路径或 Shell 片段进入策略。"""

        return _validate_logical_key(value, field_label=info.field_name)

    @field_validator("request_id", "idempotency_key")
    @classmethod
    def trim_request_keys(cls, value: str) -> str:
        """去除观测和幂等标识两端空白，避免同一事件产生不同键。"""

        return value.strip()

    @field_validator("relative_directory")
    @classmethod
    def validate_relative_directory(cls, value: str) -> str:
        """要求用户已经在授权根内指定处理目录。"""

        return _normalize_relative_path(value, field_label="relative_directory")

    @field_validator("user_request")
    @classmethod
    def normalize_user_request(cls, value: str | None) -> str | None:
        """空白附带请求等价于没有额外任务，默认整理仍会执行。"""

        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class IngestItemCreate(BaseModel):
    """一个本地清单项的不可变来源快照。"""

    model_config = ConfigDict(extra="forbid")

    client_item_id: str = Field(min_length=1, max_length=160)
    source_root_ref: str = Field(min_length=1, max_length=100)
    source_relative_path: str = Field(min_length=1, max_length=4096)
    original_filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    expected_sha256: str | None = None

    @field_validator("client_item_id", "source_root_ref")
    @classmethod
    def validate_item_keys(cls, value: str, info) -> str:
        """清单项标识和根引用必须是稳定逻辑键。"""

        return _validate_logical_key(value, field_label=info.field_name)

    @field_validator("source_relative_path")
    @classmethod
    def validate_source_relative_path(cls, value: str) -> str:
        """拒绝绝对路径和上级目录，后端只保存来源相对路径。"""

        return _normalize_relative_path(value, field_label="source_relative_path")

    @field_validator("original_filename")
    @classmethod
    def validate_original_filename(cls, value: str) -> str:
        """原始文件名不能携带目录，避免展示字段和真实清单对象错位。"""

        normalized = value.strip()
        if normalized in {".", ".."} or "/" in normalized or "\\" in normalized or "\x00" in normalized:
            raise ValueError("original_filename 必须是单个文件名")
        return normalized

    @field_validator("expected_sha256")
    @classmethod
    def validate_expected_sha256(cls, value: str | None) -> str | None:
        """存在预期哈希时只接受标准 SHA-256 十六进制值。"""

        if value is None:
            return None
        normalized = value.lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("expected_sha256 必须是 64 位十六进制 SHA-256")
        return normalized

    @model_validator(mode="after")
    def ensure_filename_matches_path(self) -> "IngestItemCreate":
        """保证展示原名就是来源路径末段，不能登记指向另一对象的别名。"""

        if PurePosixPath(self.source_relative_path).name != self.original_filename:
            raise ValueError("original_filename 必须与 source_relative_path 的文件名一致")
        return self


class IngestItemsAppendRequest(BaseModel):
    """分页追加固定清单项；单次请求设上限以控制验证和事务大小。"""

    model_config = ConfigDict(extra="forbid")

    items: list[IngestItemCreate] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def reject_duplicate_client_item_ids(self) -> "IngestItemsAppendRequest":
        """同一页不允许相同客户端项重复出现并携带潜在冲突载荷。"""

        item_ids = [item.client_item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("同一请求中的 client_item_id 不能重复")
        return self


class IngestItemResponse(BaseModel):
    """对外返回单条业务进度，不包含绝对路径或文件正文。"""

    id: str
    client_item_id: str
    source_root_ref: str
    source_relative_path: str
    original_filename: str
    expected_size: int
    source_mtime_ns: int
    expected_sha256: str | None
    actual_sha256: str | None
    workflow_revision: int
    stage: str
    status: str
    ingest_status: str
    extraction_status: str
    organization_status: str
    index_status: str
    user_task_status: str
    decision: str | None
    final_document_id: str | None
    final_version_id: str | None
    final_working_copy_id: str | None
    # 导入完成名称是不可变审计快照；当前名称与状态来自工作副本实时投影。
    ingest_final_filename: str | None
    current_filename: str | None
    current_file_status: str
    current_file_available: bool
    current_working_copy_revision: int | None
    current_document_version_id: str | None
    error: dict
    result: dict
    created_at: datetime
    updated_at: datetime


class IngestBatchCounts(BaseModel):
    """批次逐状态计数；等待和取消不能混入成功或失败。"""

    total: int = 0
    pending: int = 0
    running: int = 0
    waiting_duplicate_confirmation: int = 0
    waiting_external_extraction: int = 0
    waiting_existing_result: int = 0
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    cancelled: int = 0
    expired: int = 0
    skipped: int = 0


class IngestBatchResponse(BaseModel):
    """批次状态与冻结策略响应。"""

    id: str
    client_id: str
    request_id: str
    manifest_status: str
    manifest_revision: int
    result_revision: int
    policy: dict
    policy_version: str
    user_request: str | None
    conversation_id: str | None
    status: str
    counts: IngestBatchCounts
    display_status: Literal["PROCESSING", "FILE_PROCESSING_COMPLETED"] = "PROCESSING"
    final_receipt_ready: bool = False
    receipt: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class IngestItemsAppendResponse(BaseModel):
    """追加清单页后的幂等结果。"""

    batch: IngestBatchResponse
    items: list[IngestItemResponse]
    created_count: int
    reused_count: int


class IngestItemsPageResponse(BaseModel):
    """稳定游标分页的条目明细。"""

    items: list[IngestItemResponse]
    next_cursor: str | None = None


class IngestContentUploadResponse(BaseModel):
    """单个批次条目接收完成后的可恢复业务引用。"""

    batch: IngestBatchResponse
    item: IngestItemResponse
    filesystem_job_id: str | None = None
    accepted: bool
    reused: bool


class IngestBatchResumeResponse(BaseModel):
    """批次传输恢复检查结果，不会重置已经失败或完成的条目。"""

    batch: IngestBatchResponse
    receivable_item_ids: list[str] = Field(default_factory=list)


class IngestDuplicateCandidateResponse(BaseModel):
    """WorkBuddy 可展示的真实重复候选，不包含服务器文件路径。"""

    candidate_id: str
    match_type: str
    match_scope: str
    similarity_score: float
    summary: dict
    existing_document_id: str | None = None


class IngestDuplicateReviewResponse(BaseModel):
    """绑定批次条目与固定修订的结构化重复确认。"""

    item_id: str
    review_id: str
    review_revision: int
    comparison_phase: str
    status: str
    expires_at: datetime
    duplicate_group_id: str | None = None
    group_revision: int | None = None
    group_member_item_ids: list[str] = Field(default_factory=list)
    allowed_decisions: list[str]
    candidates: list[IngestDuplicateCandidateResponse]


class IngestDuplicateDecisionRequest(BaseModel):
    """WorkBuddy 在用户明确选择后提交的版本化重复决定。"""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=200)
    review_id: str = Field(min_length=1, max_length=36)
    review_revision: int = Field(ge=1)
    group_revision: int | None = Field(default=None, ge=1)
    group_member_item_ids: list[str] = Field(default_factory=list, max_length=200)
    candidate_id: str | None = Field(default=None, min_length=1, max_length=36)
    decision: Literal["USE_EXISTING_FILE", "CONTINUE_UPLOAD", "CANCEL_UPLOAD", "WAIT_AND_REUSE"]

    @model_validator(mode="after")
    def validate_candidate_requirement(self) -> "IngestDuplicateDecisionRequest":
        """只有需要绑定具体候选的决定可以或必须携带 candidate_id。"""

        if self.decision in {"USE_EXISTING_FILE", "WAIT_AND_REUSE"} and not self.candidate_id:
            raise ValueError("当前决定必须指定 candidate_id")
        if self.decision in {"CONTINUE_UPLOAD", "CANCEL_UPLOAD"} and self.candidate_id:
            raise ValueError("当前决定不能指定 candidate_id")
        if self.group_revision is None and self.group_member_item_ids:
            raise ValueError("非批内重复决定不能携带组成员")
        if self.group_revision is not None and not self.group_member_item_ids:
            raise ValueError("批内重复决定必须携带用户看到的完整组成员")
        if len(self.group_member_item_ids) != len(set(self.group_member_item_ids)):
            raise ValueError("批内重复组成员不能重复")
        return self


class IngestDuplicateDecisionResponse(BaseModel):
    """重复决定后的最新逐项和批次状态。"""

    review: IngestDuplicateReviewResponse
    item: IngestItemResponse
    batch: IngestBatchResponse
    filesystem_job_id: str | None = None
    reused: bool = False


class IngestItemActionRequest(BaseModel):
    """对失败或未完成条目执行取消/显式重试的幂等请求。"""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=500)


class IngestItemActionResponse(BaseModel):
    """条目动作后的稳定状态和可选续跑任务。"""

    item: IngestItemResponse
    batch: IngestBatchResponse
    filesystem_job_id: str | None = None
    accepted: bool = True
    reused: bool = False
