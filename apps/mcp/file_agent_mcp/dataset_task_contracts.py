"""WorkBuddy 表格任务的严格宿主契约；与 API 契约通过测试比对。"""

from typing import Literal
import re
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .dataset_tools import DatasetField, DatasetLayout, DatasetValue
from .dataset_assessment import SourceQuote


class StrictInput(BaseModel):
    """额外字段拒绝，不提供任意文件路径或脚本入口。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TaskSource(StrictInput):
    """来源可为待归档提取任务或已经发布的固定正文版本。"""
    extraction_task_id: UUID | None = None
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    extraction_run_id: UUID | None = None
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_source(self):
        """宿主与后端使用同一来源互斥规则，错误在发出请求前呈现。"""
        ids = (self.document_id, self.document_version_id, self.extraction_run_id)
        if self.extraction_task_id:
            if any(ids):
                raise ValueError("提取任务与工作副本引用不能混用")
        elif not all(ids):
            raise ValueError("已有来源需要 document/version/extraction 三个 ID")
        return self


class TaskFieldSpec(DatasetField):
    """与 API 默认字段语义一致，确保跨进程预览指纹稳定。"""
    multiple: bool = False
    aliases: list[str] = Field(default_factory=list, max_length=10)
    enum_values: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        """字段名与后端指纹规范保持一致。"""
        normalized = " ".join(value.strip().split())
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("字段名不能包含控制字符")
        return normalized

    @field_validator("aliases", "enum_values")
    @classmethod
    def normalize_values(cls, values: list[str]) -> list[str]:
        """别名与枚举值去重及长度限制与后端一致。"""
        normalized = []
        for value in values:
            item = " ".join(str(value or "").strip().split())
            if not item or len(item) > 80 or any(ord(char) < 32 for char in item):
                raise ValueError("别名或枚举值必须为 1–80 个普通字符")
            if item not in normalized:
                normalized.append(item)
        return normalized


class NewTarget(StrictInput):
    """无模板时使用的固定字段清单。"""
    name: str = Field(min_length=1, max_length=100)
    filename: str | None = Field(default=None, min_length=1, max_length=240)
    fields: list[TaskFieldSpec] = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def check_schema(self):
        """新表字段必须是唯一、标量且为受支持类型。"""
        keys = [field.key for field in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("字段 key 不能重复")
        if any(field.multiple for field in self.fields):
            raise ValueError("累计表只支持标量字段")
        return self


class UploadedTarget(StrictInput):
    """复用已入库 XLS/XLSX 的受控目标注册参数。"""
    document_id: UUID
    document_version_id: UUID | None = None
    extraction_run_id: UUID | None = None
    sheet_name: str | None = Field(default=None, min_length=1, max_length=120)
    legacy_target_policy: Literal["CONVERT_TO_XLSX"] | None = None
    layout: DatasetLayout | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)


class TaskTarget(StrictInput):
    """按模板真实列顺序冻结目标与完整标题，不要求先创建数据集。"""
    new: NewTarget | None = None
    uploaded: UploadedTarget | None = None
    uploaded_task_id: UUID | None = None
    filename: str = Field(min_length=1, max_length=240)
    dataset_id: UUID | None = None
    document_version_id: UUID | None = None
    sheet_name: str | None = Field(default=None, max_length=120)
    layout: DatasetLayout | None = None
    labels: list[str] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def check_target(self):
        """与后端保持四选一、版本绑定和完整标题长度约束。"""
        if sum(x is not None for x in (self.new, self.uploaded, self.uploaded_task_id, self.dataset_id)) != 1:
            raise ValueError("必须且只能指定一个目标策略")
        if self.dataset_id and not self.document_version_id:
            raise ValueError("已有目标必须绑定当前版本")
        if any(not label.strip() or len(label) > 4000 for label in self.labels):
            raise ValueError("完整标题不能为空或超过 4000 字符")
        return self


class ExtractionConfirmationRef(StrictInput):
    """一次用户确认同时绑定已展示文档和逐列表格草稿。"""
    task_id: UUID
    confirmation_id: UUID
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaskManifest(StrictInput):
    """预览阶段只发元数据到服务器，完整候选留在宿主缓存。"""
    sources: list[TaskSource] = Field(min_length=1, max_length=20)
    target: TaskTarget
    sealed_only: bool = False
    identity_column_range: Literal["B:I"] | None = Field(default=None, exclude_if=lambda value: value is None)
    source_fact_column_range: Literal["J:P"] | None = Field(default=None, exclude_if=lambda value: value is None)
    write_scope: Literal["A:I", "A:I,L", "A:AA", "TEMPLATE_ALL"] | None = Field(default=None, exclude_if=lambda value: value is None)
    required_pdf_columns: Literal["B:I", "B:I,L"] | None = Field(default=None, exclude_if=lambda value: value is None)
    extraction_confirmations: list[ExtractionConfirmationRef] = Field(default_factory=list, max_length=21)

    @model_validator(mode="after")
    def check_identity_columns(self):
        """宿主先验证免审批范围绑定实际物理列，不让模型按字段名称猜列。"""
        if self.write_scope == "A:AA":
            if (self.required_pdf_columns is not None or not self.sealed_only
                    or self.identity_column_range != "B:I" or self.source_fact_column_range != "J:P"
                    or self.target.new is not None or self.target.layout is None):
                raise ValueError("完整教授表任务须绑定真实模板、身份列与盖章列策略")
            from openpyxl.utils.cell import range_boundaries
            left, _, right, _ = range_boundaries(self.target.layout.header_range)
            if left != 1 or right != 27 or len(self.target.labels) != 27:
                raise ValueError("完整教授表必须精确覆盖物理 A:AA 列")
        elif self.write_scope == "TEMPLATE_ALL":
            names = [index for index, label in enumerate(self.target.labels)
                     if re.sub(r"\s*\[[A-Z]+\]$", "", label.split(" / ")[-1]).strip() == "姓名"]
            if (self.target.new is not None or self.target.layout is None or self.required_pdf_columns is not None
                    or self.sealed_only or self.identity_column_range or self.source_fact_column_range
                    or self.target.labels[0].strip() not in {"序号", "编号"}
                    or _header_left_column(self.target.layout) != 1 or len(names) != 1):
                raise ValueError("模板全列填表须绑定真实 Excel 布局，不能混用教授表策略")
        elif (self.write_scope is None) != (self.required_pdf_columns is None):
            raise ValueError("定向写入范围与 PDF 必填范围必须同时提供")
        if self.write_scope in {"A:I", "A:I,L"}:
            if self.target.new is not None or self.target.layout is None or self.sealed_only or self.identity_column_range or self.source_fact_column_range:
                raise ValueError("定向写入需要已有模板的真实布局，且不能混用盖章任务")
            if self.required_pdf_columns != ("B:I,L" if self.write_scope == "A:I,L" else "B:I"):
                raise ValueError("定向写入与 PDF 必填列范围不一致")
            left = _header_left_column(self.target.layout)
            from openpyxl.utils.cell import range_boundaries
            right = range_boundaries(self.target.layout.header_range)[2]
            if left != 1 or right < (12 if self.write_scope == "A:I,L" else 9) or len(self.target.labels) < (12 if self.write_scope == "A:I,L" else 9):
                raise ValueError("定向写入要求目标真实物理列从 A 开始并覆盖全部指定列")
        if self.source_fact_column_range is not None:
            if not self.sealed_only or self.identity_column_range != "B:I" or self.target.new is not None:
                raise ValueError("J:P 无章来源事实仅适用于盖章专用 Excel 模板")
            left = _header_left_column(self.target.layout)
            if left > 10 or left + len(self.target.labels) - 1 < 16:
                raise ValueError("目标表头必须实际覆盖 Excel J:P 列")
        if self.identity_column_range is None:
            return self
        if not self.sealed_only or self.target.new is not None:
            raise ValueError("身份列免审批仅适用于盖章任务的 Excel 模板")
        left = _header_left_column(self.target.layout)
        if left > 2 or left + len(self.target.labels) - 1 < 9:
            raise ValueError("目标表头必须实际覆盖 Excel B:I 列")
        return self


def _header_left_column(layout: DatasetLayout | None) -> int:
    """只从已校验的模板布局计算列号；无复杂布局时标准标题从 A 列开始。"""
    if layout is None:
        return 1
    match = re.match(r"[A-Z]+", layout.header_range)
    if match is None:
        raise ValueError("目标表头范围无效")
    number = 0
    for letter in match.group():
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


class TeacherRenderPart(StrictInput):
    fragment_index: int = Field(ge=0, le=99)
    role: Literal["GRADUATION", "SCHOOL", "MAJOR", "TEXT", "ISSN", "JOURNAL_NAME", "ZONE"]
    text: str = Field(min_length=1, max_length=1000)


class TeacherRenderItem(StrictInput):
    ordinal: int | None = Field(default=None, ge=1, le=100)
    stage: Literal["博士", "硕士", "本科"] | None = None
    parts: list[TeacherRenderPart] = Field(min_length=1, max_length=20)


class TeacherRender(StrictInput):
    kind: Literal["EDUCATION", "PAPER", "PROJECT", "ACHIEVEMENT"]
    items: list[TeacherRenderItem] = Field(min_length=1, max_length=100)
    catalog_version_id: UUID | None = None


class TaskField(DatasetValue):
    """表格预览证据可以引用本轮真实宿主 unit_id。"""
    evidence_refs: list[SourceQuote] = Field(default_factory=list, max_length=20)
    teacher_render: TeacherRender | None = None


class TaskRecord(StrictInput):
    """一名人员一条记录，所有列按模板顺序提供。"""
    source_index: int = Field(ge=0, le=19)
    source_indices: list[int] = Field(default_factory=list, max_length=20, exclude_if=lambda value: not value)
    fields: list[TaskField] = Field(min_length=1, max_length=40)


class TaskPayload(StrictInput):
    """在本地校验完整列覆盖，避免把漏列当正常空白。"""
    manifest: TaskManifest
    records: list[TaskRecord] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_coverage(self):
        """必填完整列及留空原因在发送前即可发现。"""
        if (self.manifest.write_scope in {"A:I", "A:I,L", "A:AA"}
                and [record.source_index for record in self.records] != list(range(len(self.manifest.sources)))):
            raise ValueError("A–I 批量任务必须每份来源恰好对应一行，并按附件顺序排列")
        if self.manifest.write_scope == "TEMPLATE_ALL":
            assigned = [index for row in self.records for index in row.source_indices]
            if (sorted(assigned) != list(range(len(self.manifest.sources)))
                    or any(not row.source_indices or row.source_indices[0] != row.source_index
                           or len(set(row.source_indices)) != len(row.source_indices) for row in self.records)
                    or [row.source_index for row in self.records] != sorted(row.source_index for row in self.records)):
                raise ValueError("每份来源必须恰好归属一位人员，首个来源为该行主来源")
            name_offset = next(index for index, label in enumerate(self.manifest.target.labels)
                               if re.sub(r"\s*\[[A-Z]+\]$", "", label.split(" / ")[-1]).strip() == "姓名")
            if any(len(row.fields) <= name_offset or not row.fields[name_offset].raw_text or not row.fields[name_offset].evidence_refs
                   for row in self.records):
                raise ValueError("每位人员必须有正式正文支持的姓名")
        for record in self.records:
            if record.source_index >= len(self.manifest.sources):
                raise ValueError("来源索引越界")
            if len(record.fields) != len(self.manifest.target.labels):
                raise ValueError("必须逐列提供完整结果")
            left = _header_left_column(self.manifest.target.layout)
            for index, field in enumerate(record.fields):
                if re.fullmatch(r"〔空白列:[A-Z]{1,3}〕", self.manifest.target.labels[index]):
                    if (field.raw_text is not None or field.blank_reason != "TARGET_BLANK_COLUMN"
                            or field.evidence_refs or field.fragments or field.source_only_fragments
                            or field.approval or field.assessment or field.teacher_render
                            or field.numbered_items or field.ocr_status or field.ocr_unit_id):
                        raise ValueError("目标空白列必须原样保留，不得填写或附加证据")
                    continue
                leaf_label = re.sub(r"\s*\[[A-Z]+\]$", "", self.manifest.target.labels[index].split(" / ")[-1]).strip()
                if (self.manifest.write_scope in {None, "TEMPLATE_ALL"}
                        and len(field.fragments) > 1 and not field.teacher_render and not field.numbered_items
                        and any(title in leaf_label for title in ("论文题目", "论文标题", "论文名称"))):
                    raise ValueError("论文题目有多条原文片段时必须逐篇编号")
                if not field.raw_text and not field.blank_reason:
                    raise ValueError("每个空列必须提供留空原因")
                if self.manifest.write_scope == "TEMPLATE_ALL":
                    column = left + index
                    if column == 1 and self.manifest.target.labels[index].strip() in {"序号", "编号"}:
                        if field.raw_text or field.blank_reason != "TARGET_DERIVED" or field.evidence_refs:
                            raise ValueError("模板序号列只能由目标表生成")
                    elif field.raw_text:
                        if (not field.evidence_refs or field.approval or field.assessment or field.ocr_status
                                or field.source_only_fragments):
                            raise ValueError("模板已填写字段须有真实原文证据，且不得作资格或印章判定")
                    elif field.evidence_refs or field.fragments or field.approval or field.assessment:
                        raise ValueError("模板空字段不得携带证据或资格判定")
                if self.manifest.write_scope in {"A:I", "A:I,L"}:
                    column = left + index
                    if column == 1 and (field.raw_text or field.blank_reason != "TARGET_DERIVED"):
                        raise ValueError("A 列只能沿用目标模板序号")
                    if 2 <= column <= 9:
                        if field.raw_text:
                            if (len(field.evidence_refs) != 1 or field.assessment or field.approval
                                    or field.fragments or field.source_only_fragments):
                                raise ValueError("B–I 已填写字段必须有唯一原文证据且不得做资格判定")
                        elif (self.manifest.write_scope != "A:I,L"
                              or field.blank_reason not in {"SOURCE_FIELD_NOT_FOUND", "OCR_UNREADABLE"}
                              or field.evidence_refs or field.fragments or field.source_only_fragments
                              or field.assessment or field.approval):
                            raise ValueError("B–I 留空须给出缺失或无法辨认原因，且不得附证据或资格判定")
                    if column == 12 and self.manifest.write_scope == "A:I,L":
                        if field.raw_text:
                            if (not re.fullmatch(r"(?:19|20)\d{2}(?:、(?:19|20)\d{2})*", field.raw_text)
                                    or not field.evidence_refs or field.fragments or field.assessment or field.approval):
                                raise ValueError("L 列只能填写有原文证据的优秀年度")
                        elif (field.blank_reason not in {"NO_EXCELLENT_YEARS", "ANNUAL_REVIEW_UNREADABLE", "ANNUAL_REVIEW_NOT_FOUND"}
                              or field.evidence_refs or field.fragments or field.assessment or field.approval):
                            raise ValueError("L 列留空须说明考核优秀年度状态")
                    elif column >= 10 and (field.raw_text or field.blank_reason != "OUT_OF_SCOPE" or field.evidence_refs or field.fragments or field.assessment or field.approval):
                        raise ValueError("范围外业务列必须为 OUT_OF_SCOPE")
                if self.manifest.write_scope == "A:AA":
                    column = left + index
                    if column == 1 and (field.raw_text or field.blank_reason != "TARGET_DERIVED" or field.ocr_status):
                        raise ValueError("A 列只能沿用目标模板序号")
                    if column >= 2 and field.blank_reason == "OUT_OF_SCOPE":
                        raise ValueError("完整教授表须处理 B–AA 全部列，不能标记为范围外")
                    if 2 <= column <= 9 and not field.ocr_status and (
                            not field.raw_text or not field.evidence_refs or field.approval or field.assessment):
                        raise ValueError("清晰的 B–I 字段必须提供本来源 OCR 证据")
                elif field.ocr_status:
                    raise ValueError("OCR 待核对标记仅允许完整教授表任务")
                identity_field = self.manifest.identity_column_range == "B:I" and 2 <= left + index <= 9
                column = left + index
                source_fact = self.manifest.source_fact_column_range == "J:P" and column in {10, 11, 13, 15, 16}
                if identity_field and field.assessment is not None:
                    raise ValueError("身份列不得进行资格条件判定或加对勾")
                if identity_field and field.raw_text and not (field.evidence_refs or field.fragments):
                    raise ValueError("身份列免审批仍必须提供原文引用")
                if field.source_only_fragments and not source_fact:
                    raise ValueError("SOURCE_ONLY 仅允许专用模板 J、K、M、O、P 列")
                sealed_fragments = [ref for ref in field.fragments if ref not in field.source_only_fragments]
                if field.source_only_fragments and sealed_fragments and not field.approval:
                    raise ValueError("混合状态字段的盖章片段必须绑定审批证据")
                if (field.raw_text and self.manifest.sealed_only and not identity_field
                        and not field.approval and not field.source_only_fragments and not field.ocr_status):
                    raise ValueError("盖章范围任务的所有填写内容必须绑定审批证据")
        return self
