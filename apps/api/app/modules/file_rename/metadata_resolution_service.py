"""编排重命名多解析器候选、字段提取和仲裁。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.modules.file_rename.metadata_arbitrator import (
    RenameMetadataArbitrator,
    RenameMetadataCandidate,
)
from app.modules.file_rename.metadata_extractor import FilenameMetadataExtractor
from app.modules.file_rename.parsing_service import RenameParsingService
from app.modules.file_rename.schemas import FilenameMetadataResult


@dataclass(frozen=True)
class RenameMetadataResolution:
    """最终命名字段、解析来源和结构化警告。"""

    metadata: FilenameMetadataResult
    mode: str
    candidate_parsers: list[str]
    warnings: list[dict[str, Any]] = field(default_factory=list)


class RenameMetadataResolutionService:
    """集中执行解析器候选收集和字段级仲裁。"""

    def __init__(self) -> None:
        self.parsing_service = RenameParsingService()
        self.metadata_extractor = FilenameMetadataExtractor()
        self.arbitrator = RenameMetadataArbitrator()

    def resolve(
        self,
        *,
        file_path: Path | None,
        filename: str,
        content_type: str,
        primary_result: dict[str, Any] | None,
        primary_pages: list[Any],
        primary_elements: list[Any],
    ) -> RenameMetadataResolution:
        """对每个解析器独立提取字段，再执行逐字段仲裁。"""

        parsing = self.parsing_service.collect(
            file_path=file_path,
            filename=filename,
            content_type=content_type,
            primary_result=primary_result,
            primary_pages=primary_pages,
            primary_elements=primary_elements,
        )
        metadata_candidates = [
            RenameMetadataCandidate(
                parser_name=candidate.parser_name,
                metadata=self.metadata_extractor.extract(
                    filename=filename,
                    pages=candidate.pages,
                    elements=candidate.elements,
                    parser_name=candidate.parser_name,
                ),
            )
            for candidate in parsing.candidates
        ]
        arbitration = self.arbitrator.arbitrate(metadata_candidates)
        return RenameMetadataResolution(
            metadata=arbitration.metadata,
            mode=parsing.mode,
            candidate_parsers=[candidate.parser_name for candidate in parsing.candidates],
            warnings=[*parsing.warnings, *arbitration.warnings],
        )
