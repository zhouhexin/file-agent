"""受管文件重命名建议和 OperationPlan 生成服务。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import ManagedFile, ManagedRoot, User
from app.modules.file_rename.filename_builder import FilenameBuildError, FilenameBuilder
from app.modules.file_rename.metadata_extractor import FilenameMetadataExtractor
from app.modules.file_rename.policy_loader import load_rename_policy
from app.modules.file_rename.schemas import RenameFieldResult, RenameFieldStatus, RenameSuggestion
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.extractors import extract_document_text
from app.modules.managed_files.repository import ManagedFileRepository
from app.modules.managed_files.service import resolve_managed_file_query_scope, sync_configured_managed_roots
from app.modules.managed_files.snapshot_service import ManagedFileSnapshotService
from app.modules.operations.repository import OperationPlanRepository


class RenameSuggestionService:
    """解析受管文件并持久化可确认的重命名计划。"""

    def __init__(self, db: Session, user_id: str) -> None:
        """保存请求级数据库会话和用户边界。"""

        self.db = db
        self.user_id = user_id
        self.policy = load_rename_policy()
        self.metadata_extractor = FilenameMetadataExtractor()
        self.filename_builder = FilenameBuilder()

    def generate_plan(
        self,
        *,
        conversation_id: str,
        agent_run_id: str,
        root_key: str | None = None,
        path_prefix: str | None = None,
        extension: str | None = None,
        filename_contains: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """为匹配文件生成建议，并只把 READY 项写入 OperationPlan。"""

        user = self.db.get(User, self.user_id)
        if user is None or not user.default_workspace_id:
            return _error("USER_WORKSPACE_REQUIRED", "当前用户缺少默认工作区，无法创建重命名计划。")
        scope = resolve_managed_file_query_scope(root_key=root_key, path_prefix=path_prefix)
        if scope.unresolved_root_key:
            return _error("MANAGED_ROOT_NOT_CONFIGURED", "未找到对应的受管目录配置。")
        sync_configured_managed_roots(self.db, root_key=scope.root_key, scan=True)
        rows = ManagedFileRepository(self.db).list_files(
            root_key=scope.root_key,
            root_keys=scope.configured_root_keys,
            path_prefix=scope.path_prefix,
            extension=extension,
            filename_contains=filename_contains,
            status="ACTIVE",
            limit=limit,
        )
        if not rows:
            return _error("NO_MANAGED_FILES_FOUND", "没有找到符合条件的受管文件。")

        suggestions: list[RenameSuggestion] = []
        extraction_results: list[dict[str, Any]] = []
        for managed_file, root in rows:
            suggestion, extraction_result = self._suggest_one(managed_file=managed_file, root=root)
            suggestions.append(suggestion)
            if extraction_result:
                extraction_results.append(extraction_result)

        ready = [item for item in suggestions if item.status == "READY"]
        skipped = [item for item in suggestions if item.status != "READY"]
        plan = None
        if ready:
            plan = OperationPlanRepository(self.db).create_plan(
                workspace_id=user.default_workspace_id,
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
                user_id=self.user_id,
                operation_type="RENAME_FILES",
                risk_level="medium",
                reason="按年份、文号和正文标题生成标准文件名",
                plan_json={
                    "policy_key": self.policy.policy_key,
                    "policy_version": self.policy.version,
                    "items": [_operation_plan_item(item) for item in ready],
                    "skipped_items": [item.model_dump(mode="json") for item in skipped],
                },
            )
        self.db.flush()
        return {
            "ok": True,
            "kind": "rename_plan",
            "status": "WAITING_CONFIRMATION" if plan is not None else "NEEDS_REVIEW",
            "operation_plan_id": plan.id if plan is not None else None,
            "matched_count": len(suggestions),
            "ready_count": len(ready),
            "needs_review_count": len(skipped),
            "suggestions": [item.model_dump(mode="json") for item in suggestions],
            "extraction_results": extraction_results,
            "query": {
                "root_key": scope.root_key,
                "path_prefix": scope.path_prefix,
                "extension": extension,
                "filename_contains": filename_contains,
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
            extraction_result, pages, source_sha256, document_id = self._extract_managed_file(
                managed_file=managed_file,
                root=root,
            )
            if extraction_result.get("status") != "COMPLETED":
                error = extraction_result.get("error") or {}
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
            metadata = self.metadata_extractor.extract(filename=managed_file.filename, pages=pages)
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
                        warnings=["年份或正文标题缺失，已从可执行批次中跳过。"],
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
                    year=metadata.year,
                    document_number=metadata.document_number,
                    title=metadata.title,
                    policy_key=self.policy.policy_key,
                    policy_version=self.policy.version,
                    template_key=template_key,
                    status="CONFLICT" if conflict else "READY",
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
    ) -> tuple[dict[str, Any], list[Any], str, str]:
        """创建或复用当前用户快照，并返回完整页面正文。"""

        resolution = ManagedFileSnapshotService(self.db, self.user_id).resolve(
            managed_file=managed_file,
            root=root,
        )
        repository = FileExtractionRepository(self.db, self.user_id)
        reusable = repository.get_latest_successful_extraction(document_id=resolution.document.id)
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
                    resolution.source_sha256,
                    resolution.document.id,
                )
            extraction = extract_document_text(
                file_path=resolved["file_path"],
                filename=resolution.document.original_filename,
                content_type=resolution.document.content_type,
            )
            run = repository.create_extraction_run(
                document_id=resolution.document.id,
                extractor=extraction["extractor"],
            )
            if extraction.get("ok"):
                repository.complete_extraction_run(run=run, pages=extraction.get("pages", []))
                reusable = repository.get_latest_successful_extraction(document_id=resolution.document.id)
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
                    resolution.source_sha256,
                    resolution.document.id,
                )
        run = reusable["run"]
        pages = reusable["pages"]
        result = {
            "ok": True,
            "document_id": resolution.document.id,
            "extraction_run_id": run.id,
            "status": "COMPLETED",
            "extractor": run.extractor,
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
        }
        return result, pages, resolution.source_sha256, resolution.document.id

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
            "year": suggestion.year.model_dump(mode="json"),
            "document_number": suggestion.document_number.model_dump(mode="json"),
            "title": suggestion.title.model_dump(mode="json"),
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

