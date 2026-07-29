"""阶段五基于活动工作副本和 EvidenceSpan 的证据回答服务。

本服务负责 RAG 闭环中的检索、证据包构造、模型生成、引用校验、缓存与持久化。它不直接
访问文件系统，也不会把完整证据包写入 AgentGraphState 或普通用户回执。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.db.models import (
    AnswerReference,
    Document,
    DocumentChunk,
    DocumentCategorySuggestion,
    DocumentIndexRun,
    DocumentSummary,
    DocumentVersion,
    EvidenceSpan,
    QAAnswer,
    TrashEntry,
    UploadArchiveRecord,
    WorkingCopy,
)
from app.modules.chunks.tokenizer import ChineseLexicalTokenizer, load_default_business_terms
from app.modules.classification.summary_service import build_extractive_document_overview
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.file_lifecycle.trash_lookup import ExactTrashFilenameLookupService
from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient
from app.modules.retrieval.chunk_lexical_search import DocumentChunkLexicalSearchService
from app.modules.retrieval.query_parser import FileSearchQueryParser
from app.modules.retrieval.readiness import WorkingCopySearchReadinessService
from app.modules.retrieval.clarification_service import FileSearchClarificationService
from app.modules.retrieval.scope_resolver import (
    ConversationFileSearchContextService,
    FileSearchScopeResolver,
)
from app.modules.retrieval.two_stage_search import TwoStageFileSearchService
from app.modules.evidence_answer.policy import EvidenceQuestionPolicy
from app.modules.evidence_answer.schemas import EvidenceItem, EvidencePackage, StructuredAnswer


_NUMERIC_PATTERN = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?%?")
_NEGATION_MARKERS = ("不需要", "无需", "不得", "禁止", "尚未", "未", "没有", "不")
_CLAIM_STOP_TERMS = {
    "根据",
    "文件",
    "文档",
    "原文",
    "显示",
    "说明",
    "其中",
    "这个",
    "这份",
    "相关",
}
_EXPLICIT_FILENAME_SUFFIXES = {
    ".csv",
    ".doc",
    ".docx",
    ".md",
    ".pdf",
    ".txt",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".tsv",
}
_FULL_SUMMARY_CACHE_KEY = "full-document-summary-contract-v2"
_FULL_SUMMARY_MODEL_QUESTION = (
    "请基于该文件全部已提供的原文证据进行完整总结，按原文顺序覆盖标题或主题、"
    "主要事项或章节、关键要求或结论，以及附件或表格要点（如原文存在）。"
    "不要逐段照抄，不要编造原文没有的背景、关系或结论。"
)


class EvidenceAnswerService:
    """执行准确性优先的证据回答，并只输出普通用户可见的紧凑投影。"""

    def __init__(
        self,
        *,
        db: Session,
        user_id: str,
        conversation_id: str,
        agent_run_id: str | None = None,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        """保存请求级依赖；数据库会话和模型客户端不得写入图状态。"""

        self.db = db
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.agent_run_id = agent_run_id
        self.settings = settings or get_settings()
        self.workspace_id = get_shared_workspace_id(db)
        self.tokenizer = ChineseLexicalTokenizer(load_default_business_terms())
        self.client = client if client is not None else self._build_client()

    def answer(
        self,
        *,
        question: str,
        document_ids: list[str] | None = None,
        answer_mode: str = "AUTO",
    ) -> dict[str, Any]:
        """从当前活动版本检索证据、生成回答并持久化可验证引用。"""

        normalized_question = str(question or "").strip()
        started_at = time.perf_counter()
        requested_ids = list(
            dict.fromkeys(
                str(item) for item in (document_ids or []) if str(item)
            )
        )
        readiness = WorkingCopySearchReadinessService(
            db=self.db,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
        )
        canonical_scope = readiness.canonicalize_document_ids(requested_ids)
        explicit_ids = list(canonical_scope.document_ids)
        parsed_question = FileSearchQueryParser(
            tokenizer=self.tokenizer
        ).parse(normalized_question)
        log_event(
            "evidence_answer.scope_resolved",
            settings=self.settings,
            agent_run_id=self.agent_run_id,
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            status="RUNNING",
            document_count=len(explicit_ids),
            message="证据回答范围解析完成",
        )
        if not normalized_question:
            return self._failure("EMPTY_QUESTION", "请输入需要查询或总结的问题。")

        trash = ExactTrashFilenameLookupService(
            db=self.db,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
        ).lookup(query=normalized_question)
        if trash is not None:
            self._log_completed(
                event="evidence_answer.degraded",
                started_at=started_at,
                status="NEEDS_CONFIRMATION",
                document_count=0,
                evidence_count=0,
                error_code="DOCUMENT_TRASHED",
            )
            return {
                "ok": True,
                "kind": "trash_restore_selection",
                "status": "NEEDS_CONFIRMATION",
                "message": trash["message"],
                "candidates": trash["candidates"],
                "answer": "",
                "references": [],
            }

        policy = EvidenceQuestionPolicy().decide(
            question=normalized_question,
            requested_mode=answer_mode,
        )
        mode = policy.answer_mode
        if policy.question_type == "UNSUPPORTED":
            return self._no_evidence(
                question=normalized_question,
                mode=mode,
                index_status="NO_EVIDENCE",
                message="当前回答只使用已同步文件中的证据，不能查询外部实时信息。",
            )
        exact_filename = _explicit_filename_from_question(normalized_question)
        active_rows = self._resolve_active_working_copies(explicit_ids)
        if exact_filename:
            # 完整文件名是最高优先级范围约束。会话上下文即使因历史附件模糊匹配
            # 传入多个 document_id，也不能把它们当作本次总结的依据。
            exact_rows = self._resolve_exact_filename_working_copies(exact_filename)
            exact_selection = self._exact_filename_selection(
                exact_rows,
                question=normalized_question,
            )
            if exact_selection is not None:
                return exact_selection
            if exact_rows:
                active_rows = exact_rows
            else:
                # 未命中完整名称时，先保留“刚上传、尚未导入”的明确状态；其余场景
                # 只能展示相似文件单选，绝不能退回已推断附件或全库正文回答。
                if canonical_scope.unresolved_document_ids or (
                    explicit_ids and len(active_rows) != len(explicit_ids)
                ):
                    deleted = self._deleted_selection(requested_ids)
                    if deleted:
                        return deleted
                    prepared = readiness.prepare_on_miss(
                        query=normalized_question,
                        parsed=parsed_question,
                        unresolved_upload_document_ids=(
                            canonical_scope.unresolved_document_ids
                        ),
                    )
                    if prepared is not None:
                        return prepared
                    return self._no_evidence(
                        question=normalized_question,
                        mode=mode,
                        index_status="NO_EVIDENCE",
                        message="当前没有可用于回答的活动文件。",
                    )
                prepared = readiness.prepare_on_miss(
                    query=normalized_question,
                    parsed=parsed_question,
                    unresolved_upload_document_ids=(
                        canonical_scope.unresolved_document_ids
                    ),
                )
                if prepared is not None:
                    return prepared
                similar_selection = self._similar_filename_selection(
                    requested_filename=exact_filename,
                    question=normalized_question,
                )
                if similar_selection is not None:
                    return similar_selection
                return self._no_evidence(
                    question=normalized_question,
                    mode=mode,
                    index_status="NO_EVIDENCE",
                    message="未找到该文件。请重新附加文件，或提供更准确的完整文件名。",
                )

        if canonical_scope.unresolved_document_ids or (
            explicit_ids and len(active_rows) != len(explicit_ids)
        ):
            deleted = self._deleted_selection(requested_ids)
            if deleted:
                return deleted
            prepared = readiness.prepare_on_miss(
                query=normalized_question,
                parsed=parsed_question,
                unresolved_upload_document_ids=(
                    canonical_scope.unresolved_document_ids
                ),
            )
            if prepared is not None:
                return prepared
            return self._no_evidence(
                question=normalized_question,
                mode=mode,
                index_status="NO_EVIDENCE",
                message="当前没有可用于回答的活动文件。",
            )

        if not active_rows:
            active_rows = self._recall_active_working_copies(normalized_question)
            active_rows = self._expand_same_name_rows(active_rows)
            if active_rows:
                # 没有完整文件名或显式附件时，召回结果只能作为候选。即使只有
                # 一份候选也必须先由用户确认；多份候选允许用户选择一份或多份。
                return self._create_file_selection(
                    rows=active_rows,
                    question=normalized_question,
                    message="找到以下相似文件，请选择一份或多份后继续原任务。",
                )
            prepared = readiness.prepare_on_miss(
                query=normalized_question,
                parsed=parsed_question,
            )
            if prepared is not None:
                return prepared
        ambiguity = self._same_name_ambiguity(
            active_rows,
            explicit_ids,
            question=normalized_question,
        )
        if ambiguity is not None:
            return ambiguity
        if not active_rows:
            return self._no_evidence(
                question=normalized_question,
                mode=mode,
                index_status="NO_EVIDENCE",
                message="没有找到可用于回答的活动文件。",
            )

        use_persisted_summary = (
            policy.question_type == "SUMMARY"
            and mode != "FULL_SUMMARY"
        )
        evidence_source = "document_chunks"
        if use_persisted_summary:
            # 普通概览先复用上传或同步阶段生成的当前版本摘要。只有摘要缺失、
            # 已过期或无法回到真实 EvidenceSpan 时，才读取完整正文证据。
            items, index_status = self._load_persisted_summary_evidence(
                working_copy_rows=active_rows,
            )
            evidence_source = "document_summary"
            if not items:
                items, index_status = self._load_evidence(
                    question=normalized_question,
                    working_copy_rows=active_rows,
                    full_summary=True,
                )
                evidence_source = "document_chunks"
        else:
            items, index_status = self._load_evidence(
                question=normalized_question,
                working_copy_rows=active_rows,
                full_summary=mode == "FULL_SUMMARY",
            )
        log_event(
            "evidence_answer.retrieval_completed",
            settings=self.settings,
            agent_run_id=self.agent_run_id,
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            status=index_status,
            document_count=len(active_rows),
            evidence_count=len(items),
            question_type=policy.question_type,
            evidence_source=evidence_source,
            message="阶段五回答证据检索完成",
        )
        if not items:
            if index_status in {"INDEX_PENDING", "PARTIAL_INDEX"}:
                prepared = readiness.prepare_on_miss(
                    query=normalized_question,
                    parsed=parsed_question,
                )
                if prepared is not None:
                    return prepared
                message = "相关文件暂时无法读取，请稍后重试。"
            elif index_status == "INDEX_FAILED":
                message = "相关文件的正文索引建立失败，请重新处理文件后再试。"
            else:
                message = "没有找到能够支持回答的原文证据。"
            self._log_completed(
                event="evidence_answer.degraded",
                started_at=started_at,
                status="NO_EVIDENCE",
                document_count=len(active_rows),
                evidence_count=0,
                error_code=index_status,
            )
            return self._no_evidence(
                question=normalized_question,
                mode=mode,
                index_status=index_status,
                message=message,
            )

        request_fingerprint = self._request_fingerprint(
            question=normalized_question,
            mode=mode,
            working_copy_rows=active_rows,
        )
        evidence_fingerprint = self._evidence_fingerprint(items)
        cached = self._read_cache(
            request_fingerprint=request_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
        )
        if cached is not None:
            self._log_completed(
                event="evidence_answer.cache_hit",
                started_at=started_at,
                status=str(cached.get("status") or "COMPLETED"),
                document_count=len(active_rows),
                evidence_count=len(items),
            )
            return cached

        log_event(
            "evidence_answer.package_built",
            settings=self.settings,
            agent_run_id=self.agent_run_id,
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            status="COMPLETED",
            document_count=len(active_rows),
            evidence_count=len(items),
            input_chars=sum(len(item.quote) for item in items),
            message="阶段五证据包构造完成",
        )
        try:
            package = EvidencePackage(
                # 全文总结使用规范化任务说明，避免“总结/完整总结/覆盖章节”三种
                # 同义表达让模型产生三种不同任务理解。用户原问题仍持久化到 QAAnswer。
                question=_model_question(normalized_question, mode=mode),
                question_type=policy.question_type,
                answer_mode=mode,
                scope={
                    "mode": "explicit_documents" if explicit_ids else "conversation_then_workspace",
                    "document_ids": [version.document_id for _, version in active_rows],
                },
                evidence_items=items,
                limitations=[],
                evidence_fingerprint=evidence_fingerprint,
            )
            structured, usage = self._generate(
                package=package,
            )
        except (LLMResponseError, ValidationError) as exc:
            # 模型或 schema 失败不能让 PostgreSQL 请求事务进入异常态，也不能返回无引用自由文本。
            log_event(
                "evidence_answer.validation_failed",
                settings=self.settings,
                level="WARNING",
                agent_run_id=self.agent_run_id,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                status="DEGRADED",
                error_code=exc.__class__.__name__,
                message="模型响应未通过阶段五结构校验，已切换确定性降级",
            )
            structured = self._deterministic_fallback(
                items=items,
                mode=mode,
                reason="模型回答校验失败，已切换为原文抽取式摘要。",
            )
            usage = {
                "llm_calls": 0,
                "input_chars": 0,
                "fallback_error": exc.__class__.__name__,
            }
        validated, validation_warnings = self._validate_claims(
            structured,
            items,
            answer_mode=mode,
        )
        if validation_warnings:
            log_event(
                "evidence_answer.validation_failed",
                settings=self.settings,
                level="WARNING",
                agent_run_id=self.agent_run_id,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                status="PARTIAL",
                rejected_claim_count=len(validation_warnings),
                message="部分模型结论未通过原文支持性校验",
            )
        if not validated:
            return self._no_evidence(
                question=normalized_question,
                mode=mode,
                index_status=index_status,
                message="模型没有生成可由当前原文证据支持的结论。",
            )

        # 模型调用结束后再次校验活动版本，防止回答期间文件被删除或替换。
        if not self._versions_are_still_active(items):
            return self._failure(
                "SOURCE_CHANGED",
                "回答生成期间文件状态发生变化，请重新查询。",
            )

        answer_text, used_ids = self._render_answer(validated)
        # limitations 同样来自模型输出，不能未经校验原样进入普通聊天或检索轨迹。
        # 这里只投影后端定义的有限状态说明。
        limitations = list(validation_warnings)
        if any("文档过长" in value for value in structured.limitations):
            limitations.append("文档过长，当前回答只覆盖了已进入模型上下文的部分证据。")
        elif structured.status == "PARTIAL" or structured.limitations:
            limitations.append("当前回答只保留了通过原文校验的部分结论。")
        limitations = list(dict.fromkeys(limitations))
        status = "PARTIAL" if limitations or index_status == "PARTIAL_INDEX" else "COMPLETED"
        record = self._persist(
            question=normalized_question,
            answer_text=answer_text,
            mode=mode,
            status=status,
            request_fingerprint=request_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            items=items,
            used_ids=used_ids,
            index_status=index_status,
            limitations=limitations,
            usage=usage,
            question_type=policy.question_type,
        )
        self._log_completed(
            event="evidence_answer.persisted",
            started_at=started_at,
            status=status,
            document_count=len(active_rows),
            evidence_count=len(items),
            qa_answer_id=record.id,
            llm_call_count=int(usage.get("llm_calls") or 0),
        )
        return self._public_payload(record=record, items=items, limitations=limitations, cached=False)

    def persist_deterministic_calculation(
        self,
        *,
        question: str,
        document_id: str,
        answer_text: str,
        calculation_result: dict[str, Any],
    ) -> str | None:
        """保存表格确定性计算结果和血缘；该路径不调用 LLM。

        计算的逐单元格依据保留在脱敏审计 JSON 中，普通聊天只展示清晰公式和文件结果。
        如果文件在执行完成前进入回收站或版本发生变化，则拒绝写入回答记录。
        """

        rows = self._resolve_active_working_copies([document_id])
        if len(rows) != 1:
            return None
        working_copy, version = rows[0]
        trace = {
            "kind": "deterministic_spreadsheet_calculation",
            "working_copy_id": working_copy.id,
            "document_version_id": version.id,
            "metric": calculation_result.get("metric") or {},
            "group_by": calculation_result.get("group_by"),
            "filters": calculation_result.get("filters") or [],
            "results": calculation_result.get("results") or [],
            "sheet_breakdown": calculation_result.get("sheet_breakdown") or [],
            "evidence_items": calculation_result.get("evidence_items") or [],
            "rows_scanned": int(calculation_result.get("rows_scanned") or 0),
            "rows_matched": int(calculation_result.get("rows_matched") or 0),
            "rows_included": int(calculation_result.get("rows_included") or 0),
            "rows_ignored": int(calculation_result.get("rows_ignored") or 0),
        }
        request_fingerprint = self._request_fingerprint(
            question=question,
            mode="TABLE_CALCULATION",
            working_copy_rows=rows,
        )
        evidence_fingerprint = hashlib.sha256(
            json.dumps(trace, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        record = QAAnswer(
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            agent_run_id=self.agent_run_id,
            question=question,
            answer_text=answer_text,
            status="COMPLETED",
            answer_mode="TABLE_CALCULATION",
            request_fingerprint=request_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            prompt_version="deterministic-spreadsheet-v1",
            schema_version=self.settings.evidence_answer_schema_version,
            provider="deterministic",
            model_name="",
            usage_json={"llm_calls": 0},
            retrieval_trace_json=trace,
        )
        self.db.add(record)
        self.db.flush()
        return record.id

    def _build_client(self) -> OpenAICompatibleLLMClient | None:
        """仅在阶段五和全局 LLM 都显式启用时构造模型客户端。"""

        if (
            not self.settings.evidence_answer_enabled
            or self.settings.evidence_answer_provider != "llm"
            or not self.settings.llm_enabled
        ):
            return None
        return OpenAICompatibleLLMClient(
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            model=self.settings.llm_chat_model,
            timeout_seconds=self.settings.llm_timeout_seconds,
        )

    def _resolve_active_working_copies(
        self, document_ids: list[str]
    ) -> list[tuple[WorkingCopy, DocumentVersion]]:
        """把上传附件或工作副本 Document ID 解析为当前活动版本。

        前端附件长期保存上传 Document ID，而后台导入会为共享工作副本创建新的
        Document。这里必须通过 UploadArchiveRecord 的受控血缘映射二者；不能仅因
        ID 不同就把已经完成导入的文件误报为仍在等待 worker。
        """

        if not document_ids:
            return []
        direct_rows = (
            self.db.query(WorkingCopy, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == WorkingCopy.current_version_id)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.document_id.in_(document_ids),
            )
            .all()
        )
        direct_by_document_id = {
            working_copy.document_id: (working_copy, version)
            for working_copy, version in direct_rows
        }
        unresolved_ids = [
            document_id
            for document_id in document_ids
            if document_id not in direct_by_document_id
        ]
        mapped_by_upload_document_id: dict[
            str,
            tuple[WorkingCopy, DocumentVersion],
        ] = {}
        if unresolved_ids:
            upload_versions = (
                self.db.query(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .filter(
                    DocumentVersion.document_id.in_(unresolved_ids),
                    DocumentVersion.storage_tier == "UPLOAD",
                    Document.user_id == self.user_id,
                )
                .order_by(
                    DocumentVersion.document_id.asc(),
                    DocumentVersion.version_number.desc(),
                )
                .all()
            )
            latest_upload_by_document: dict[str, DocumentVersion] = {}
            for version in upload_versions:
                latest_upload_by_document.setdefault(version.document_id, version)
            archives = (
                self.db.query(UploadArchiveRecord)
                .filter(
                    UploadArchiveRecord.upload_document_version_id.in_(
                        [version.id for version in latest_upload_by_document.values()]
                    ),
                    UploadArchiveRecord.status == "ARCHIVED",
                    UploadArchiveRecord.managed_file_id.is_not(None),
                )
                .all()
                if latest_upload_by_document
                else []
            )
            archive_by_version_id = {
                archive.upload_document_version_id: archive
                for archive in archives
            }
            managed_file_ids = {
                str(archive.managed_file_id)
                for archive in archives
                if archive.managed_file_id
            }
            mapped_rows = (
                self.db.query(WorkingCopy, DocumentVersion)
                .join(
                    DocumentVersion,
                    DocumentVersion.id == WorkingCopy.current_version_id,
                )
                .filter(
                    WorkingCopy.workspace_id == self.workspace_id,
                    WorkingCopy.status == "ACTIVE",
                    WorkingCopy.managed_file_id.in_(managed_file_ids),
                )
                .all()
                if managed_file_ids
                else []
            )
            row_by_managed_file_id = {
                working_copy.managed_file_id: (working_copy, version)
                for working_copy, version in mapped_rows
            }
            for document_id, upload_version in latest_upload_by_document.items():
                archive = archive_by_version_id.get(upload_version.id)
                if archive is None or not archive.managed_file_id:
                    continue
                row = row_by_managed_file_id.get(archive.managed_file_id)
                if row is not None:
                    mapped_by_upload_document_id[document_id] = row

        # 按用户传入顺序返回并去重；同一工作副本不能因上传 ID 和工作副本 ID 同时出现而重复回答。
        resolved: list[tuple[WorkingCopy, DocumentVersion]] = []
        seen_working_copy_ids: set[str] = set()
        for document_id in document_ids:
            row = (
                direct_by_document_id.get(document_id)
                or mapped_by_upload_document_id.get(document_id)
            )
            if row is None or row[0].id in seen_working_copy_ids:
                continue
            seen_working_copy_ids.add(row[0].id)
            resolved.append(row)
        return resolved

    def _recall_active_working_copies(
        self, question: str
    ) -> list[tuple[WorkingCopy, DocumentVersion]]:
        """通过当前活动 Chunk 索引召回候选文件，回收站不会参与检索。"""

        parsed = FileSearchQueryParser(tokenizer=self.tokenizer).parse(question)
        scope = FileSearchScopeResolver(
            session_file_service=ConversationFileSearchContextService(
                db=self.db,
                user_id=self.user_id,
            )
        ).resolve(
            query=question,
            explicit_attachment_ids=[],
            conversation_id=self.conversation_id,
        )
        result = TwoStageFileSearchService(
            db=self.db,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            config=self.settings,
            tokenizer=self.tokenizer,
        ).search(
            query=question,
            parsed_query=parsed,
            scope=scope,
        )
        version_ids = [
            str(item.get("document_version_id") or "")
            for item in result.get("results", [])[: self.settings.evidence_answer_max_documents]
        ]
        if not version_ids:
            return []
        rows = (
            self.db.query(WorkingCopy, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == WorkingCopy.current_version_id)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.current_version_id.in_(version_ids),
            )
            .all()
        )
        order = {value: index for index, value in enumerate(version_ids)}
        rows.sort(key=lambda item: order.get(item[1].id, len(order)))
        return rows

    def _resolve_exact_filename_working_copies(
        self, filename: str
    ) -> list[tuple[WorkingCopy, DocumentVersion]]:
        """按完整文件名锁定共享活动副本，避免正文问答退回全库召回。"""

        identity = _normalize_filename_identity(filename)
        rows = (
            self.db.query(WorkingCopy, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == WorkingCopy.current_version_id)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                func.lower(WorkingCopy.filename) == filename.lower(),
            )
            .all()
        )
        return [
            (working_copy, version)
            for working_copy, version in rows
            if _normalize_filename_identity(working_copy.filename) == identity
        ]

    def _exact_filename_selection(
        self,
        rows: list[tuple[WorkingCopy, DocumentVersion]],
        *,
        question: str,
    ) -> dict[str, Any] | None:
        """多个完整同名活动副本必须先选择，不能在用户确认前合并内容。"""

        if len(rows) <= 1:
            return None
        return self._create_file_selection(
            rows=rows,
            question=question,
            message="找到多个同名文件，请选择要读取的一份或多份文件。",
        )

    def _similar_filename_selection(
        self,
        *,
        requested_filename: str,
        question: str,
    ) -> dict[str, Any] | None:
        """精确名称未命中时仅给出有限相似候选，仍需用户确认。"""

        requested = _normalize_filename_identity(requested_filename)
        requested_stem, requested_extension = _split_filename_identity(requested)
        candidates: list[tuple[float, WorkingCopy, DocumentVersion]] = []
        rows = (
            self.db.query(WorkingCopy, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == WorkingCopy.current_version_id)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
            )
            .all()
        )
        for working_copy, version in rows:
            candidate = _normalize_filename_identity(working_copy.filename)
            candidate_stem, candidate_extension = _split_filename_identity(candidate)
            score = SequenceMatcher(None, requested_stem, candidate_stem).ratio()
            if requested_extension and requested_extension == candidate_extension:
                score += 0.08
            # 不以“文件”等泛词扩张候选；仅返回名称具备明显相似性的少量项目。
            if score >= 0.56:
                candidates.append((score, working_copy, version))
        candidates.sort(key=lambda item: (-item[0], item[1].filename.casefold(), item[1].id))
        selected = [(working_copy, version) for _, working_copy, version in candidates[:5]]
        if not selected:
            return None
        return self._create_file_selection(
            rows=selected,
            question=question,
            message="未找到完全同名的文件。请从相似文件中选择一份或多份；如果都不是，请重新附加文件。",
        )

    def _create_file_selection(
        self,
        *,
        rows: list[tuple[WorkingCopy, DocumentVersion]],
        question: str,
        message: str,
    ) -> dict[str, Any]:
        """持久化文件选择上下文，续跑时只能使用用户选择的 document_id。"""

        choices = [
            {
                "option_id": f"document-{position}",
                "document_id": working_copy.document_id,
                "document_version_id": version.id,
                "working_copy_id": working_copy.id,
                "filename": working_copy.filename,
                "size_bytes": working_copy.size_bytes,
                "created_at": working_copy.created_at.isoformat(),
            }
            for position, (working_copy, version) in enumerate(rows, start=1)
        ]
        record = FileSearchClarificationService(self.db).create(
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            agent_run_id=self.agent_run_id,
            original_query=question,
            core_phrase=choices[0]["filename"],
            relation_mode="DOCUMENT_SELECTION",
            options=[
                {
                    "id": item["option_id"],
                    "label": item["filename"],
                    "description": (
                        f"大小 {item['size_bytes']} 字节，创建于 {item['created_at']}"
                    ),
                    "document_id": item["document_id"],
                    "examples": [],
                    "estimated_count": None,
                }
                for item in choices
            ],
        )
        return {
            "ok": True,
            "kind": "file_selection",
            "status": "NEEDS_CLARIFICATION",
            "message": message,
            "clarification_id": record.id,
            "choices": choices,
            "answer": "",
            "references": [],
        }

    def _expand_same_name_rows(
        self,
        rows: list[tuple[WorkingCopy, DocumentVersion]],
    ) -> list[tuple[WorkingCopy, DocumentVersion]]:
        """补齐召回候选的同名活动副本，避免 Top-K 恰好只返回其中一个而绕过用户选择。"""

        filenames = list({working_copy.filename for working_copy, _ in rows})
        if not filenames:
            return rows
        expanded = (
            self.db.query(WorkingCopy, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == WorkingCopy.current_version_id)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.filename.in_(filenames),
            )
            .all()
        )
        by_id = {working_copy.id: (working_copy, version) for working_copy, version in rows}
        for working_copy, version in expanded:
            by_id.setdefault(working_copy.id, (working_copy, version))
        return list(by_id.values())

    def _load_persisted_summary_evidence(
        self,
        *,
        working_copy_rows: list[tuple[WorkingCopy, DocumentVersion]],
    ) -> tuple[list[EvidenceItem], str]:
        """复用当前活动版本摘要中的原文引用，不重新读取整份正文生成逐文件摘要。"""

        version_ids = [version.id for _, version in working_copy_rows]
        summary_rows = (
            self.db.query(DocumentSummary)
            .filter(
                DocumentSummary.document_version_id.in_(version_ids),
                DocumentSummary.status == "COMPLETED",
            )
            .order_by(DocumentSummary.updated_at.desc())
            .all()
        )
        latest_by_version: dict[str, DocumentSummary] = {}
        for summary in summary_rows:
            latest_by_version.setdefault(summary.document_version_id, summary)
        if len(latest_by_version) != len(version_ids):
            return [], "SUMMARY_MISSING"

        spans = (
            self.db.query(EvidenceSpan)
            .filter(EvidenceSpan.document_version_id.in_(version_ids))
            .order_by(
                EvidenceSpan.document_version_id.asc(),
                EvidenceSpan.page_number.asc(),
                EvidenceSpan.sheet_name.asc(),
                EvidenceSpan.span_index.asc(),
            )
            .all()
        )
        spans_by_version: dict[str, list[EvidenceSpan]] = defaultdict(list)
        for span in spans:
            spans_by_version[span.document_version_id].append(span)
        row_by_version = {
            version.id: (working_copy, version)
            for working_copy, version in working_copy_rows
        }

        result: list[EvidenceItem] = []
        seen_span_ids: set[str] = set()
        for version_id in version_ids:
            summary = latest_by_version[version_id]
            quotes = _document_summary_evidence_quotes(summary)
            matched_for_version = 0
            for quote in quotes:
                span = _match_summary_quote_to_span(
                    quote=quote,
                    spans=spans_by_version.get(version_id, []),
                )
                if span is None or span.id in seen_span_ids:
                    continue
                working_copy, _version = row_by_version[version_id]
                seen_span_ids.add(span.id)
                matched_for_version += 1
                result.append(
                    EvidenceItem(
                        evidence_id=span.id,
                        document_id=span.document_id,
                        document_version_id=span.document_version_id,
                        working_copy_id=working_copy.id,
                        filename=working_copy.filename,
                        quote=span.quote,
                        page_number=span.page_number,
                        sheet_name=span.sheet_name,
                        cell_range=span.cell_range,
                    )
                )
                if matched_for_version >= 8:
                    break
            if matched_for_version == 0:
                # 摘要没有任何可验证原文引用时不能直接用于回答，统一回退正文证据。
                return [], "SUMMARY_INSUFFICIENT"
        return result, "SUMMARY_READY"

    def _load_evidence(
        self,
        *,
        question: str,
        working_copy_rows: list[tuple[WorkingCopy, DocumentVersion]],
        full_summary: bool,
    ) -> tuple[list[EvidenceItem], str]:
        """加载全量摘要证据或问题相关证据，并返回明确索引状态。"""

        version_ids = [version.id for _, version in working_copy_rows]
        index_rows = (
            self.db.query(DocumentIndexRun)
            .filter(DocumentIndexRun.document_version_id.in_(version_ids))
            .order_by(DocumentIndexRun.updated_at.desc())
            .all()
        )
        latest: dict[str, DocumentIndexRun] = {}
        for row in index_rows:
            latest.setdefault(row.document_version_id, row)
        completed = {
            version_id
            for version_id, row in latest.items()
            if row.status == "COMPLETED"
            and row.evidence_count > 0
            and row.index_version == "document-chunk-index-v2"
        }
        if not completed:
            if latest and all(row.status == "FAILED" for row in latest.values()):
                return [], "INDEX_FAILED"
            return [], "INDEX_PENDING" if latest else "NO_EVIDENCE"
        index_status = "PARTIAL_INDEX" if len(completed) != len(version_ids) else "INDEX_READY"

        if full_summary:
            spans = (
                self.db.query(EvidenceSpan)
                .join(
                    WorkingCopy,
                    (WorkingCopy.document_id == EvidenceSpan.document_id)
                    & (WorkingCopy.current_version_id == EvidenceSpan.document_version_id),
                )
                .filter(
                    EvidenceSpan.document_version_id.in_(completed),
                    WorkingCopy.workspace_id == self.workspace_id,
                    WorkingCopy.status == "ACTIVE",
                    WorkingCopy.current_version_id == EvidenceSpan.document_version_id,
                )
                .order_by(
                    EvidenceSpan.document_version_id.asc(),
                    EvidenceSpan.page_number.asc(),
                    EvidenceSpan.sheet_name.asc(),
                    EvidenceSpan.span_index.asc(),
                )
                .all()
            )
        else:
            search = DocumentChunkLexicalSearchService(
                db=self.db,
                user_id=self.user_id,
                workspace_id=self.workspace_id,
                tokenizer=self.tokenizer,
            ).search(
                query=FileSearchQueryParser(tokenizer=self.tokenizer).parse(question).cleaned,
                document_version_ids=list(completed),
                limit=self.settings.evidence_answer_max_items,
            )
            chunk_ids = [str(item.get("chunk_id") or "") for item in search]
            spans = (
                self.db.query(EvidenceSpan)
                .filter(EvidenceSpan.chunk_id.in_(chunk_ids))
                .order_by(EvidenceSpan.chunk_id.asc(), EvidenceSpan.span_index.asc())
                .all()
                if chunk_ids
                else []
            )
        row_by_version = {version.id: (working_copy, version) for working_copy, version in working_copy_rows}
        result: list[EvidenceItem] = []
        for span in spans:
            pair = row_by_version.get(span.document_version_id)
            if pair is None:
                continue
            working_copy, _ = pair
            result.append(
                EvidenceItem(
                    evidence_id=span.id,
                    document_id=span.document_id,
                    document_version_id=span.document_version_id,
                    working_copy_id=working_copy.id,
                    filename=working_copy.filename,
                    quote=span.quote,
                    page_number=span.page_number,
                    sheet_name=span.sheet_name,
                    cell_range=span.cell_range,
                )
            )
        return result, index_status

    def _generate(
        self,
        *,
        package: EvidencePackage,
    ) -> tuple[StructuredAnswer, dict[str, Any]]:
        """调用模型生成结构化结论；模型不可用时返回可验证的抽取式降级。"""

        items = package.evidence_items
        if self.client is None:
            return (
                self._deterministic_fallback(
                    items=items,
                    mode=package.answer_mode,
                    reason="当前未启用证据回答 LLM，已使用原文抽取式摘要。",
                ),
                {"llm_calls": 0, "input_chars": 0},
            )

        batches = self._evidence_batches(items)
        responses: list[StructuredAnswer] = []
        calls = 0
        input_chars = 0
        repair_calls = 0
        for batch in batches[: self.settings.evidence_answer_max_calls]:
            if calls >= self.settings.evidence_answer_max_calls:
                break
            payload = self._model_payload(package=package, items=batch)
            input_chars += sum(len(item.quote) for item in batch)
            call_started_at = time.perf_counter()
            log_event(
                "evidence_answer.llm_started",
                settings=self.settings,
                agent_run_id=self.agent_run_id,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                status="RUNNING",
                call_index=calls + 1,
                evidence_count=len(batch),
                message="阶段五模型生成开始",
            )
            raw = self.client.complete_json(
                system_prompt=self._system_prompt(answer_mode=package.answer_mode),
                user_payload=payload,
            )
            calls += 1
            log_event(
                "evidence_answer.llm_completed",
                settings=self.settings,
                agent_run_id=self.agent_run_id,
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                status="COMPLETED",
                call_index=calls,
                evidence_count=len(batch),
                duration_ms=int((time.perf_counter() - call_started_at) * 1000),
                message="阶段五模型生成完成",
            )
            parsed, repaired = self._parse_or_repair(
                raw=raw,
                payload=payload,
                repair_budget=min(
                    self.settings.evidence_answer_repair_calls - repair_calls,
                    self.settings.evidence_answer_max_calls - calls,
                ),
            )
            calls += repaired
            repair_calls += repaired
            responses.append(parsed)
        if len(responses) == 1 and len(batches) == 1:
            return responses[0], {
                "llm_calls": calls,
                "repair_calls": repair_calls,
                "input_chars": input_chars,
            }

        merged_claims = [claim.model_dump() for response in responses for claim in response.claims]
        limitations = [value for response in responses for value in response.limitations]
        if len(batches) > len(responses):
            limitations.append("文档过长，当前回答只覆盖了已进入模型上下文的部分证据。")
        return (
            StructuredAnswer(
                claims=merged_claims,
                limitations=list(dict.fromkeys(limitations)),
                status="PARTIAL" if limitations else "COMPLETED",
            ),
            {
                "llm_calls": calls,
                "repair_calls": repair_calls,
                "input_chars": input_chars,
                "batch_count": len(responses),
            },
        )

    def _parse_or_repair(
        self, *, raw: dict[str, Any], payload: dict[str, Any], repair_budget: int
    ) -> tuple[StructuredAnswer, int]:
        """校验模型 schema，允许一次受控修复，不接受自由文本兜底。"""

        try:
            return StructuredAnswer.model_validate(raw), 0
        except ValidationError as exc:
            if (
                repair_budget < 1
                or self.client is None
            ):
                raise LLMResponseError(f"证据回答结构校验失败：{exc}") from exc
            repaired = self.client.complete_json(
                system_prompt=(
                    self._system_prompt(answer_mode=str(payload.get("answer_mode") or "FOCUSED"))
                    + "\n上一响应未通过 schema 校验。只返回合法 JSON，不得添加新事实。"
                ),
                user_payload={**payload, "validation_error": str(exc)[:1000], "invalid_response": raw},
            )
            return StructuredAnswer.model_validate(repaired), 1

    def _evidence_batches(self, items: list[EvidenceItem]) -> list[list[EvidenceItem]]:
        """按字符上限分批，保证不会在证据中间截断文本。"""

        max_chars = self.settings.evidence_answer_max_input_chars
        batches: list[list[EvidenceItem]] = []
        current: list[EvidenceItem] = []
        current_chars = 0
        for item in items:
            size = len(item.quote)
            if current and current_chars + size > max_chars:
                batches.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += size
        if current:
            batches.append(current)
        return batches or [[]]

    @staticmethod
    def _model_payload(
        *, package: EvidencePackage, items: Iterable[EvidenceItem]
    ) -> dict[str, Any]:
        """构造只含问题与已授权证据的模型输入。"""

        return {
            "question": package.question,
            "question_type": package.question_type,
            "answer_mode": package.answer_mode,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "filename": item.filename,
                    "quote": item.quote,
                    "page_number": item.page_number,
                    "sheet_name": item.sheet_name,
                    "cell_range": item.cell_range,
                }
                for item in items
            ],
            "required_output": {
                "claims": [{"text": "结论", "evidence_ids": ["evidence-id"]}],
                "limitations": [],
                "status": "COMPLETED",
            },
        }

    @staticmethod
    def _system_prompt(*, answer_mode: str) -> str:
        """返回准确性优先的阶段五提示词。"""

        base = (
            "你是文件证据回答器。只能使用 evidence 中的事实，不得使用常识补全。"
            "每条 claim 必须引用一个或多个真实 evidence_id。数字、日期、姓名必须逐字存在于引用证据；"
            "证据不足时放入 limitations 或返回 NO_EVIDENCE。只输出符合 required_output 的 JSON 对象。"
        )
        if answer_mode != "FULL_SUMMARY":
            return base
        return (
            base
            + "当前任务是完整文档总结：请按原文逻辑顺序输出 3 至 8 条凝练要点，"
            "覆盖可见的主要章节或事项；不要逐句摘录、不要添加 [1] 这类引用编号。"
            "每条要点仍必须给出支持它的 evidence_id。"
        )

    def _validate_claims(
        self,
        answer: StructuredAnswer,
        items: list[EvidenceItem],
        *,
        answer_mode: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """验证引用存在且关键数字受到引用文本支持。"""

        item_map = {item.evidence_id: item for item in items}
        valid: list[dict[str, Any]] = []
        warnings: list[str] = []
        for claim in answer.claims:
            evidence_ids = list(dict.fromkeys(claim.evidence_ids))
            cited = [item_map[value] for value in evidence_ids if value in item_map]
            if not cited:
                # warnings.append("已移除一条没有有效引用的模型结论。")
                continue
            evidence_text = "\n".join(item.quote for item in cited)
            claim_numbers = {_normalize_number(value) for value in _NUMERIC_PATTERN.findall(claim.text)}
            evidence_numbers = {_normalize_number(value) for value in _NUMERIC_PATTERN.findall(evidence_text)}
            if claim_numbers - evidence_numbers:
                # warnings.append("已移除一条数字无法由引用原文支持的模型结论。")
                continue
            claim_terms = {
                value
                for value in self.tokenizer.tokenize(claim.text)
                if len(value) >= 2
                and not value.isdigit()
                and value not in _CLAIM_STOP_TERMS
            }
            evidence_terms = {
                value
                for value in self.tokenizer.tokenize(evidence_text)
                if len(value) >= 2 and not value.isdigit()
            }
            overlap_ratio = (
                len(claim_terms.intersection(evidence_terms)) / len(claim_terms)
                if claim_terms
                else 1.0
            )
            # 全文总结允许在不改变数字、否定关系和引用边界的前提下压缩表述；
            # 仍要求至少一半关键事实词可回到原文，避免把自然语言概括误判为幻觉。
            if answer_mode == "FULL_SUMMARY":
                required_overlap = 0.6 if len(claim_terms) <= 6 else 0.5
            else:
                required_overlap = 0.8 if len(claim_terms) <= 6 else 0.65
            if overlap_ratio < required_overlap:
                # warnings.append("已移除一条与引用原文缺少事实词项重合的模型结论。")
                continue
            claim_negations = {
                marker for marker in _NEGATION_MARKERS if marker in claim.text
            }
            evidence_negations = {
                marker for marker in _NEGATION_MARKERS if marker in evidence_text
            }
            if bool(claim_negations) != bool(evidence_negations):
                # warnings.append("已移除一条肯定或否定关系与引用原文不一致的模型结论。")
                continue
            valid.append({"text": claim.text.strip(), "evidence_ids": [item.evidence_id for item in cited]})
        return valid, warnings

    @staticmethod
    def _deterministic_fallback(
        *,
        items: list[EvidenceItem],
        mode: str,
        reason: str,
    ) -> StructuredAnswer:
        """在模型不可用时仍为全文总结返回稳定、可审计的抽取式概览。"""

        usable = [item for item in items if item.quote.strip()]
        if mode == "FULL_SUMMARY":
            overview = build_extractive_document_overview(
                filename=usable[0].filename if usable else "文件",
                full_text="\n".join(item.quote for item in usable),
            )
            if overview:
                # 概览仅由当前证据原句抽取而来，因此保留全部参与证据的审计边界。
                return StructuredAnswer(
                    claims=[
                        {
                            "text": overview,
                            "evidence_ids": [item.evidence_id for item in usable],
                        }
                    ],
                    limitations=[reason],
                    status="PARTIAL",
                )
        return StructuredAnswer(
            claims=[
                {"text": item.quote.strip(), "evidence_ids": [item.evidence_id]}
                for item in usable[: min(8, len(usable))]
            ],
            limitations=[reason],
            status="PARTIAL",
        )

    @staticmethod
    def _render_answer(claims: list[dict[str, Any]]) -> tuple[str, list[str]]:
        """保存稳定引用顺序，但不把内部编号拼入普通用户回答。"""

        ordered_ids: list[str] = []
        parts: list[str] = []
        for claim in claims:
            for evidence_id in claim["evidence_ids"]:
                if evidence_id not in ordered_ids:
                    ordered_ids.append(evidence_id)
            # AnswerReference 持久化审计引用；界面仅展示自然语言结论和文件卡。
            parts.append(str(claim["text"]))
        return "\n\n".join(parts), ordered_ids

    def _persist(
        self,
        *,
        question: str,
        answer_text: str,
        mode: str,
        status: str,
        request_fingerprint: str,
        evidence_fingerprint: str,
        items: list[EvidenceItem],
        used_ids: list[str],
        index_status: str,
        limitations: list[str],
        usage: dict[str, Any],
        question_type: str,
    ) -> QAAnswer:
        """保存回答与稳定 EvidenceSpan 引用，不保存普通 UI 不需要的重复正文。"""

        record = QAAnswer(
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            agent_run_id=self.agent_run_id,
            question=question,
            answer_text=answer_text,
            status=status,
            answer_mode=mode,
            request_fingerprint=request_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            prompt_version=self.settings.evidence_answer_prompt_version,
            schema_version=self.settings.evidence_answer_schema_version,
            provider=self.settings.evidence_answer_provider,
            model_name=self.settings.llm_chat_model,
            usage_json=usage,
            retrieval_trace_json={
                "index_status": index_status,
                "evidence_count": len(items),
                "limitations": limitations,
                "question_type": question_type,
            },
        )
        self.db.add(record)
        self.db.flush()
        item_map = {item.evidence_id: item for item in items}
        for reference_index, evidence_id in enumerate(used_ids, start=1):
            item = item_map[evidence_id]
            self.db.add(
                AnswerReference(
                    qa_answer_id=record.id,
                    evidence_span_id=item.evidence_id,
                    document_id=item.document_id,
                    document_version_id=item.document_version_id,
                    working_copy_id=item.working_copy_id,
                    reference_index=reference_index,
                    label=item.filename,
                )
            )
        self.db.flush()
        return record

    def _read_cache(
        self, *, request_fingerprint: str, evidence_fingerprint: str
    ) -> dict[str, Any] | None:
        """只复用证据指纹完全相同且引用仍属于活动当前版本的回答。"""

        if not self.settings.evidence_answer_cache_enabled:
            return None
        record = (
            self.db.query(QAAnswer)
            .filter(
                QAAnswer.user_id == self.user_id,
                QAAnswer.conversation_id == self.conversation_id,
                QAAnswer.request_fingerprint == request_fingerprint,
                QAAnswer.evidence_fingerprint == evidence_fingerprint,
                QAAnswer.prompt_version == self.settings.evidence_answer_prompt_version,
                QAAnswer.schema_version == self.settings.evidence_answer_schema_version,
                QAAnswer.status.in_(["COMPLETED", "PARTIAL"]),
            )
            .order_by(QAAnswer.created_at.desc())
            .first()
        )
        if record is None:
            return None
        refs = (
            self.db.query(AnswerReference, WorkingCopy, EvidenceSpan)
            .join(WorkingCopy, WorkingCopy.id == AnswerReference.working_copy_id)
            .join(EvidenceSpan, EvidenceSpan.id == AnswerReference.evidence_span_id)
            .filter(
                AnswerReference.qa_answer_id == record.id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.current_version_id == AnswerReference.document_version_id,
            )
            .order_by(AnswerReference.reference_index.asc())
            .all()
        )
        expected = self.db.query(AnswerReference).filter(AnswerReference.qa_answer_id == record.id).count()
        if not refs or len(refs) != expected:
            return None
        items = [
            EvidenceItem(
                evidence_id=span.id,
                document_id=reference.document_id,
                document_version_id=reference.document_version_id,
                working_copy_id=working_copy.id,
                filename=working_copy.filename,
                quote=span.quote,
                page_number=span.page_number,
                sheet_name=span.sheet_name,
                cell_range=span.cell_range,
            )
            for reference, working_copy, span in refs
        ]
        limitations = list((record.retrieval_trace_json or {}).get("limitations") or [])
        return self._public_payload(record=record, items=items, limitations=limitations, cached=True)

    def _public_payload(
        self,
        *,
        record: QAAnswer,
        items: list[EvidenceItem],
        limitations: list[str],
        cached: bool,
    ) -> dict[str, Any]:
        """生成普通回执投影，不返回 quote、页码、单元格或内部检索轨迹。"""

        cards: dict[str, dict[str, Any]] = {}
        document_ids = list(dict.fromkeys(item.document_id for item in items))
        category_rows = (
            self.db.query(DocumentCategorySuggestion)
            .filter(DocumentCategorySuggestion.document_id.in_(document_ids))
            .order_by(
                DocumentCategorySuggestion.document_id.asc(),
                DocumentCategorySuggestion.created_at.desc(),
                DocumentCategorySuggestion.rank.asc(),
            )
            .all()
        )
        category_labels: dict[str, list[str]] = defaultdict(list)
        for suggestion in category_rows:
            path = [
                str(value) for value in (suggestion.category_path_json or []) if str(value)
            ]
            label = " / ".join(path) or str(suggestion.category_name or "")
            if (
                label
                and label not in category_labels[suggestion.document_id]
                and len(category_labels[suggestion.document_id]) < 3
            ):
                category_labels[suggestion.document_id].append(label)
        reference_indexes_by_document: dict[str, list[int]] = defaultdict(list)
        for reference in (
            self.db.query(AnswerReference)
            .filter(AnswerReference.qa_answer_id == record.id)
            .order_by(AnswerReference.reference_index.asc())
            .all()
        ):
            reference_indexes_by_document[reference.document_id].append(
                reference.reference_index
            )
        for item in items:
            cards.setdefault(
                item.document_id,
                {
                    "document_id": item.document_id,
                    "document_version_id": item.document_version_id,
                    "working_copy_id": item.working_copy_id,
                    "filename": item.filename,
                    "category_labels": category_labels.get(item.document_id, []),
                    "availability": "AVAILABLE",
                    "availability_message": "文件可用",
                    "can_open": True,
                    "can_restore": False,
                    "reference_indexes": reference_indexes_by_document.get(
                        item.document_id, []
                    ),
                },
            )
        return {
            "ok": True,
            "kind": "evidence_answer",
            "status": record.status,
            "answer_id": record.id,
            # 兼容旧 QAAnswer 已保存的 [1] 引用后缀；引用事实仍由 AnswerReference 保留。
            "answer": _strip_legacy_inline_reference_indexes(
                record.answer_text,
                reference_indexes={
                    index
                    for indexes in reference_indexes_by_document.values()
                    for index in indexes
                },
            ),
            "limitations": limitations,
            "references": list(cards.values()),
            "cached": cached,
        }

    def _request_fingerprint(
        self,
        *,
        question: str,
        mode: str,
        working_copy_rows: list[tuple[WorkingCopy, DocumentVersion]],
    ) -> str:
        """计算包含问题、回答模式和当前内容版本的请求指纹。"""

        payload = {
            # 等价的全文总结用同一个指纹，确保先生成的经校验结果能被后续同义问法复用。
            "question": (
                _FULL_SUMMARY_CACHE_KEY
                if mode == "FULL_SUMMARY"
                else " ".join(question.split()).casefold()
            ),
            "mode": mode,
            "versions": sorted(
                (working_copy.id, version.id, version.sha256)
                for working_copy, version in working_copy_rows
            ),
            "prompt": self.settings.evidence_answer_prompt_version,
            "schema": self.settings.evidence_answer_schema_version,
            "model": self.settings.llm_chat_model,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _evidence_fingerprint(items: list[EvidenceItem]) -> str:
        """根据证据 ID、版本和正文摘要生成可失效指纹。"""

        payload = [
            (item.evidence_id, item.document_version_id, hashlib.sha256(item.quote.encode("utf-8")).hexdigest())
            for item in items
        ]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _versions_are_still_active(self, items: list[EvidenceItem]) -> bool:
        """在写入回答前重新检查所有引用的工作副本状态和当前版本。"""

        expected = {(item.working_copy_id, item.document_version_id) for item in items}
        rows = (
            self.db.query(WorkingCopy.id, WorkingCopy.current_version_id)
            .filter(
                WorkingCopy.id.in_([value[0] for value in expected]),
                WorkingCopy.status == "ACTIVE",
            )
            .all()
        )
        return {(row.id, row.current_version_id) for row in rows} == expected

    def _deleted_selection(self, document_ids: list[str]) -> dict[str, Any] | None:
        """把明确指向回收站文件的附件或上下文转换为恢复提示。"""

        rows = (
            self.db.query(TrashEntry, WorkingCopy, DocumentVersion)
            .join(WorkingCopy, WorkingCopy.id == TrashEntry.working_copy_id)
            .join(DocumentVersion, DocumentVersion.id == TrashEntry.document_version_id)
            .filter(
                TrashEntry.workspace_id == self.workspace_id,
                TrashEntry.status == "ACTIVE",
                WorkingCopy.status == "TRASHED",
                WorkingCopy.document_id.in_(document_ids),
            )
            .order_by(TrashEntry.deleted_at.desc(), TrashEntry.id.asc())
            .all()
        )
        if not rows:
            return None
        return {
            "ok": True,
            "kind": "trash_restore_selection",
            "status": "NEEDS_CONFIRMATION",
            "message": "所选文件已删除。请选择需要恢复的文件后再读取正文。",
            "candidates": [
                {
                    "trash_entry_id": entry.id,
                    "filename": working_copy.filename,
                    "size_bytes": working_copy.size_bytes,
                    "version_number": version.version_number,
                    "deleted_at": entry.deleted_at.isoformat(),
                    "created_at": working_copy.created_at.isoformat(),
                }
                for entry, working_copy, version in rows
            ],
            "answer": "",
            "references": [],
        }

    def _same_name_ambiguity(
        self,
        rows: list[tuple[WorkingCopy, DocumentVersion]],
        explicit_ids: list[str],
        *,
        question: str,
    ) -> dict[str, Any] | None:
        """同名不同内容且用户未明确选择文件时停止回答。"""

        if explicit_ids:
            return None
        groups: dict[str, list[tuple[WorkingCopy, DocumentVersion]]] = defaultdict(list)
        for working_copy, version in rows:
            groups[working_copy.filename.casefold()].append((working_copy, version))
        ambiguous = [
            group
            for group in groups.values()
            if len(group) > 1 and len({version.sha256 for _, version in group}) > 1
        ]
        if not ambiguous:
            return None
        return self._create_file_selection(
            rows=[item for group in ambiguous for item in group],
            question=question,
            message="找到多个同名但内容不同的文件，请选择一份或多份后继续。",
        )

    def _no_evidence(
        self, *, question: str, mode: str, index_status: str, message: str
    ) -> dict[str, Any]:
        """返回明确无依据状态，不调用模型、不伪造引用。"""

        return {
            "ok": True,
            "kind": "evidence_answer",
            "status": "NO_EVIDENCE",
            "answer": message,
            "answer_id": None,
            "limitations": [],
            "references": [],
            "index_status": index_status,
            "question": question,
            "answer_mode": mode,
        }

    @staticmethod
    def _failure(code: str, message: str) -> dict[str, Any]:
        """返回可审计的阶段五业务失败。"""

        return {
            "ok": False,
            "kind": "evidence_answer",
            "status": "FAILED",
            "answer": message,
            "references": [],
            "error": {"code": code, "message": message},
        }

    def _log_completed(
        self,
        *,
        event: str,
        started_at: float,
        status: str,
        document_count: int,
        evidence_count: int,
        error_code: str | None = None,
        qa_answer_id: str | None = None,
        llm_call_count: int = 0,
    ) -> None:
        """记录不含问题、证据正文和回答正文的阶段五诊断事件。"""

        log_event(
            event,
            settings=self.settings,
            level="WARNING" if status in {"FAILED", "PARTIAL", "NO_EVIDENCE"} else "INFO",
            agent_run_id=self.agent_run_id,
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            qa_answer_id=qa_answer_id,
            status=status,
            document_count=document_count,
            evidence_count=evidence_count,
            llm_call_count=llm_call_count,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            error_code=error_code,
            message="证据回答处理完成",
        )


def _normalize_number(value: str) -> str:
    """统一数字中的千分位，便于验证模型没有生成证据外数值。"""

    return str(value).replace(",", "")


def _document_summary_evidence_quotes(summary: DocumentSummary) -> list[str]:
    """从持久化摘要契约中提取原文引用，摘要结论本身不能伪装成原文证据。"""

    payload = summary.summary_json if isinstance(summary.summary_json, dict) else {}
    quotes: list[str] = []
    for key_point in payload.get("key_points", []):
        if not isinstance(key_point, dict):
            continue
        for reference in key_point.get("evidence_refs", []):
            if not isinstance(reference, dict):
                continue
            quote = str(reference.get("quote") or "").strip()
            if quote and quote not in quotes:
                quotes.append(quote)
    return quotes


def _match_summary_quote_to_span(
    *,
    quote: str,
    spans: list[EvidenceSpan],
) -> EvidenceSpan | None:
    """把摘要保存的逐字引用映射回当前版本 EvidenceSpan。"""

    normalized_quote = re.sub(r"\s+", "", quote)
    if len(normalized_quote) < 4:
        return None
    for span in spans:
        normalized_span = re.sub(r"\s+", "", str(span.quote or ""))
        if normalized_quote in normalized_span or normalized_span in normalized_quote:
            return span
    return None


def _explicit_filename_from_question(question: str) -> str | None:
    """从自然语言中提取完整文件名，作为单文件读取的后端范围约束。"""

    normalized = unicodedata.normalize("NFKC", str(question or ""))
    suffix_pattern = "|".join(
        re.escape(value.lstrip("."))
        for value in sorted(_EXPLICIT_FILENAME_SUFFIXES, key=len, reverse=True)
    )
    match = re.search(
        rf"(?P<filename>[^/\\，。！？\r\n]+?\.(?:{suffix_pattern}))",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    candidate = match.group("filename").strip().strip("“”\"'《》【】")
    candidate = re.sub(
        r"^(?:请|麻烦)?(?:帮我)?(?:对|把|将)?\s*"
        r"(?:(?:完整|全面|详细|全文)?(?:总结|概括|讲解|说明|读取|解析)(?:一下)?)\s*",
        "",
        candidate,
    ).strip()
    if not candidate or "/" in candidate or "\\" in candidate:
        return None
    suffix = next(
        (value for value in _EXPLICIT_FILENAME_SUFFIXES if candidate.casefold().endswith(value)),
        None,
    )
    return candidate if suffix else None


def _normalize_filename_identity(value: str) -> str:
    """统一文件名身份的 Unicode 与大小写，保留业务字符以避免误合并。"""

    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _split_filename_identity(value: str) -> tuple[str, str]:
    """拆分规范化名称的主体和扩展名，仅用于近似候选排序。"""

    stem, separator, extension = value.rpartition(".")
    if not separator:
        return value, ""
    return stem, f".{extension}"


def _model_question(question: str, *, mode: str) -> str:
    """将全文总结归一为稳定任务契约，避免同义措辞改变模型行为。"""

    return _FULL_SUMMARY_MODEL_QUESTION if mode == "FULL_SUMMARY" else question


def _strip_legacy_inline_reference_indexes(
    value: str,
    *,
    reference_indexes: set[int],
) -> str:
    """移除历史回答中的引用编号，避免旧缓存继续污染普通聊天正文。"""

    def replace(match: re.Match[str]) -> str:
        return "" if int(match.group(1)) in reference_indexes else match.group(0)

    # 只移除确实对应 AnswerReference 的历史编号，避免误删法规年份等普通正文内容。
    return re.sub(r"\[(\d+)\]", replace, str(value or ""))
