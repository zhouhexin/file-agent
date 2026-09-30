"""WorkBuddy 完整 Excel 回传的新入口契约；不改变既有累计表格任务。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from .task_schemas import ExtractionConfirmationRef


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WritebackCheckout(StrictInput):
    filename: str = Field(min_length=1, max_length=240)
    request_key: str = Field(min_length=1, max_length=120)
    review_mode: Literal["DIRECT_SAVE", "REVIEW_REQUIRED"] = "DIRECT_SAVE"


class WritebackPreview(StrictInput):
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extraction_confirmations: list[ExtractionConfirmationRef] = Field(default_factory=list, max_length=21)


class WritebackConfirm(StrictInput):
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class WritebackPublish(StrictInput):
    task_id: UUID
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManualRevisionPublish(StrictInput):
    """本轮人工补全文件必须绑定上传前看到的同名基线。"""

    filename: str = Field(min_length=1, max_length=240)
    request_key: str = Field(min_length=1, max_length=120)
    base_document_version_id: UUID
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: int = Field(ge=0)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
