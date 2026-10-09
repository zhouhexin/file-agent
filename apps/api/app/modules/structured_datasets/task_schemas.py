"""Excel 任务的预览、确认和提交契约；确认前 API 不接受候选正文。"""

from typing import Literal
import re
from uuid import UUID
from pydantic import Field, model_validator

from .schemas import StrictInput, DatasetCreate, DatasetCreateFromExcel, FieldCandidate
from .assessment import SourceQuote


class TaskSource(StrictInput):
    """来源固定为宿主提取任务或已有工作副本快照，二者不能混用。"""
    extraction_task_id: UUID | None = None
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    extraction_run_id: UUID | None = None
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_source(self):
        """快照不能半绑定，任务哈希由服务端再次校验。"""
        ids = (self.document_id, self.document_version_id, self.extraction_run_id)
        if self.extraction_task_id:
            if any(ids):
                raise ValueError("提取任务与工作副本引用不能混用")
        elif not all(ids):
            raise ValueError("已有来源需要 document/version/extraction 三个 ID")
        return self


class TaskTarget(StrictInput):
    """目标策略在预览时冻结，不依赖后续模型重新选择。"""
    new: DatasetCreate | None = None
    uploaded: DatasetCreateFromExcel | None = None
    uploaded_task_id: UUID | None = None
    filename: str = Field(min_length=1, max_length=240)
    dataset_id: UUID | None = None
    document_version_id: UUID | None = None
    sheet_name: str | None = Field(default=None, max_length=120)
    layout: dict | None = None
    labels: list[str] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def check_target(self):
        """只允许一个目标来源，布局使用已有严格 schema。"""
        from app.modules.structured_extraction.workbook_layout import WorkbookLayout
        if sum(x is not None for x in (self.new, self.uploaded, self.uploaded_task_id, self.dataset_id)) != 1:
            raise ValueError("必须且只能指定一个目标策略")
        if self.dataset_id and not self.document_version_id:
            raise ValueError("已有目标必须绑定当前版本")
        if self.layout:
            self.layout = WorkbookLayout.model_validate(self.layout).model_dump()
        if any(not label.strip() or len(label) > 4000 for label in self.labels):
            raise ValueError("完整标题不能为空或超过 4000 字符")
        return self


class ExtractionConfirmationRef(StrictInput):
    """表格确认同时覆盖此前已展示的固定文档草稿，不重新生成正文。"""
    task_id: UUID
    confirmation_id: UUID
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaskManifest(StrictInput):
    """仅含来源固定引用与目标元数据，不含提取正文。"""
    sources: list[TaskSource] = Field(min_length=1, max_length=20)
    target: TaskTarget
    sealed_only: bool = False
    identity_column_range: Literal["B:I"] | None = Field(default=None, exclude_if=lambda value: value is None)
    source_fact_column_range: Literal["J:P"] | None = Field(default=None, exclude_if=lambda value: value is None)
    write_scope: Literal["A:I", "A:I,L", "A:AA"] | None = Field(default=None, exclude_if=lambda value: value is None)
    required_pdf_columns: Literal["B:I", "B:I,L"] | None = Field(default=None, exclude_if=lambda value: value is None)
    extraction_confirmations: list[ExtractionConfirmationRef] = Field(default_factory=list, max_length=21)

    @model_validator(mode="after")
    def check_identity_columns(self):
        """免审批只允许绑定真实上传或既有模板的物理 B:I，不扩大到新建表。"""
        if self.write_scope == "A:AA":
            if (self.required_pdf_columns is not None or not self.sealed_only
                    or self.identity_column_range != "B:I" or self.source_fact_column_range != "J:P"
                    or self.target.new is not None or self.target.layout is None):
                raise ValueError("完整教授表任务须绑定真实模板、身份列与盖章列策略")
            from openpyxl.utils.cell import range_boundaries
            left, _, right, _ = range_boundaries(self.target.layout["header_range"])
            if left != 1 or right != 27 or len(self.target.labels) != 27:
                raise ValueError("完整教授表必须精确覆盖物理 A:AA 列")
        elif (self.write_scope is None) != (self.required_pdf_columns is None):
            raise ValueError("定向写入范围与 PDF 必填范围必须同时提供")
        if self.write_scope in {"A:I", "A:I,L"}:
            if self.target.new is not None or self.target.layout is None or self.sealed_only or self.identity_column_range or self.source_fact_column_range:
                raise ValueError("定向写入需要已有模板的真实布局，且不能混用盖章任务")
            if self.required_pdf_columns != ("B:I,L" if self.write_scope == "A:I,L" else "B:I"):
                raise ValueError("定向写入与 PDF 必填列范围不一致")
            from openpyxl.utils.cell import range_boundaries
            left, _, right, _ = range_boundaries(self.target.layout["header_range"])
            if left != 1 or right < (12 if self.write_scope == "A:I,L" else 9) or len(self.target.labels) < (12 if self.write_scope == "A:I,L" else 9):
                raise ValueError("定向写入要求目标真实物理列从 A 开始并覆盖全部指定列")
        if self.source_fact_column_range is not None:
            if not self.sealed_only or self.identity_column_range != "B:I" or self.target.new is not None:
                raise ValueError("J:P 无章来源事实仅适用于盖章专用 Excel 模板")
            from openpyxl.utils.cell import range_boundaries
            left = range_boundaries(self.target.layout["header_range"])[0] if self.target.layout else 1
            if left > 10 or left + len(self.target.labels) - 1 < 16:
                raise ValueError("目标表头必须实际覆盖 Excel J:P 列")
        if self.identity_column_range is None:
            return self
        if not self.sealed_only or self.target.new is not None:
            raise ValueError("身份列免审批仅适用于盖章任务的 Excel 模板")
        from openpyxl.utils.cell import range_boundaries
        left = range_boundaries(self.target.layout["header_range"])[0] if self.target.layout else 1
        if left > 2 or left + len(self.target.labels) - 1 < 9:
            raise ValueError("目标表头必须实际覆盖 Excel B:I 列")
        return self


