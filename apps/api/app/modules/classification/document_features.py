"""构造分类候选使用的分来源文档特征。

该模块只构造轻量引用和可定位片段，不持久化全文，也不负责选择 PRIMARY 或写数据库。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FeatureTextRef(BaseModel):
    """指向解析结果中一段可定位文字，避免用无来源拼接文本充当证据。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=3000)
    page_number: int | None = Field(default=None, ge=1)
    sheet_name: str | None = Field(default=None, max_length=200)
    cell_range: str | None = Field(default=None, max_length=100)
    paragraph_index: int | None = Field(default=None, ge=0)


class DocumentFeaturesV2(BaseModel):
    """分类服务运行期的结构化特征，正文仍由受控服务按引用读取。"""

    model_config = ConfigDict(extra="forbid")

    filename: str = ""
    body_title: str = ""
    section_refs: list[FeatureTextRef] = Field(default_factory=list)
    sheet_refs: list[FeatureTextRef] = Field(default_factory=list)
    header_refs: list[FeatureTextRef] = Field(default_factory=list)
    issuer_refs: list[FeatureTextRef] = Field(default_factory=list)
    recipient_refs: list[FeatureTextRef] = Field(default_factory=list)
    subject_refs: list[FeatureTextRef] = Field(default_factory=list)
    document_type: str | None = None
    resource_type: str | None = None
    source_context_ref: str | None = None

    def evidence_text(self) -> str:
        """为确定性候选规则生成有界文本；最终证据仍保留原引用位置。"""

        refs = [
            *self.header_refs,
            *self.issuer_refs,
            *self.recipient_refs,
            *self.subject_refs,
            *self.section_refs,
            *self.sheet_refs,
        ]
        return "\n".join(ref.text for ref in refs if ref.text)
