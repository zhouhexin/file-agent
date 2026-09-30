"""累计表字段的有界证据判定和展示标记；不调用 OCR、不修改原文。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceQuote(BaseModel):
    """允许草稿 unit_id；提交时必须唯一绑定到本次正式 source unit。"""

    model_config = ConfigDict(extra="forbid")
    source_unit_id: str = Field(min_length=1, max_length=120)
    quote: str = Field(min_length=1, max_length=8000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_range(self):
        """拒绝单边范围和反向区间。"""
        if (self.start is None) != (self.end is None) or (
            self.start is not None and self.end <= self.start
        ):
            raise ValueError("证据范围必须成对且正向")
        return self


class ApprovalScope(BaseModel):
    """审批范围靠证据定位，不要求显式项号或签字。"""

    model_config = ConfigDict(extra="forbid")
    binding_method: Literal["EXPLICIT_RANGE", "WHOLE_ITEM", "WHOLE_SECTION", "CHECKED", "ROW_ORDER", "LAST_ITEM_FALLBACK"]
    declaration: SourceQuote
    seal: SourceQuote
    scope: list[SourceQuote] = Field(min_length=1, max_length=20)
    explanation: str = Field(min_length=1, max_length=1000)
    approval_kind: Literal["STANDARD", "HR_ANNUAL_EXCELLENCE"] = "STANDARD"


class RequirementCheck(BaseModel):
    """标题条件与原文观测值分开提交，服务器只执行白名单比较。"""

    model_config = ConfigDict(extra="forbid")
    criterion: str = Field(min_length=1, max_length=4000)
    operator: Literal["CONTAINS", "EQUALS", "GTE", "LTE"]
    expected: str = Field(min_length=1, max_length=200)
    observed: str = Field(min_length=1, max_length=4000)
    evidence: list[SourceQuote] = Field(min_length=1, max_length=20)


class RequirementGroup(BaseModel):
    """一层分支内部为 AND，分支之间支持全选、任选和至少 N 选。"""

    model_config = ConfigDict(extra="forbid")
    checks: list[RequirementCheck] = Field(min_length=1, max_length=20)


class FieldAssessment(BaseModel):
    """冻结完整列标题及组合约束，不接收任意布尔通过值。"""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=4000)
    mode: Literal["ALL", "ANY", "AT_LEAST"] = "ALL"
    minimum: int = Field(default=1, ge=1, le=20)
    required_groups: list[int] = Field(default_factory=list, max_length=20)
    groups: list[RequirementGroup] = Field(min_length=1, max_length=20)
    unresolved_reasons: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check_groups(self):
        """必选分支使用零基索引，数量条件不允许超出候选范围。"""
        if self.minimum > len(self.groups) or any(i < 0 or i >= len(self.groups) for i in self.required_groups):
            raise ValueError("条件数量或必选分支越界")
        return self


def validate_quote(reference: SourceQuote, units: list[dict]) -> dict:
    """逐字校验当前源快照，返回正式 ID；同文重复必须提供偏移。"""
    matches = [u for u in units if reference.source_unit_id in {
        str(u["source_unit_id"]), str(u.get("external_unit_id") or "")
    }]
    if len(matches) != 1:
        raise ValueError("EVIDENCE_SCOPE_MISMATCH: 证据不属于当前来源")
    unit = matches[0]
    if reference.start is not None:
        valid = unit["text"][reference.start:reference.end] == reference.quote
    else:
        valid = unit["text"].count(reference.quote) == 1
    if not valid:
        raise ValueError("EVIDENCE_AMBIGUOUS: 引文不唯一或与原文不符")
    return {**reference.model_dump(), "source_unit_id": str(unit["source_unit_id"])}


def validate_approval(approval: ApprovalScope, units: list[dict]) -> None:
    """证据须有印章与审批事实，但不要求编号；签字不能替代印章。"""
    for ref in [approval.declaration, approval.seal, *approval.scope]:
        validate_quote(ref, units)
    validate_approval_claim(approval)
    if approval.approval_kind == "HR_ANNUAL_EXCELLENCE":
        # 年度优秀是章内核验结论，但必须有同一页的带坐标 SEAL 区块，不能靠模型口述方章。
        matches = [unit for unit in units if approval.seal.source_unit_id in {
            str(unit["source_unit_id"]), str(unit.get("external_unit_id") or "")
        }]
        unit = matches[0] if len(matches) == 1 else None
        quote_start = (approval.seal.start if approval.seal.start is not None else
            unit["text"].index(approval.seal.quote) if unit else -1)
        if (unit is None or unit.get("kind") not in {"PDF_PAGE", "IMAGE_PAGE"} or not unit.get("page_number")
                or not any(block.get("bbox") and isinstance(block.get("start"), int)
                    and isinstance(block.get("end"), int)
                    and block["start"] <= quote_start
                    and quote_start + len(approval.seal.quote) <= block["end"]
                    and approval.seal.quote in block.get("text", "")
                    for block in unit.get("seal_blocks", []))):
            raise ValueError("APPROVAL_SEAL_REQUIRED: 年度优秀缺少同页可定位真实印章区块")
        return


def validate_approval_claim(approval: ApprovalScope) -> None:
    """只检查提交的声明与印章文字契约，供耗时目标准备前安全预检。"""
    if approval.approval_kind == "HR_ANNUAL_EXCELLENCE":
        if not re.search(r"年度考核优秀", approval.declaration.quote) or approval.declaration != approval.seal:
            raise ValueError("APPROVAL_DECLARATION_REQUIRED: 年度优秀结论必须来自印章原文")
        return
    if (not re.search(r"属实|通过|同意|审核|确认", approval.declaration.quote)
            or re.search(r"不属实|不实|未通过|不通过|不同意|未审核|待审核|未确认|待确认", approval.declaration.quote)):
        raise ValueError("APPROVAL_DECLARATION_REQUIRED: 缺少可确认的审批声明")
    if (not re.search(r"章|印", approval.seal.quote)
            or re.search(r"无章|无印章|未盖章|无盖章|未见|没有|未检测|未识别", approval.seal.quote)):
        raise ValueError("APPROVAL_SEAL_REQUIRED: 缺少宿主可验证的盖章证据")
    if re.search(r"本人承诺|本人签名", approval.declaration.quote):
        raise ValueError("APPROVAL_DECLARATION_REQUIRED: 本人承诺不能代替部门审批声明")


def validate_scope(reference: SourceQuote, approval: ApprovalScope, units: list[dict]) -> None:
    """原文证据必须落在已声明的审批范围中，不能借同页印章填写范围外内容。"""
    actual = validate_quote(reference, units)
    for scope in approval.scope:
        bound = validate_quote(scope, units)
        if actual["source_unit_id"] != bound["source_unit_id"]:
            continue
        unit = next(u for u in units if str(u["source_unit_id"]) == actual["source_unit_id"])
        start = actual["start"] if actual["start"] is not None else unit["text"].index(actual["quote"])
        scope_start = bound["start"] if bound["start"] is not None else unit["text"].index(bound["quote"])
        if scope_start <= start and start + len(actual["quote"]) <= scope_start + len(bound["quote"]):
            return
    raise ValueError("APPROVAL_SCOPE_MISMATCH: 字段或条件证据超出已确认审批范围")


def assess_field(*, label: str, assessment: FieldAssessment, approval: ApprovalScope,
                 units: list[dict]) -> dict:
    """验证审批和条件事实，确定性计算展示标记，不以盖章代替资格要求。"""
    if assessment.title != label:
        raise ValueError("FIELD_TITLE_CHANGED: 判定标题与目标列不一致")
    validate_approval(approval, units)
    results = []
    for group in assessment.groups:
        checks = []
        for check in group.checks:
            if check.criterion not in label or check.expected not in check.criterion:
                raise ValueError("REQUIREMENT_SCOPE_MISMATCH: 阈值或条件不来自目标标题")
            for ref in check.evidence:
                validate_scope(ref, approval, units)
            if not any(check.observed in ref.quote for ref in check.evidence):
                raise ValueError("REQUIREMENT_EVIDENCE_REQUIRED: 观测值没有原文支持")
            if check.operator == "CONTAINS":
                passed = check.expected in check.observed
            elif check.operator == "EQUALS":
                passed = check.expected == check.observed
            else:
                # 只比较完整数值，禁止从混杂的原文中随意挑一个数字。
                try:
                    actual, expected = Decimal(check.observed), Decimal(check.expected)
                    if not actual.is_finite() or not expected.is_finite():
                        raise InvalidOperation
                    # 80 中的 8/0 不能被当成标题阈值；不允许截取数字改变条件。
                    thresholds = re.findall(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?![\d.])", check.criterion)
                    if expected not in [Decimal(number) for number in thresholds]:
                        raise InvalidOperation
                    observations = [Decimal(number) for ref in check.evidence for number in
                        re.findall(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?![\d.])", ref.quote)]
                    if actual not in observations:
                        raise InvalidOperation
                    if (re.search(r"不少于|至少|不低于|>=|≥", check.criterion) and check.operator != "GTE"
                            or re.search(r"不多于|至多|不高于|<=|≤", check.criterion) and check.operator != "LTE"):
                        raise InvalidOperation
                except InvalidOperation as exc:
                    raise ValueError("REQUIREMENT_VALUE_INVALID: 数值条件无法确定") from exc
                passed = actual >= expected if check.operator == "GTE" else actual <= expected
            checks.append(passed)
        results.append(all(checks))
    minimum = len(results) if assessment.mode == "ALL" else 1 if assessment.mode == "ANY" else assessment.minimum
    verified = (sum(results) >= minimum and all(results[i] for i in assessment.required_groups)
                and not assessment.unresolved_reasons)
    return {"status": "VERIFIED" if verified else "UNRESOLVED" if assessment.unresolved_reasons else "NOT_MET",
            "group_results": results, "checkmark": verified,
            "assessment": assessment.model_dump(), "approval": approval.model_dump()}


def display_value(value, assessment: dict):
    """只有通过校验的文本格增加一个末尾展示标记；不改写规范化事实和证据。"""
    indexes = assessment.get("source_only_fragment_indexes") or []
    if isinstance(value, str) and indexes:
        parts = value.split("\n")
        for index in indexes:
            if not isinstance(index, int) or not 0 <= index < len(parts):
                raise ValueError("SOURCE_ONLY 展示索引与原文字段不一致")
            suffix = "〔PDF 原文，未见对应印章〕"
            if not parts[index].endswith(suffix):
                parts[index] += suffix
        value = "\n".join(parts)
    if isinstance(value, str) and value.strip() and assessment.get("checkmark"):
        return value.rstrip() if value.rstrip().endswith("√") else value.rstrip() + " √"
    return value