class TaskField(FieldCandidate):
    """预览可使用宿主 unit_id；提交阶段才绑定正式 UUID。"""
    evidence_refs: list[SourceQuote] = Field(default_factory=list, max_length=20)


class TaskRecord(StrictInput):
    """字段按目标列顺序完整提供，防止重复标题和遗漏列。"""
    source_index: int = Field(ge=0, le=19)
    fields: list[TaskField] = Field(min_length=1, max_length=40)


class TaskPayload(StrictInput):
    """候选只在 MCP 本地预览缓存及确认后的服务端事实中存在。"""
    manifest: TaskManifest
    records: list[TaskRecord] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_coverage(self):
        """所有列必须有内容或原因，每条候选绑定确定来源。"""
        if (self.manifest.write_scope in {"A:I", "A:I,L", "A:AA"}
                and [row.source_index for row in self.records] != list(range(len(self.manifest.sources)))):
            raise ValueError("定向批量任务必须每份来源恰好对应一行，并按附件顺序排列")
        for row in self.records:
            if row.source_index >= len(self.manifest.sources):
                raise ValueError("来源索引越界")
            if len(row.fields) != len(self.manifest.target.labels):
                raise ValueError("必须逐列提供完整结果，不得遗漏字段")
            left = 1
            if self.manifest.target.layout:
                from openpyxl.utils.cell import range_boundaries
                left = range_boundaries(self.manifest.target.layout["header_range"])[0]
            for index, field in enumerate(row.fields):
                if not field.raw_text and not field.blank_reason:
                    raise ValueError("每个空列必须提供留空原因")
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
                    if column >= 2 and field.ocr_status == "UNREADABLE" and field.blank_reason != "OCR_UNREADABLE":
                        raise ValueError("OCR 未识别字段需要固定留空原因")
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


class TaskPreview(StrictInput):
    """服务器预览登记只接收摘要，拒绝正文提前保存。"""
    request_key: str = Field(min_length=1, max_length=120)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_mode: Literal["REVIEW_REQUIRED", "DIRECT_SAVE"] = "DIRECT_SAVE"
    manifest: TaskManifest


class TaskConfirm(StrictInput):
    """固定用户已查看的整个表格草稿，不只确认 OCR 原文。"""
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaskCommit(StrictInput):
    """首次提交完整已确认草稿，之后可只携带指纹恢复。"""
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: TaskPayload | None = None
