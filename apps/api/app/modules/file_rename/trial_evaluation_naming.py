"""应聘试讲意见表专用文件名识别与构造。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_TRIAL_EVALUATION_PATTERN = re.compile(
    r"^(?P<title>计算机科学与工程学院应聘试讲意见表"
    r"(?:（[^）]{1,30}）|\([^)]{1,30}\))?)"
    r"\s*[-_—]\s*(?P<applicant>[\u3400-\u9fff]{2,4})"
    r"\s*[-_—]\s*(?P<reviewer>[\u3400-\u9fff]{2,4})\s*$"
)
_SOURCE_YEAR_PATTERN = re.compile(
    r"(?:^|/)(?P<year>(?:19|20)\d{2})(?:年)?(?:/|$)"
)
_PROTECTED_PARENT_PARTS = ("考查试讲表", "考察试讲表")
_TRIAL_EVALUATION_FILENAME_PREFIX = "计算机科学与工程学院应聘试讲意见表"


@dataclass(frozen=True, slots=True)
class TrialEvaluationFilenameSuggestion:
    """角色和年份可信时生成的试讲意见表标准名。"""

    filename: str
    year: str
    title: str
    applicant_name: str
    reviewer_name: str
    evidence_quote: str


def should_preserve_trial_evaluation_source_name(
    *,
    original_filename: str,
    source_relative_path: str,
) -> bool:
    """指定外来应聘考察材料目录中的试讲意见表始终保留源文件名。"""

    if not Path(original_filename).stem.strip().startswith(
        _TRIAL_EVALUATION_FILENAME_PREFIX
    ):
        return False
    parts = tuple(
        part
        for part in str(source_relative_path or "").replace("\\", "/").split("/")
        if part
    )
    parent_parts = parts[:-1]
    for index in range(len(parent_parts) - 1):
        if parent_parts[index : index + 2] != _PROTECTED_PARENT_PARTS:
            continue
        return index == 1 or "外来应聘" in parent_parts[:index]
    return False


def suggest_trial_evaluation_filename(
    *,
    original_filename: str,
    source_relative_path: str,
) -> TrialEvaluationFilenameSuggestion | None:
    """从受管源文件名保留应聘人和评议人，避免同目录意见表重名。"""

    normalized_path = str(source_relative_path or "").replace("\\", "/")
    year_match = _SOURCE_YEAR_PATTERN.search(normalized_path)
    filename_match = _TRIAL_EVALUATION_PATTERN.fullmatch(
        Path(original_filename).stem.strip()
    )
    if year_match is None or filename_match is None:
        return None
    year = year_match.group("year")
    title = filename_match.group("title")
    applicant_name = filename_match.group("applicant")
    reviewer_name = filename_match.group("reviewer")
    extension = Path(original_filename).suffix.lower()
    return TrialEvaluationFilenameSuggestion(
        filename=(
            f"{year}_{title}_应聘人{applicant_name}_评议人{reviewer_name}{extension}"
        ),
        year=year,
        title=title,
        applicant_name=applicant_name,
        reviewer_name=reviewer_name,
        evidence_quote=Path(original_filename).stem,
    )
