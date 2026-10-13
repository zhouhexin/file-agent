"""共享累计表格的有界输入契约；不接收路径、SQL 或权限配置。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.agent.tool_schemas import StructuredFieldSpec
from app.modules.structured_extraction.workbook_layout import WorkbookLayout
from .assessment import ApprovalScope, FieldAssessment, SourceQuote


class StrictInput(BaseModel):
    """拒绝未知参数，避免模型误认为额外指令会被执行。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DatasetCreate(StrictInput):
    """创建共享表格时一次固定字段；不配置 owner 或授权。"""

    name: str = Field(min_length=1, max_length=100)
    filename: str | None = Field(default=None, min_length=1, max_length=240)
    fields: list[StructuredFieldSpec] = Field(min_length=1, max_length=40)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def check_schema(self):
        """Demo 仅接受可确定性比较的标量字段。"""
        keys = [field.key for field in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("字段 key 不能重复")
        for field in self.fields:
            if field.multiple or field.field_type not in {
                "string", "person_name", "organization", "integer", "decimal", "date"
            }:
                raise ValueError("Demo 仅支持文本、整数、小数和日期标量")
        return self


class DatasetCreateFromExcel(StrictInput):
    """把已入库的当前 Excel 工作副本注册为新的累计目标表。"""

    document_id: UUID
    document_version_id: UUID | None = None
    extraction_run_id: UUID | None = None
    sheet_name: str | None = Field(default=None, min_length=1, max_length=120)
    legacy_target_policy: Literal["CONVERT_TO_XLSX"] | None = None
    layout: WorkbookLayout | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)


class TeacherRenderPart(StrictInput):
    fragment_index: int = Field(ge=0, le=99)
    role: Literal["GRADUATION", "SCHOOL", "MAJOR", "TEXT", "ISSN", "JOURNAL_NAME", "ZONE"]
    text: str = Field(min_length=1, max_length=1000)


class TeacherRenderItem(StrictInput):
    ordinal: int | None = Field(default=None, ge=1, le=100)
    stage: Literal["博士", "硕士", "本科"] | None = None
    parts: list[TeacherRenderPart] = Field(min_length=1, max_length=20)


class TeacherRender(StrictInput):
    """受控单元格展示说明；各 part 必须由已经核验的原文片段支持。"""

    kind: Literal["EDUCATION", "PAPER", "PROJECT", "ACHIEVEMENT"]
    items: list[TeacherRenderItem] = Field(min_length=1, max_length=100)
    catalog_version_id: UUID | None = None


class FieldCandidate(StrictInput):
    """字段原文必须带回宿主正文中的稳定区段与可定位引文。"""

    raw_text: str | None = Field(default=None, max_length=4000)
    page_number: int | None = Field(default=1, ge=1)
    evidence_refs: list["DatasetEvidenceRef"] = Field(default_factory=list, max_length=20)
    confidence: float | None = Field(default=None, ge=0, le=1)
    fragments: list[SourceQuote] = Field(default_factory=list, max_length=100)
    source_only_fragments: list[SourceQuote] = Field(default_factory=list, max_length=100)
    blank_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    # 完整教授表允许一次 OCR 后保留不确定候选；状态与来源区段由服务端核验。
    ocr_status: Literal["UNCERTAIN", "UNREADABLE"] | None = Field(default=None, exclude_if=lambda value: value is None)
    ocr_unit_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    approval: ApprovalScope | None = None
    assessment: FieldAssessment | None = None
    teacher_render: TeacherRender | None = None

    @model_validator(mode="after")
    def validate_presentation(self):
        """原文拼接与空白说明互斥，对勾判定必须携带审批证据。"""
        if self.fragments:
            joined = "\n".join(ref.quote for ref in self.fragments)
            if self.raw_text is not None and self.raw_text != joined:
                raise ValueError("fragments 必须与 raw_text 逐字拼接一致")
            if len(joined) > 4000:
                raise ValueError("字段拼接内容超过 4000 字符")
            self.raw_text = joined
        if self.blank_reason and self.raw_text:
            raise ValueError("有内容字段不能同时声明留空")
        if self.ocr_status:
            if not self.ocr_unit_id or self.approval or self.assessment or self.fragments or self.source_only_fragments:
                raise ValueError("OCR 待核对字段必须绑定来源区段，且不能声明审批或资格通过")
            if self.ocr_status == "UNCERTAIN" and (not self.raw_text or not self.evidence_refs or self.blank_reason):
                raise ValueError("OCR 候选须有可定位的原始 OCR 引文")
            if self.ocr_status == "UNREADABLE" and (self.raw_text or self.evidence_refs or self.blank_reason != "OCR_UNREADABLE"):
                raise ValueError("OCR 未识别字段只能提供来源区段和固定原因")
        elif self.ocr_unit_id is not None:
            raise ValueError("OCR 来源区段只用于待核对字段")
        if self.source_only_fragments:
            # 无章状态只能绑定已经逐字验证的片段；不能由调用方提供另一个展示文本。
            all_refs = [ref.model_dump() for ref in self.fragments]
            only_refs = [ref.model_dump() for ref in self.source_only_fragments]
            if not self.fragments or len(only_refs) != len({str(ref) for ref in only_refs}) or any(ref not in all_refs for ref in only_refs):
                raise ValueError("SOURCE_ONLY 必须逐一引用字段 fragments 中的唯一原文片段")
            if self.assessment:
                raise ValueError("SOURCE_ONLY 片段不能作为标题资格通过依据")
            if self.approval and len(only_refs) == len(all_refs):
                raise ValueError("全部无章片段不得借用审批证据")
        if self.assessment and (not self.approval or not self.raw_text):
            raise ValueError("标题条件判定必须具有字段内容及审批证据")
        return self


