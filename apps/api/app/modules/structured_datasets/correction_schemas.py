"""已发布 Excel 行更正的独立契约；与新增行回传严格隔离。"""

from uuid import UUID
from typing import Literal

from pydantic import Field, model_validator

from .task_schemas import TaskField, TaskSource
from .writeback_schemas import StrictInput


class CorrectionCheckout(StrictInput):
    """冻结唯一工作副本、稳定记录和真实数据行。"""

    filename: str = Field(min_length=1, max_length=240)
    request_key: str = Field(min_length=1, max_length=120)
    record_id: str = Field(min_length=1, max_length=100)
    row_number: int = Field(ge=1, le=100000)
    source_extraction_run_id: UUID | None = None
    review_mode: Literal["DIRECT_SAVE", "REVIEW_REQUIRED"] = "DIRECT_SAVE"


class CorrectionChange(StrictInput):
    """before/after 是实际 Excel 展示值，字段证据另行校验。"""

    field_key: str = Field(min_length=1, max_length=100)
    before: str | int | float | bool | None = None
    after: str | int | float | bool | None = None
    field: TaskField


class CorrectionPayload(StrictInput):
    """仅修正同一已发布记录；不携带新人员行。"""

    record_id: str = Field(min_length=1, max_length=100)
    row_number: int = Field(ge=1, le=100000)
    source: TaskSource
    changes: list[CorrectionChange] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def unique_fields(self):
        """同一格不能出现互相竞争的两个新值。"""
        keys = [change.field_key for change in self.changes]
        if len(keys) != len(set(keys)):
            raise ValueError("更正字段不得重复")
        if any(change.before == change.after for change in self.changes):
            raise ValueError("更正清单不能包含未变化的字段")
        return self


class CorrectionPublish(StrictInput):
    """发布时再次绑定文件与预览指纹。"""

    task_id: UUID
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
