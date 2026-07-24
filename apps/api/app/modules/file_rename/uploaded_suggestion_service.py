"""上传来源文件的工作副本重命名建议服务。

本模块把当前用户明确附件解析为已经异步归档并导入的工作副本，通过受控解析结果生成
basename 建议和 OperationPlan。它不推断分类、不修改上传暂存或受管原始目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import (
    AgentRun,
    Document,
    DocumentVersion,
    ManagedFileSnapshot,
    UploadArchiveRecord,
    User,
    WorkingCopy,
)
from app.modules.file_lifecycle.operations import WorkingCopyOperationService
from app.modules.file_rename.filename_builder import FilenameBuildError, FilenameBuilder
from app.modules.file_rename.metadata_resolution_service import RenameMetadataResolutionService
from app.modules.file_rename.parsing_service import extract_rename_primary
from app.modules.file_rename.policy_loader import load_rename_policy
from app.modules.file_rename.schemas import RenameFieldResult, RenameFieldStatus
from app.modules.file_rename.validation_service import RenameValidationService
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.readable_source import ReadableDocumentSourceResolver, apply_readable_source_metadata
from app.modules.operations.schemas import OperationPlanCreateRequest, OperationPlanItem
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


class UploadedRenameSuggestionService:
    """为上传来源文件生成只作用于工作副本的待确认重命名计划。"""

    def __init__(
        self,
        db: Session,
        user_id: str,
        validation_service: RenameValidationService | None = None,
    ) -> None:
        """保存请求级数据库会话和用户边界。"""

        self.db = db
        self.user_id = user_id
        self.policy = load_rename_policy()
        self.metadata_resolution_service = RenameMetadataResolutionService()
        self.readable_source_resolver = ReadableDocumentSourceResolver(db=db)
        self.filename_builder = FilenameBuilder()
        self.validation_service = validation_service or RenameValidationService()
        self._llm_validation_calls = 0

    def generate_plan(
        self,
        *,
        conversation_id: str,
        agent_run_id: str,
        document_ids: list[str],
        limit: int = 20,
    ) -> dict[str, Any]:
        """解析明确附件对应的工作副本并创建重命名 OperationPlan。"""

        user = self.db.get(User, self.user_id)
        if user is None:
            return _error("USER_NOT_FOUND", "当前用户不存在，无法创建重命名计划。")
        run = self.db.get(AgentRun, agent_run_id)
        if run is None or run.user_id != self.user_id or run.conversation_id != conversation_id:
            return _error("AGENT_RUN_SCOPE_INVALID", "重命名计划与当前 AgentRun 范围不一致。")
        if not document_ids:
            return _error("DOCUMENT_SCOPE_REQUIRED", "请先选择要重命名的文件。")
        if len(document_ids) > limit:
            return _error("RENAME_BATCH_TOO_LARGE", f"单次最多处理 {limit} 个工作副本。")
        self._llm_validation_calls = 0

        documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == self.user_id)
            .all()
        )
        documents_by_id = {document.id: document for document in documents}
        missing_ids = [document_id for document_id in document_ids if document_id not in documents_by_id]
        if missing_ids:
            return _error("DOCUMENT_NOT_FOUND", "部分附件不存在或不属于当前用户。")

        resolved: list[tuple[str, WorkingCopy, Document]] = []
        waiting: list[str] = []
        for source_document_id in document_ids:
            working_copy = self._resolve_working_copy(source_document=documents_by_id[source_document_id])
            if working_copy is None:
                waiting.append(source_document_id)
                continue
            working_document = self.db.get(Document, working_copy.document_id)
            if working_document is None or working_document.user_id != self.user_id:
                waiting.append(source_document_id)
                continue
            resolved.append((source_document_id, working_copy, working_document))
        if waiting:
            result = _error(
                "WORKING_COPY_NOT_READY",
                "文件仍在异步归档或导入工作副本，请稍后再生成重命名建议。",
            )
            result["status"] = "WAITING_FOR_ASYNC_JOB"
            result["pending_document_ids"] = waiting
            return result

        suggestions: list[dict[str, Any]] = []
        extraction_results: list[dict[str, Any]] = []
        for source_document_id, working_copy, working_document in resolved:
            suggestion, extraction_result = self._suggest_one(document=working_document)
            suggestion.update(
                {
                    "source_kind": "working_copy",
                    "working_copy_id": working_copy.id,
                    "source_document_id": source_document_id,
                }
            )
            suggestions.append(suggestion)
            if extraction_result is not None:
                extraction_results.append(extraction_result)

        ready = [item for item in suggestions if item.get("status") == "READY"]
        skipped = [item for item in suggestions if item.get("status") != "READY"]
        plan = None
        if ready:
            plan = WorkingCopyOperationService(self.db).create_plan(
                current_user=user,
                request=OperationPlanCreateRequest(
                    conversation_id=conversation_id,
                    operation_type="RENAME_WORKING_COPIES",
                    risk_level="medium",
                    reason="按年份、文号和正文标题重命名工作副本",
                    items=[
                        OperationPlanItem(
                            document_id=str(item["document_id"]),
                            working_copy_id=str(item["working_copy_id"]),
                            after={"filename": item["proposed_filename"]},
                            rename_metadata=_rename_metadata(item),
                        )
                        for item in ready
                    ],
                ),
            )
            plan.agent_run_id = agent_run_id
            plan.plan_json = {
                **plan.plan_json,
                "policy_key": self.policy.policy_key,
                "policy_version": self.policy.version,
                "skipped_items": skipped,
            }
            flag_modified(plan, "plan_json")
        self.db.flush()
        return {
            "ok": True,
            "kind": "rename_plan",
            "source_kind": "working_copy",
            "storage_scope": "working_copy",
            "status": "WAITING_CONFIRMATION" if plan is not None else "NEEDS_REVIEW",
            "operation_plan_id": plan.id if plan is not None else None,
            "matched_count": len(suggestions),
            "ready_count": len(ready),
            "needs_review_count": len(skipped),
            "suggestions": suggestions,
            "extraction_results": extraction_results,
            "query": {"document_ids": document_ids},
        }

    def suggest_for_initial_import(
        self,
        *,
        document: Document,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """为尚未发布的工作副本生成首次命名建议，不创建 OperationPlan。

        该入口只供系统生命周期 worker 使用。首次名称属于工作副本创建参数；文件发布为
        活动工作副本后的任何重命名仍必须走 ``generate_plan`` 和用户确认。
        """

        if document.user_id != self.user_id:
            return _error("DOCUMENT_NOT_FOUND", "文件不存在或不属于当前用户。"), None
        return self._suggest_one(document=document)

    def _resolve_working_copy(self, *, source_document: Document) -> WorkingCopy | None:
        """把上传 Document 或工作副本 Document 唯一解析为活动工作副本。"""

        direct = (
            self.db.query(WorkingCopy)
            .join(Document, Document.id == WorkingCopy.document_id)
            .filter(
                WorkingCopy.document_id == source_document.id,
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                WorkingCopy.status == "ACTIVE",
                Document.user_id == self.user_id,
            )
            .one_or_none()
        )
        if direct is not None:
            return direct
        upload_version = (
            self.db.query(DocumentVersion)
            .filter(
                DocumentVersion.document_id == source_document.id,
                DocumentVersion.storage_tier == "UPLOAD",
            )
            .order_by(DocumentVersion.version_number.desc())
            .first()
        )
        if upload_version is None:
            return None
        archive = (
            self.db.query(UploadArchiveRecord)
            .filter(
                UploadArchiveRecord.upload_document_version_id == upload_version.id,
                UploadArchiveRecord.status == "ARCHIVED",
            )
            .one_or_none()
        )
        if archive is None or not archive.managed_file_id:
            return None
        return (
            self.db.query(WorkingCopy)
            .join(Document, Document.id == WorkingCopy.document_id)
            .filter(
                WorkingCopy.managed_file_id == archive.managed_file_id,
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                WorkingCopy.status == "ACTIVE",
            )
            .one_or_none()
        )

    def _suggest_one(self, *, document: Document) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """为一个工作副本 Document 生成建议；单文件失败不会扩大到其他文件。"""

        empty_field = RenameFieldResult(status=RenameFieldStatus.MISSING)
        base = {
            "source_kind": "working_copy",
            "document_id": document.id,
            "filename": document.original_filename,
            "extension": Path(document.original_filename).suffix.lower(),
            "size_bytes": document.size_bytes,
            "document_status": document.status,
            "source_sha256": document.sha256,
            "policy_key": self.policy.policy_key,
            "policy_version": self.policy.version,
        }
        if self.db.query(ManagedFileSnapshot.id).filter(
            ManagedFileSnapshot.document_id == document.id
        ).first() is not None:
            return (
                {
                    **base,
                    "proposed_filename": None,
                    "document_date": empty_field.model_dump(mode="json"),
                    "year": empty_field.model_dump(mode="json"),
                    "document_number": empty_field.model_dump(mode="json"),
                    "title": empty_field.model_dump(mode="json"),
                    "template_key": None,
                    "status": "NEEDS_REVIEW",
                    "warnings": [],
                    "errors": [{
                        "code": "MANAGED_SNAPSHOT_IMMUTABLE",
                        "message": "该 Document 是受管原始文件快照，不能作为工作副本重命名。",
                    }],
                },
                None,
            )

        try:
            extraction_result, pages, elements = self._extract_document(document=document)
            if extraction_result.get("status") != "COMPLETED":
                error = extraction_result.get("error") or {}
                return (
                    {
                        **base,
                        "proposed_filename": None,
                        "document_date": empty_field.model_dump(mode="json"),
                        "year": empty_field.model_dump(mode="json"),
                        "document_number": empty_field.model_dump(mode="json"),
                        "title": empty_field.model_dump(mode="json"),
                        "template_key": None,
                        "status": "NEEDS_REVIEW",
                        "warnings": [],
                        "errors": [{
                            "code": str(error.get("code") or "EXTRACTION_FAILED"),
                            "message": str(error.get("message") or "文件正文解析失败。"),
                        }],
                    },
                    extraction_result,
                )
            resolved_source = FileExtractionRepository(self.db, self.user_id).resolve_original_file_for_document(document)
            readable_source = (
                self.readable_source_resolver.resolve(
                    document=document,
                    original_path=resolved_source["file_path"],
                    purpose="rename",
                )
                if resolved_source.get("ok")
                else None
            )
            metadata_resolution = self.metadata_resolution_service.resolve(
                file_path=readable_source.parse_path if readable_source is not None else None,
                filename=readable_source.parse_filename if readable_source is not None else document.original_filename,
                content_type=readable_source.parse_content_type if readable_source is not None else document.content_type,
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
                    {
                        **base,
                        "proposed_filename": None,
                        "document_date": metadata.document_date.model_dump(mode="json"),
                        "year": metadata.year.model_dump(mode="json"),
                        "document_number": metadata.document_number.model_dump(mode="json"),
                        "title": metadata.title.model_dump(mode="json"),
                        "template_key": None,
                        "status": "NEEDS_REVIEW",
                        "warnings": ["正文标题缺失或存在歧义，当前批次等待用户复核。", *warning_messages],
                        "rename_parse_mode": metadata_resolution.mode,
                        "rename_candidate_parsers": metadata_resolution.candidate_parsers,
                        "arbitration_warnings": arbitration_warnings,
                        "errors": [],
                    },
                    extraction_result,
                )
            proposed_filename, template_key = self.filename_builder.build(
                original_filename=document.original_filename,
                metadata=metadata,
                policy=self.policy,
            )
            no_change = proposed_filename == document.original_filename
            validation = self._validate_suggestion(
                original_filename=document.original_filename,
                proposed_filename=proposed_filename,
                metadata=metadata,
                arbitration_warnings=arbitration_warnings,
            )
            return (
                {
                    **base,
                    "proposed_filename": proposed_filename,
                    "document_date": metadata.document_date.model_dump(mode="json"),
                    "year": metadata.year.model_dump(mode="json"),
                    "document_number": metadata.document_number.model_dump(mode="json"),
                    "title": metadata.title.model_dump(mode="json"),
                    "template_key": template_key,
                    "status": "NO_CHANGE" if no_change else validation.status,
                    "warnings": [
                        *(["文件名已经符合当前规则，无需重命名。"] if no_change else []),
                        *warning_messages,
                        *([] if no_change else validation.warning_codes),
                    ],
                    "rename_parse_mode": metadata_resolution.mode,
                    "rename_candidate_parsers": metadata_resolution.candidate_parsers,
                    "arbitration_warnings": arbitration_warnings,
                    "rename_validation": validation.audit.model_dump(mode="json"),
                    "errors": [],
                },
                extraction_result,
            )
        except (FilenameBuildError, OSError, RuntimeError, ValueError) as exc:
            return (
                {
                    **base,
                    "proposed_filename": None,
                    "document_date": empty_field.model_dump(mode="json"),
                    "year": empty_field.model_dump(mode="json"),
                    "document_number": empty_field.model_dump(mode="json"),
                    "title": empty_field.model_dump(mode="json"),
                    "template_key": None,
                    "status": "FAILED",
                    "warnings": [],
                    "errors": [{"code": exc.__class__.__name__, "message": str(exc)}],
                },
                None,
            )

    def _validate_suggestion(
        self,
        *,
        original_filename: str,
        proposed_filename: str,
        metadata: Any,
        arbitration_warnings: list[dict[str, Any]],
    ) -> Any:
        """对工作副本复用同一质量门禁和批次模型额度。"""

        needs_llm = self.validation_service.would_call_llm(
            original_filename=original_filename,
            proposed_filename=proposed_filename,
            metadata=metadata,
            arbitration_warnings=arbitration_warnings,
        )
        max_calls = self.validation_service.settings.file_rename_llm_validation_max_items_per_batch
        allow_llm = not needs_llm or self._llm_validation_calls < max_calls
        result = self.validation_service.validate(
            original_filename=original_filename,
            proposed_filename=proposed_filename,
            metadata=metadata,
            arbitration_warnings=arbitration_warnings,
            allow_llm=allow_llm,
        )
        if needs_llm and allow_llm:
            self._llm_validation_calls += 1
        return result

    def _extract_document(
        self,
        *,
        document: Document,
    ) -> tuple[dict[str, Any], list[Any], list[Any]]:
        """生成或复用 document_pages，并只把轻量摘要返回给 Graph State。"""

        repository = FileExtractionRepository(self.db, self.user_id)
        parser_config_hash = self.readable_source_resolver.expected_parser_config_hash(
            document=document,
            purpose="rename",
        )
        reusable = repository.get_latest_successful_extraction(
            document_id=document.id,
            parser_config_hash=parser_config_hash,
        )
        reused = reusable is not None
        if reusable is None:
            resolved = repository.resolve_original_file_for_document(document)
            if not resolved.get("ok"):
                return _failed_extraction_result(
                    document=document,
                    error=resolved.get("error") or {},
                ), [], []
            try:
                readable_source = self.readable_source_resolver.resolve(
                    document=document,
                    original_path=resolved["file_path"],
                    purpose="rename",
                )
                extraction = extract_rename_primary(
                    file_path=readable_source.parse_path,
                    filename=readable_source.parse_filename,
                    content_type=readable_source.parse_content_type,
                )
                extraction = apply_readable_source_metadata(extraction, source=readable_source)
            except Exception as exc:
                run = repository.create_extraction_run(
                    document_id=document.id,
                    extractor="uploaded-file-rename",
                    parser_config_hash=parser_config_hash,
                )
                repository.fail_extraction_run(run=run, error_message=str(exc))
                return _failed_extraction_result(
                    document=document,
                    error={"code": "DOCUMENT_EXTRACTION_EXCEPTION", "message": str(exc)},
                    extraction_run_id=run.id,
                    extractor=run.extractor,
                ), [], []
            run = repository.create_extraction_run(
                document_id=document.id,
                extractor=str(extraction.get("extractor") or "uploaded-file-rename"),
                parser_name=str(extraction.get("parser_name") or ""),
                parser_version=str(extraction.get("parser_version") or ""),
                parser_config_hash=str(extraction.get("parser_config_hash") or ""),
            )
            if not extraction.get("ok"):
                error = extraction.get("error") or {}
                repository.fail_extraction_run(
                    run=run,
                    error_message=str(error.get("message") or "解析失败"),
                )
                return _failed_extraction_result(
                    document=document,
                    error=error,
                    extraction_run_id=run.id,
                    extractor=run.extractor,
                ), [], []
            repository.complete_extraction_run(
                run=run,
                pages=list(extraction.get("pages") or []),
                elements=list(extraction.get("elements") or []),
            )
            reusable = repository.get_latest_successful_extraction(
                document_id=document.id,
                parser_config_hash=run.parser_config_hash,
            )
        if reusable is None:
            return _failed_extraction_result(
                document=document,
                error={"code": "EXTRACTION_RESULT_MISSING", "message": "解析结果未能持久化。"},
            ), [], []
        run = reusable["run"]
        pages = reusable["pages"]
        elements = reusable.get("elements", [])
        return (
            {
                "ok": True,
                "document_id": document.id,
                "extraction_run_id": run.id,
                "status": "COMPLETED",
                "extractor": run.extractor,
                "parser_name": run.parser_name,
                "reused": reused,
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
                "source": "generate-rename-suggestions",
                "source_kind": "working_copy",
                "source_sha256": document.sha256,
                "structured_element_count": len(elements),
            },
            pages,
            elements,
        )


def _rename_metadata(suggestion: dict[str, Any]) -> dict[str, Any]:
    """提取可审计的重命名依据，路径仍由工作副本服务确定。"""

    return {
        "policy_key": suggestion["policy_key"],
        "policy_version": suggestion["policy_version"],
        "template_key": suggestion["template_key"],
        "document_date": suggestion["document_date"],
        "year": suggestion["year"],
        "document_number": suggestion["document_number"],
        "title": suggestion["title"],
        "parse_mode": suggestion.get("rename_parse_mode", ""),
        "candidate_parsers": suggestion.get("rename_candidate_parsers", []),
        "arbitration_warnings": suggestion.get("arbitration_warnings", []),
        "rename_validation": suggestion.get("rename_validation"),
    }


def _failed_extraction_result(
    *,
    document: Document,
    error: dict[str, Any],
    extraction_run_id: str = "",
    extractor: str = "uploaded-file-rename",
) -> dict[str, Any]:
    """构造 Graph 可聚合的工作副本解析失败结果。"""

    return {
        "ok": False,
        "document_id": document.id,
        "extraction_run_id": extraction_run_id or f"failed-uploaded-rename-{document.id}",
        "status": "FAILED",
        "extractor": extractor,
        "pages": [],
        "error": {
            "code": str(error.get("code") or "EXTRACTION_FAILED"),
            "message": str(error.get("message") or "文件解析失败。"),
        },
        "source": "generate-rename-suggestions",
        "source_kind": "working_copy",
        "source_sha256": document.sha256,
    }


def _error(code: str, message: str) -> dict[str, Any]:
    """构造工作副本重命名 Tool 的结构化错误。"""

    return {
        "ok": False,
        "kind": "rename_plan",
        "source_kind": "working_copy",
        "storage_scope": "working_copy",
        "status": "FAILED",
        "error": {"code": code, "message": message},
        "suggestions": [],
        "extraction_results": [],
    }
