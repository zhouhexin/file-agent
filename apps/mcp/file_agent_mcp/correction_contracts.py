"""WorkBuddy 已发布 Excel 行更正的本地严格输入契约。"""

from pydantic import Field, model_validator

from .dataset_task_contracts import StrictInput, TaskField, TaskSource


class CorrectionChange(StrictInput):
    """每个更正格同时给出冻结旧值、新值和来源证据。"""

    field_key: str = Field(min_length=1, max_length=100)
    before: str | int | float | bool | None = None
    after: str | int | float | bool | None = None
    field: TaskField


class CorrectionPayload(StrictInput):
    """只接受一个已发布记录的有界字段更正。"""

    record_id: str = Field(min_length=1, max_length=100)
    row_number: int = Field(ge=1, le=100000)
    source: TaskSource
    changes: list[CorrectionChange] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def check_changes(self):
        """同一字段只允许一个新值，防止本地和服务端预览歧义。"""
        keys = [change.field_key for change in self.changes]
        if len(keys) != len(set(keys)) or any(x.before == x.after for x in self.changes):
            raise ValueError("更正字段重复或无实际变化")
        return self
