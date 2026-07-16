"""上传附件临时存储重命名建议服务。

本模块只读取当前用户明确附件对应的 Document，通过受控解析结果生成 basename 建议和
OperationPlan。它不推断分类、不选择受管目录，也不在确认前修改 FileObject 或物理文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AgentRun, Document, ManagedFileSnapshot, User
from app.modules.file_rename.filename_builder import FilenameBuildError, FilenameBuilder
from app.modules.file_rename.metadata_resolution_service import RenameMetadataResolutionService
from app.modules.file_rename.parsing_service import extract_rename_primary, rename_primary_config_hash
from app.modules.file_rename.policy_loader import load_rename_policy
from app.modules.file_rename.schemas import RenameFieldResult, RenameFieldStatus
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.operations.repository import OperationPlanRepository


class UploadedRenameSuggestionService:
    """为上传附件生成只作用于临时存储的待确认重命名计划。"""

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
        document_ids: list[str],
        limit: int = 20,
    ) -> dict[str, Any]:
        """解析明确附件并创建 RENAME_UPLOADED_FILES OperationPlan。"""

        user = self.db.get(User, self.user_id)
        if user is None or not user.default_workspace_id:
            return _error("USER_WORKSPACE_REQUIRED", "当前用户缺少默认工作区，无法创建重命名计划。")
        run = self.db.get(AgentRun, agent_run_id)
        if run is None or run.user_id != self.user_id or run.conversation_id != conversation_id:
            return _error("AGENT_RUN_SCOPE_INVALID", "重命名计划与当前 AgentRun 范围不一致。")
        if not document_ids:
            return _error("DOCUMENT_SCOPE_REQUIRED", "请先选择要重命名的上传附件。")
        if len(document_ids) > limit:
            return _error("RENAME_BATCH_TOO_LARGE", f"单次最多处理 {limit} 个上传附件。")

        documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == self.user_id)
            .all()
        )
        documents_by_id = {document.id: document for document in documents}
        missing_ids = [document_id for document_id in document_ids if document_id not in documents_by_id]
        if missing_ids:
            return _error("DOCUMENT_NOT_FOUND", "部分附件不存在或不属于当前用户。")

        suggestions: list[dict[str, Any]] = []
        extraction_results: list[dict[str, Any]] = []
        for document_id in document_ids:
            suggestion, extraction_result = self._suggest_one(document=documents_by_id[document_id])
            suggestions.append(suggestion)
            if extraction_result is not None:
                extraction_results.append(extraction_result)

        ready = [item for item in suggestions if item.get("status") == "READY"]
        skipped = [item for item in suggestions if item.get("status") != "READY"]
        plan = None
        if ready:
            plan = OperationPlanRepository(self.db).create_plan(
                workspace_id=user.default_workspace_id,
                conversation_id=conversation_id,
                agent_run_id=agent_run_id,
                user_id=self.user_id,
                operation_type="RENAME_UPLOADED_FILES",
                risk_level="medium",
                reason="按年份、文号和正文标题重命名上传附件的临时文件",
                plan_json={
                    "storage_scope": "temporary",
                    "policy_key": self.policy.policy_key,
                    "policy_version": self.policy.version,
                    "items": [_operation_plan_item(item) for item in ready],
                    "skipped_items": skipped,
                },
            )
        self.db.flush()
        return {
            "ok": True,
            "kind": "rename_plan",
            "source_kind": "uploaded_document",
            "storage_scope": "temporary",
            "status": "WAITING_CONFIRMATION" if plan is not None else "NEEDS_REVIEW",
            "operation_plan_id": plan.id if plan is not None else None,
            "matched_count": len(suggestions),
            "ready_count": len(ready),
            "needs_review_count": len(skipped),
            "suggestions": suggestions,
            "extraction_results": extraction_results,
            "query": {"document_ids": document_ids},
        }

    def _suggest_one(self, *, document: Document) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """为一个上传 Document 生成建议；单文件失败不会扩大到其他附件。"""

        empty_field = RenameFieldResult(status=RenameFieldStatus.MISSING)
        base = {
            "source_kind": "uploaded_document",
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
                        "message": "该 Document 是受管文件快照，不能作为上传临时文件重命名。",
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
            metadata_resolution = self.metadata_resolution_service.resolve(
                file_path=resolved_source.get("file_path") if resolved_source.get("ok") else None,
                filename=document.original_filename,
                content_type=document.content_type,
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
                        "warnings": ["年份或正文标题存在缺失或歧义，已从可执行批次中跳过。", *warning_messages],
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
            return (
                {
                    **base,
                    "proposed_filename": proposed_filename,
                    "document_date": metadata.document_date.model_dump(mode="json"),
                    "year": metadata.year.model_dump(mode="json"),
                    "document_number": metadata.document_number.model_dump(mode="json"),
                    "title": metadata.title.model_dump(mode="json"),
                    "template_key": template_key,
                    "status": "NO_CHANGE" if no_change else "READY",
                    "warnings": [
                        *(["文件名已经符合当前规则，无需重命名。"] if no_change else []),
                        *warning_messages,
                    ],
                    "rename_parse_mode": metadata_resolution.mode,
                    "rename_candidate_parsers": metadata_resolution.candidate_parsers,
                    "arbitration_warnings": arbitration_warnings,
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

    def _extract_document(
        self,
        *,
        document: Document,
    ) -> tuple[dict[str, Any], list[Any], list[Any]]:
        """生成或复用 document_pages，并只把轻量摘要返回给 Graph State。"""

        repository = FileExtractionRepository(self.db, self.user_id)
        parser_config_hash = rename_primary_config_hash(filename=document.original_filename)
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
                extraction = extract_rename_primary(
                    file_path=resolved["file_path"],
                    filename=document.original_filename,
                    content_type=document.content_type,
                )
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
                parser_config_hash=str(extraction.get("parser_config_hash") or parser_config_hash),
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
                parser_config_hash=parser_config_hash,
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
                "source_kind": "uploaded_document",
                "source_sha256": document.sha256,
                "structured_element_count": len(elements),
            },
            pages,
            elements,
        )


def _operation_plan_item(suggestion: dict[str, Any]) -> dict[str, Any]:
    """把 READY 上传附件建议转换为不含任意路径的 OperationPlan item。"""

    return {
        "document_id": suggestion["document_id"],
        "before": {
            "source_kind": "uploaded_document",
            "filename": suggestion["filename"],
            "source_sha256": suggestion["source_sha256"],
            "size_bytes": suggestion["size_bytes"],
        },
        "after": {"filename": suggestion["proposed_filename"]},
        "rename_metadata": {
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
        },
        "execution_status": "PLANNED",
    }


def _failed_extraction_result(
    *,
    document: Document,
    error: dict[str, Any],
    extraction_run_id: str = "",
    extractor: str = "uploaded-file-rename",
) -> dict[str, Any]:
    """构造 Graph 可聚合的上传附件解析失败结果。"""

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
        "source_kind": "uploaded_document",
        "source_sha256": document.sha256,
    }


def _error(code: str, message: str) -> dict[str, Any]:
    """构造上传附件重命名 Tool 的结构化错误。"""

    return {
        "ok": False,
        "kind": "rename_plan",
        "source_kind": "uploaded_document",
        "storage_scope": "temporary",
        "status": "FAILED",
        "error": {"code": code, "message": message},
        "suggestions": [],
        "extraction_results": [],
    }
