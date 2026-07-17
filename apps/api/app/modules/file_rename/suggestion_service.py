"""受管文件重命名建议和 OperationPlan 生成服务。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Document, ManagedFile, ManagedRoot, User
from app.modules.file_rename.batch_service import RenameBatchService
from app.modules.file_rename.filename_builder import FilenameBuildError, FilenameBuilder
from app.modules.file_rename.metadata_resolution_service import RenameMetadataResolutionService
from app.modules.file_rename.parsing_service import extract_rename_primary, rename_primary_config_hash
from app.modules.file_rename.policy_loader import load_rename_policy
from app.modules.file_rename.review_service import RenameReviewService
from app.modules.file_rename.schemas import RenameFieldResult, RenameFieldStatus, RenameSuggestion
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.managed_files.directory_scope_resolver import (
    ManagedDirectoryScopeResolution,
    ManagedDirectoryScopeResolver,
)
from app.modules.managed_files.repository import ManagedFileRepository
from app.modules.managed_files.service import resolve_managed_file_query_scope, sync_configured_managed_roots
from app.modules.managed_files.snapshot_service import ManagedFileSnapshotService


_SPREADSHEET_RENAME_SUFFIXES = {".xls", ".xlsx", ".xlsm", ".csv", ".tsv"}


class RenameSuggestionService:
    """解析受管文件并持久化可确认的重命名计划。"""

    def __init__(self, db: Session, user_id: str) -> None:
        """保存请求级数据库会话和用户边界。"""

        self.db = db
        self.user_id = user_id
        self.policy = load_rename_policy()
        self.metadata_resolution_service = RenameMetadataResolutionService()
        self.filename_builder = FilenameBuilder()

    def generate_plan(
        self,
        *,
        conversation_id: str,
        agent_run_id: str,
        root_key: str | None = None,
        path_prefix: str | None = None,
        relative_path: str | None = None,
        path_candidates: list[str] | None = None,
        scope_confidence: float | None = None,
        extension: str | None = None,
        filename_contains: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """为匹配文件生成建议，并只把 READY 项写入 OperationPlan。"""

        user = self.db.get(User, self.user_id)
        if user is None or not user.default_workspace_id:
            return _error("USER_WORKSPACE_REQUIRED", "当前用户缺少默认工作区，无法创建重命名计划。")
        scope = resolve_managed_file_query_scope(root_key=root_key, path_prefix=path_prefix)
        if scope.unresolved_root_key:
            return _error("MANAGED_ROOT_NOT_CONFIGURED", "未找到对应的受管目录配置。")
        sync_configured_managed_roots(self.db, root_key=scope.root_key, scan=True)
        repository = ManagedFileRepository(self.db)
        directory_resolution = ManagedDirectoryScopeResolver(repository).resolve(
            root_key=scope.root_key,
            configured_root_keys=scope.configured_root_keys,
            path_prefix=scope.path_prefix,
            path_candidates=path_candidates,
        )
        if directory_resolution.status != "RESOLVED":
            return _directory_scope_clarification(
                resolution=directory_resolution,
                requested_path=scope.path_prefix,
                scope_confidence=scope_confidence,
            )
        resolved_root_key = directory_resolution.root_key or scope.root_key
        resolved_path_prefix = directory_resolution.path_prefix
        if not relative_path:
            matched_total = repository.count_files(
                root_key=resolved_root_key,
                root_keys=scope.configured_root_keys if resolved_root_key is None else None,
                path_prefix=resolved_path_prefix,
                extension=extension,
                filename_contains=filename_contains,
                status="ACTIVE",
            )
            if matched_total > limit:
                return _error(
                    "RENAME_SCOPE_TOO_LARGE",
                    f"当前范围匹配到 {matched_total} 个文件，单批最多处理 {limit} 个，请缩小目录或增加过滤条件。",
                )
        rows = repository.list_files(
            root_key=resolved_root_key,
            root_keys=scope.configured_root_keys if resolved_root_key is None else None,
            path_prefix=resolved_path_prefix,
            extension=extension,
            filename_contains=filename_contains,
            status="ACTIVE",
            limit=limit,
        )
        if relative_path:
            normalized_relative_path = relative_path.replace("\\", "/").strip().strip("/")
            rows = [
                (managed_file, root)
                for managed_file, root in rows
                if managed_file.relative_path == normalized_relative_path
            ]
        if not rows:
            return _error("NO_MANAGED_FILES_FOUND", "没有找到符合条件的受管文件。")

        suggestions: list[RenameSuggestion] = []
        extraction_results: list[dict[str, Any]] = []
        prepared_suggestions: list[tuple[RenameSuggestion, ManagedFile, ManagedRoot]] = []
        for managed_file, root in rows:
            suggestion, extraction_result = self._suggest_one(managed_file=managed_file, root=root)
            suggestion = suggestion.model_copy(
                update={
                    "extension": managed_file.extension,
                    "size_bytes": managed_file.size_bytes,
                    "managed_status": managed_file.status,
                }
            )
            prepared_suggestions.append((suggestion, managed_file, root))
            if extraction_result:
                extraction_results.append(extraction_result)

        prepared_suggestions = self._apply_duplicate_title_dates(prepared_suggestions)
        reserved_targets: set[tuple[str, str]] = set()
        reserved_logical_targets: set[tuple[str, str]] = set()
        for suggestion, managed_file, root in prepared_suggestions:
            suggestion = self._resolve_target_conflict(
                suggestion=suggestion,
                managed_file=managed_file,
                root=root,
                reserved_targets=reserved_targets,
                reserved_logical_targets=reserved_logical_targets,
            )
            suggestions.append(suggestion)

        ready = [item for item in suggestions if item.status == "READY"]
        skipped = [item for item in suggestions if item.status != "READY"]
        scope_payload = {
            "root_key": resolved_root_key,
            "path_prefix": resolved_path_prefix,
            "relative_path": relative_path,
            "extension": extension,
            "filename_contains": filename_contains,
            "policy_key": self.policy.policy_key,
            "policy_version": self.policy.version,
        }
        batch_service = RenameBatchService(self.db, self.user_id)
        batch = batch_service.create_batch(
            conversation_id=conversation_id,
            agent_run_id=agent_run_id,
            scope=scope_payload,
        )
        batch_items = {
            suggestion.managed_file_id: batch_service.add_suggestion(
                batch=batch,
                suggestion=suggestion,
                position=position,
            )
            for position, suggestion in enumerate(suggestions)
        }
        review_service = RenameReviewService(self.db, self.user_id)
        for suggestion in skipped:
            review_item = review_service.persist_suggestion(
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
                suggestion=suggestion,
                rename_batch_id=batch.id,
                rename_batch_item_id=batch_items[suggestion.managed_file_id].id,
            )
            suggestion.review_id = review_item.id
        plan = batch_service.create_operation_plan_if_complete(batch)
        preview_suggestions = (skipped + ready)[:10]
        self.db.flush()
        return {
            "ok": True,
            "kind": "rename_plan",
            "status": "WAITING_CONFIRMATION" if plan is not None else "NEEDS_REVIEW",
            "operation_plan_id": plan.id if plan is not None else None,
            "rename_batch_id": batch.id,
            "matched_count": len(suggestions),
            "ready_count": len(ready),
            "needs_review_count": len(skipped),
            "suggestions": [item.model_dump(mode="json") for item in preview_suggestions],
            "suggestions_truncated": len(suggestions) > len(preview_suggestions),
            "extraction_results": extraction_results,
            "query": {
                **scope_payload,
                "path_candidates": path_candidates or [],
                "scope_confidence": scope_confidence,
            },
        }

    def _suggest_one(
        self,
        *,
        managed_file: ManagedFile,
        root: ManagedRoot,
    ) -> tuple[RenameSuggestion, dict[str, Any] | None]:
        """为一个受管文件生成建议，异常只影响当前文件。"""

        empty_field = RenameFieldResult(status=RenameFieldStatus.MISSING)
        if root.read_only or "rename" not in set(root.allowed_operations_json or []):
            return (
                RenameSuggestion(
                    managed_file_id=managed_file.id,
                    root_key=root.root_key,
                    relative_path=managed_file.relative_path,
                    filename=managed_file.filename,
                    year=empty_field,
                    document_number=empty_field,
                    title=empty_field,
                    policy_key=self.policy.policy_key,
                    policy_version=self.policy.version,
                    status="NEEDS_REVIEW",
                    errors=[{"code": "RENAME_NOT_ALLOWED", "message": "该受管目录未启用重命名操作。"}],
                ),
                None,
            )

        try:
            (
                extraction_result,
                pages,
                elements,
                source_sha256,
                document_id,
                metadata_filename,
            ) = self._extract_managed_file(
                managed_file=managed_file, root=root
            )
            if extraction_result.get("status") != "COMPLETED":
                error = extraction_result.get("error") or {}
                filename_metadata = self.metadata_resolution_service.metadata_extractor.extract(
                    filename=metadata_filename,
                    pages=[],
                    elements=[],
                )
                if (
                    Path(managed_file.filename).suffix.lower() in _SPREADSHEET_RENAME_SUFFIXES
                    and filename_metadata.can_build_filename
                ):
                    proposed_filename, template_key = self.filename_builder.build(
                        original_filename=managed_file.filename,
                        metadata=filename_metadata,
                        policy=self.policy,
                    )
                    proposed_relative_path = (
                        Path(managed_file.relative_path).parent / proposed_filename
                    ).as_posix()
                    conflict = self._target_conflict(
                        managed_file=managed_file,
                        root=root,
                        proposed_relative_path=proposed_relative_path,
                    )
                    return (
                        RenameSuggestion(
                            managed_file_id=managed_file.id,
                            document_id=document_id,
                            root_key=root.root_key,
                            relative_path=managed_file.relative_path,
                            filename=managed_file.filename,
                            proposed_relative_path=proposed_relative_path,
                            proposed_filename=proposed_filename,
                            source_sha256=source_sha256,
                            document_date=filename_metadata.document_date,
                            year=filename_metadata.year,
                            document_number=filename_metadata.document_number,
                            title=filename_metadata.title,
                            policy_key=self.policy.policy_key,
                            policy_version=self.policy.version,
                            template_key=template_key,
                            status="CONFLICT" if conflict else "READY",
                            warnings=["表格正文解析失败，已使用结构化文件名生成待确认建议。"],
                            errors=(
                                [{"code": "TARGET_ALREADY_EXISTS", "message": "目标文件名已存在。"}]
                                if conflict
                                else []
                            ),
                        ),
                        extraction_result,
                    )
                return (
                    RenameSuggestion(
                        managed_file_id=managed_file.id,
                        document_id=document_id,
                        root_key=root.root_key,
                        relative_path=managed_file.relative_path,
                        filename=managed_file.filename,
                        source_sha256=source_sha256,
                        year=empty_field,
                        document_number=empty_field,
                        title=empty_field,
                        policy_key=self.policy.policy_key,
                        policy_version=self.policy.version,
                        status="NEEDS_REVIEW",
                        errors=[{
                            "code": str(error.get("code") or "EXTRACTION_FAILED"),
                            "message": str(error.get("message") or "文件正文解析失败。"),
                        }],
                    ),
                    extraction_result,
                )
            document = self.db.get(Document, document_id)
            resolved_source = (
                FileExtractionRepository(self.db, self.user_id).resolve_original_file_for_document(document)
                if document is not None
                else {"ok": False}
            )
            metadata_resolution = self.metadata_resolution_service.resolve(
                file_path=resolved_source.get("file_path") if resolved_source.get("ok") else None,
                filename=metadata_filename,
                content_type=document.content_type if document is not None else "",
                primary_result=extraction_result,
                primary_pages=pages,
                primary_elements=elements,
            )
            metadata = metadata_resolution.metadata
            arbitration_warnings = metadata_resolution.warnings
            warning_messages = [str(item.get("message") or "") for item in arbitration_warnings if item.get("message")]
            extraction_result["rename_parse_mode"] = metadata_resolution.mode
            extraction_result["rename_candidate_parsers"] = metadata_resolution.candidate_parsers
            extraction_result["rename_arbitration_warnings"] = arbitration_warnings
            if not metadata.can_build_filename:
                return (
                    RenameSuggestion(
                        managed_file_id=managed_file.id,
                        document_id=document_id,
                        root_key=root.root_key,
                        relative_path=managed_file.relative_path,
                        filename=managed_file.filename,
                        source_sha256=source_sha256,
                        year=metadata.year,
                        document_number=metadata.document_number,
                        title=metadata.title,
                        policy_key=self.policy.policy_key,
                        policy_version=self.policy.version,
                        status="NEEDS_REVIEW",
                        warnings=["正文标题缺失或存在歧义，当前批次等待用户复核。", *warning_messages],
                        rename_parse_mode=metadata_resolution.mode,
                        rename_candidate_parsers=metadata_resolution.candidate_parsers,
                        arbitration_warnings=arbitration_warnings,
                    ),
                    extraction_result,
                )
            proposed_filename, template_key = self.filename_builder.build(
                original_filename=managed_file.filename,
                metadata=metadata,
                policy=self.policy,
            )
            proposed_relative_path = (Path(managed_file.relative_path).parent / proposed_filename).as_posix()
            conflict = self._target_conflict(
                managed_file=managed_file,
                root=root,
                proposed_relative_path=proposed_relative_path,
            )
            return (
                RenameSuggestion(
                    managed_file_id=managed_file.id,
                    document_id=document_id,
                    root_key=root.root_key,
                    relative_path=managed_file.relative_path,
                    filename=managed_file.filename,
                    proposed_relative_path=proposed_relative_path,
                    proposed_filename=proposed_filename,
                    source_sha256=source_sha256,
                    document_date=metadata.document_date,
                    year=metadata.year,
                    document_number=metadata.document_number,
                    title=metadata.title,
                    policy_key=self.policy.policy_key,
                    policy_version=self.policy.version,
                    template_key=template_key,
                    status="CONFLICT" if conflict else "READY",
                    warnings=warning_messages,
                    rename_parse_mode=metadata_resolution.mode,
                    rename_candidate_parsers=metadata_resolution.candidate_parsers,
                    arbitration_warnings=arbitration_warnings,
                    errors=[{"code": "TARGET_ALREADY_EXISTS", "message": "目标文件名已存在。"}] if conflict else [],
                ),
                extraction_result,
            )
        except (FilenameBuildError, OSError, RuntimeError, ValueError) as exc:
            return (
                RenameSuggestion(
                    managed_file_id=managed_file.id,
                    root_key=root.root_key,
                    relative_path=managed_file.relative_path,
                    filename=managed_file.filename,
                    year=empty_field,
                    document_number=empty_field,
                    title=empty_field,
                    policy_key=self.policy.policy_key,
                    policy_version=self.policy.version,
                    status="FAILED",
                    errors=[{"code": exc.__class__.__name__, "message": str(exc)}],
                ),
                None,
            )

    def _extract_managed_file(
        self,
        *,
        managed_file: ManagedFile,
        root: ManagedRoot,
    ) -> tuple[dict[str, Any], list[Any], list[Any], str, str, str]:
        """创建或复用当前用户快照，并返回完整页面正文和结构化元素。"""

        resolution = ManagedFileSnapshotService(self.db, self.user_id).resolve(
            managed_file=managed_file,
            root=root,
        )
        repository = FileExtractionRepository(self.db, self.user_id)
        parser_config_hash = rename_primary_config_hash(filename=resolution.document.original_filename)
        reusable = repository.get_latest_successful_extraction(
            document_id=resolution.document.id,
            parser_config_hash=parser_config_hash,
        )
        if reusable is None:
            resolved = repository.resolve_original_file_for_document(resolution.document)
            if not resolved.get("ok"):
                error = resolved.get("error") or {}
                return (
                    _failed_extraction_result(
                        document_id=resolution.document.id,
                        managed_file=managed_file,
                        root=root,
                        error=error,
                    ),
                    [],
                    [],
                    resolution.source_sha256,
                    resolution.document.id,
                    resolution.document.original_filename,
                )
            try:
                extraction = extract_rename_primary(
                    file_path=resolved["file_path"],
                    filename=resolution.document.original_filename,
                    content_type=resolution.document.content_type,
                )
            except Exception as exc:
                # 单个损坏文件不能中断整个重命名批次；异常需落入解析运行和逐文件回执。
                run = repository.create_extraction_run(
                    document_id=resolution.document.id,
                    extractor="file-rename",
                    parser_config_hash=parser_config_hash or "",
                )
                repository.fail_extraction_run(run=run, error_message=str(exc))
                return (
                    _failed_extraction_result(
                        document_id=resolution.document.id,
                        managed_file=managed_file,
                        root=root,
                        error={
                            "code": "DOCUMENT_EXTRACTION_EXCEPTION",
                            "message": f"文件解析异常：{exc}",
                        },
                        extraction_run_id=run.id,
                        extractor=run.extractor,
                    ),
                    [],
                    [],
                    resolution.source_sha256,
                    resolution.document.id,
                    resolution.document.original_filename,
                )
            run = repository.create_extraction_run(
                document_id=resolution.document.id,
                extractor=extraction["extractor"],
                parser_name=extraction.get("parser_name", ""),
                parser_version=extraction.get("parser_version", ""),
                parser_config_hash=extraction.get("parser_config_hash", ""),
            )
            if extraction.get("ok"):
                repository.complete_extraction_run(
                    run=run,
                    pages=extraction.get("pages", []),
                    elements=extraction.get("elements", []),
                )
                reusable = repository.get_latest_successful_extraction(
                    document_id=resolution.document.id,
                    parser_config_hash=parser_config_hash,
                )
            else:
                repository.fail_extraction_run(
                    run=run,
                    error_message=str((extraction.get("error") or {}).get("message") or "解析失败"),
                )
                return (
                    _failed_extraction_result(
                        document_id=resolution.document.id,
                        managed_file=managed_file,
                        root=root,
                        error=extraction.get("error") or {},
                        extraction_run_id=run.id,
                        extractor=run.extractor,
                    ),
                    [],
                    [],
                    resolution.source_sha256,
                    resolution.document.id,
                    resolution.document.original_filename,
                )
        run = reusable["run"]
        pages = reusable["pages"]
        elements = reusable.get("elements", [])
        result = {
            "ok": True,
            "document_id": resolution.document.id,
            "extraction_run_id": run.id,
            "status": "COMPLETED",
            "extractor": run.extractor,
            "parser_name": run.parser_name,
            "reused": resolution.snapshot_status == "REUSED",
            "pages": [
                {
                    "page_number": page.page_number,
                    "sheet_name": page.sheet_name,
                    "text_preview": page.text_content[:300],
                    "char_count": len(page.text_content),
                    "metadata": page.metadata_json,
                }
                for page in pages
            ],
            "managed_file": {
                "root_key": root.root_key,
                "relative_path": managed_file.relative_path,
                "filename": managed_file.filename,
                "extension": managed_file.extension,
                "size_bytes": managed_file.size_bytes,
                "status": managed_file.status,
            },
            "source": "generate-rename-suggestions",
            "source_kind": "managed_file",
            "managed_file_id": managed_file.id,
            "root_key": root.root_key,
            "relative_path": managed_file.relative_path,
            "source_sha256": resolution.source_sha256,
            "structured_element_count": len(elements),
            "source_filename": resolution.document.original_filename,
        }
        return (
            result,
            pages,
            elements,
            resolution.source_sha256,
            resolution.document.id,
            resolution.document.original_filename,
        )

    def _target_conflict(
        self,
        *,
        managed_file: ManagedFile,
        root: ManagedRoot,
        proposed_relative_path: str,
    ) -> bool:
        """同时检查文件系统和 managed_files 唯一索引冲突。"""

        if proposed_relative_path == managed_file.relative_path:
            return False
        target_path = Path(root.container_path) / Path(proposed_relative_path)
        if target_path.exists():
            return True
        path_hash = hashlib.sha256(proposed_relative_path.encode("utf-8")).hexdigest()
        return (
            self.db.query(ManagedFile.id)
            .filter(
                ManagedFile.root_id == root.id,
                ManagedFile.relative_path_hash == path_hash,
                ManagedFile.id != managed_file.id,
            )
            .first()
            is not None
        )

    def _apply_duplicate_title_dates(
        self,
        items: list[tuple[RenameSuggestion, ManagedFile, ManagedRoot]],
    ) -> list[tuple[RenameSuggestion, ManagedFile, ManagedRoot]]:
        """同目录同基础名称使用完整日期区分，扩展名不参与标题分组。"""

        if self.policy.duplicate_title_strategy != "FULL_DATE_THEN_VERSION":
            return items
        groups: dict[tuple[str, str], list[int]] = {}
        for index, (suggestion, _, root) in enumerate(items):
            if suggestion.status not in {"READY", "CONFLICT"} or not suggestion.proposed_relative_path:
                continue
            target = Path(suggestion.proposed_relative_path)
            logical_path = (target.parent / target.stem).as_posix().casefold()
            groups.setdefault((root.id, logical_path), []).append(index)

        updated = list(items)
        for indexes in groups.values():
            if len(indexes) < 2:
                continue
            group = [items[index] for index in indexes]
            if not all(_resolved_full_date(suggestion.document_date) for suggestion, _, _ in group):
                continue
            for index in indexes:
                suggestion, managed_file, root = updated[index]
                document_date = str(suggestion.document_date.value)
                target = Path(str(suggestion.proposed_relative_path))
                dated_filename = _replace_year_prefix_with_date(
                    filename=target.name,
                    year=str(suggestion.year.value or ""),
                    document_date=document_date,
                    separator=self.policy.separator,
                )
                dated_relative_path = (target.parent / dated_filename).as_posix()
                updated[index] = (
                    suggestion.model_copy(
                        update={
                            "proposed_filename": dated_filename,
                            "proposed_relative_path": dated_relative_path,
                            "warnings": [
                                *suggestion.warnings,
                                "检测到同目录同标题文件，已使用精确到日的日期区分。",
                            ],
                        }
                    ),
                    managed_file,
                    root,
                )
        return updated

    def _resolve_target_conflict(
        self,
        *,
        suggestion: RenameSuggestion,
        managed_file: ManagedFile,
        root: ManagedRoot,
        reserved_targets: set[tuple[str, str]],
        reserved_logical_targets: set[tuple[str, str]],
    ) -> RenameSuggestion:
        """按策略处理文件系统、索引和本批次内的目标名称冲突。"""

        proposed_relative_path = suggestion.proposed_relative_path
        if not proposed_relative_path or suggestion.status not in {"READY", "CONFLICT"}:
            return suggestion
        target_key = (root.id, proposed_relative_path)
        logical_target_key = (root.id, _logical_target_path(proposed_relative_path))
        has_conflict = (
            target_key in reserved_targets
            or logical_target_key in reserved_logical_targets
            or self._target_conflict(
                managed_file=managed_file,
                root=root,
                proposed_relative_path=proposed_relative_path,
            )
        )
        if not has_conflict:
            reserved_targets.add(target_key)
            reserved_logical_targets.add(logical_target_key)
            return suggestion.model_copy(update={"status": "READY", "errors": []})
        if self.policy.conflict_strategy != "VERSION_SUFFIX":
            return suggestion.model_copy(
                update={
                    "status": "CONFLICT",
                    "errors": [{"code": "TARGET_ALREADY_EXISTS", "message": "目标文件名已存在。"}],
                }
            )

        original_target = Path(proposed_relative_path)
        for version in range(2, 1001):
            versioned_filename = _append_version_suffix(
                original_target.name,
                version=version,
                max_bytes=self.policy.max_filename_bytes,
            )
            versioned_relative_path = (original_target.parent / versioned_filename).as_posix()
            versioned_key = (root.id, versioned_relative_path)
            versioned_logical_key = (root.id, _logical_target_path(versioned_relative_path))
            if versioned_key in reserved_targets:
                continue
            if versioned_logical_key in reserved_logical_targets:
                continue
            if self._target_conflict(
                managed_file=managed_file,
                root=root,
                proposed_relative_path=versioned_relative_path,
            ):
                continue
            reserved_targets.add(versioned_key)
            reserved_logical_targets.add(versioned_logical_key)
            return suggestion.model_copy(
                update={
                    "proposed_relative_path": versioned_relative_path,
                    "proposed_filename": versioned_filename,
                    "status": "READY",
                    "warnings": [
                        *suggestion.warnings,
                        f"基础目标名称已存在，已按版本规则生成第{_chinese_number(version)}版。",
                    ],
                    "errors": [],
                }
            )
        return suggestion.model_copy(
            update={
                "status": "CONFLICT",
                "errors": [{"code": "VERSION_SUFFIX_EXHAUSTED", "message": "目标文件版本号已达到上限。"}],
            }
        )


def _append_version_suffix(filename: str, *, version: int, max_bytes: int) -> str:
    """在扩展名前追加中文版本号，并在需要时安全截断原名称。"""

    extension = Path(filename).suffix
    stem = filename[: -len(extension)] if extension else filename
    version_suffix = f"_第{_chinese_number(version)}版"
    reserved_bytes = len(f"{version_suffix}{extension}".encode("utf-8"))
    available_bytes = max_bytes - reserved_bytes
    if available_bytes <= 0:
        raise ValueError("文件名长度限制不足以保存版本后缀。")
    encoded_stem = stem.encode("utf-8")[:available_bytes]
    while encoded_stem:
        try:
            safe_stem = encoded_stem.decode("utf-8").rstrip(" ._-")
            return f"{safe_stem}{version_suffix}{extension}"
        except UnicodeDecodeError:
            encoded_stem = encoded_stem[:-1]
    raise ValueError("原文件名无法在长度限制内追加版本后缀。")


def _resolved_full_date(field: RenameFieldResult) -> bool:
    """判断日期字段是否已经解析为 YYYYMMDD。"""

    return (
        field.status == RenameFieldStatus.RESOLVED
        and bool(field.value)
        and len(str(field.value)) == 8
        and str(field.value).isdigit()
    )


def _replace_year_prefix_with_date(
    *,
    filename: str,
    year: str,
    document_date: str,
    separator: str,
) -> str:
    """将模板生成的年份前缀提升为完整日期，不改变标题和扩展名。"""

    extension = Path(filename).suffix
    stem = filename[: -len(extension)] if extension else filename
    year_prefix = f"{year}{separator}" if year else ""
    if year_prefix and stem.startswith(year_prefix):
        stem = f"{document_date}{separator}{stem[len(year_prefix):]}"
    else:
        stem = f"{document_date}{separator}{stem}"
    return f"{stem}{extension}"


def _logical_target_path(relative_path: str) -> str:
    """生成忽略扩展名的逻辑目标键，避免同名跨格式文件难以区分。"""

    target = Path(relative_path)
    return (target.parent / target.stem).as_posix().casefold()


def _chinese_number(value: int) -> str:
    """把 1 到 999 的版本号转换为简体中文数字。"""

    if value <= 0 or value >= 1000:
        return str(value)
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    if value < 20:
        return f"十{digits[value % 10] if value % 10 else ''}"
    if value < 100:
        tens, ones = divmod(value, 10)
        return f"{digits[tens]}十{digits[ones] if ones else ''}"
    hundreds, remainder = divmod(value, 100)
    if remainder == 0:
        return f"{digits[hundreds]}百"
    if remainder < 10:
        return f"{digits[hundreds]}百零{digits[remainder]}"
    return f"{digits[hundreds]}百{_chinese_number(remainder)}"


def _operation_plan_item(suggestion: RenameSuggestion) -> dict[str, Any]:
    """把 READY 建议转换成受控 OperationPlan item。"""

    return {
        "document_id": suggestion.document_id,
        "before": {
            "managed_file_id": suggestion.managed_file_id,
            "root_key": suggestion.root_key,
            "relative_path": suggestion.relative_path,
            "filename": suggestion.filename,
            "source_sha256": suggestion.source_sha256,
        },
        "after": {
            "relative_path": suggestion.proposed_relative_path,
            "filename": suggestion.proposed_filename,
        },
        "rename_metadata": {
            "policy_key": suggestion.policy_key,
            "policy_version": suggestion.policy_version,
            "template_key": suggestion.template_key,
            "document_date": suggestion.document_date.model_dump(mode="json"),
            "year": suggestion.year.model_dump(mode="json"),
            "document_number": suggestion.document_number.model_dump(mode="json"),
            "title": suggestion.title.model_dump(mode="json"),
            "parse_mode": suggestion.rename_parse_mode,
            "candidate_parsers": suggestion.rename_candidate_parsers,
            "arbitration_warnings": suggestion.arbitration_warnings,
        },
        "execution_status": "PLANNED",
    }


def _failed_extraction_result(
    *,
    document_id: str,
    managed_file: ManagedFile,
    root: ManagedRoot,
    error: dict[str, Any],
    extraction_run_id: str = "",
    extractor: str = "managed-file",
) -> dict[str, Any]:
    """构造 Graph 可聚合的解析失败结果。"""

    return {
        "ok": False,
        "document_id": document_id,
        "extraction_run_id": extraction_run_id or f"failed-rename-{managed_file.id}",
        "status": "FAILED",
        "extractor": extractor,
        "pages": [],
        "error": {
            "code": str(error.get("code") or "EXTRACTION_FAILED"),
            "message": str(error.get("message") or "文件解析失败。"),
        },
        "managed_file": {
            "root_key": root.root_key,
            "relative_path": managed_file.relative_path,
            "filename": managed_file.filename,
            "extension": managed_file.extension,
            "size_bytes": managed_file.size_bytes,
            "status": managed_file.status,
        },
        "source": "generate-rename-suggestions",
        "source_kind": "managed_file",
        "managed_file_id": managed_file.id,
    }


def _directory_scope_clarification(
    *,
    resolution: ManagedDirectoryScopeResolution,
    requested_path: str | None,
    scope_confidence: float | None,
) -> dict[str, Any]:
    """构造不会触发文件解析或 OperationPlan 的目录澄清结果。"""

    candidates = [candidate.to_dict() for candidate in resolution.candidates]
    if candidates:
        candidate_lines = "\n".join(
            f"{index}. {candidate['display_path']}"
            for index, candidate in enumerate(candidates, start=1)
        )
        message = (
            "无法唯一确定要处理的目录，请使用完整目录路径重新发送，例如："
            "“对校办/2024目录下的文件进行重命名”。\n"
            f"匹配到的目录：\n{candidate_lines}"
        )
    else:
        requested_label = f"“{requested_path}”" if requested_path else "所述目录"
        message = (
            f"未找到{requested_label}对应的受管目录。"
            "请使用 `/` 提供完整相对路径后重新发送。"
        )
    return {
        "ok": False,
        "kind": "rename_plan",
        "status": "NEEDS_CLARIFICATION",
        "error": {
            "code": resolution.error_code or "MANAGED_DIRECTORY_SCOPE_AMBIGUOUS",
            "message": message,
        },
        "scope_candidates": candidates,
        "scope_confidence": scope_confidence,
        "suggestions": [],
        "extraction_results": [],
    }


def _error(code: str, message: str) -> dict[str, Any]:
    """构造 Tool 结构化错误。"""

    return {
        "ok": False,
        "kind": "rename_plan",
        "status": "FAILED",
        "error": {"code": code, "message": message},
        "suggestions": [],
        "extraction_results": [],
    }