class DatasetEvidenceRef(StrictInput):
    """引用同一基础提取运行中的正式正文区段，可选字符范围用于消歧。"""

    source_unit_id: UUID
    quote: str = Field(min_length=1, max_length=8000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_range(self):
        """字符范围必须成对出现且为有效左闭右开区间。"""

        if (self.start is None) != (self.end is None):
            raise ValueError("证据 start/end 必须同时提供")
        if self.start is not None and self.end <= self.start:
            raise ValueError("证据 end 必须大于 start")
        return self


class RecordCandidate(StrictInput):
    """记录索引按提交顺序由后端生成。"""

    fields: dict[str, FieldCandidate] = Field(max_length=40)


class DatasetSupplementalSource(StrictInput):
    """仅在已冻结的模板写回任务中使用的额外证据来源。"""

    document_id: UUID
    document_version_id: UUID
    extraction_run_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DatasetAppend(StrictInput):
    """固定共享工作副本、内容版本、正文运行与提交键。"""

    document_id: UUID
    document_version_id: UUID
    extraction_run_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    supplemental_sources: list[DatasetSupplementalSource] = Field(default_factory=list, max_length=19)
    content_origin: Literal["WORKBUDDY", "WORKBUDDY_SCOPED"] | None = None
    target_working_copy_id: UUID | None = None
    target_document_id: UUID | None = None
    target_document_version_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)
    records: list[RecordCandidate] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_target_binding(self):
        """按文件名续写时三个稳定目标引用必须同时出现，禁止半绑定。"""

        target_values = (
            self.target_working_copy_id,
            self.target_document_id,
            self.target_document_version_id,
        )
        if any(target_values) and not all(target_values):
            raise ValueError("目标 Excel 的 working_copy/document/version 必须同时提供")
        return self


class QueryFilter(StrictInput):
    """字段级固定比较，拒绝任意表达式。"""

    field: str = Field(min_length=1, max_length=64)
    op: Literal["eq", "contains", "gte", "lte"] = "eq"
    value: str | int | float = Field(union_mode="left_to_right")

    @model_validator(mode="after")
    def limit_filter_text(self):
        """查询也限制资源，避免通过过滤值注入超大文本。"""
        if isinstance(self.value, str) and len(self.value) > 4000:
            raise ValueError("过滤值不能超过 4000 字符")
        return self


class DatasetQuery(StrictInput):
    """小数据集的确定性查询与全量筛选后求和。"""

    filters: list[QueryFilter] = Field(default_factory=list, max_length=10)
    sum_field: str | None = Field(default=None, max_length=64)
    include_review: bool = False
    offset: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=100, ge=1, le=200)
