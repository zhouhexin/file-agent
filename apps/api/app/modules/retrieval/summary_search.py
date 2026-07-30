"""基于持久化摘要、最终文件名和分类建议召回工作副本。

本模块只负责文档级候选召回，不使用摘要直接回答事实问题。需要精确事实时，
后续 evidence-answer 必须继续在候选文档的原文页面或表格单元格中取证。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from app.core.logging import log_event
from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentSummary,
    WorkingCopy,
)
from app.modules.retrieval.query_parser import normalize_file_search_query


@dataclass(frozen=True)
class _SearchCandidate:
    """工作副本及其当前版本摘要的内部检索投影。"""

    working_copy: WorkingCopy
    document: Document
    summary: DocumentSummary


class WorkingCopySummarySearchService:
    """在授权工作区内执行轻量、确定性的摘要优先检索。"""

    def __init__(
        self,
        *,
        db: Any,
        user_id: str,
        workspace_id: str | None = None,
        max_candidates: int = 500,
    ) -> None:
        """绑定检索权限边界。

        正式对话检索传入共享 ``workspace_id``，允许所有用户读取唯一共享工作目录；
        未传工作区仅用于旧调用兼容，此时继续按 ``Document.user_id`` 隔离。
        """

        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.max_candidates = max(1, max_candidates)

    def search(
        self,
        *,
        query: str,
        document_ids: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """返回按相关度排序的工作副本，不暴露原始文件名和内部处理状态。"""

        # 摘要检索既是显式关闭两阶段检索时的低成本路径，也是数据库索引异常时的
        # 安全降级路径，必须与主链路使用同一个核心查询，不能重新从原句切出语法噪声。
        started_at = time.perf_counter()
        cleaned_query = normalize_file_search_query(query)
        normalized_query = _normalize_text(cleaned_query)
        terms = _query_terms(cleaned_query)
        if not normalized_query or not terms:
            log_event(
                "retrieval.summary_search.completed",
                level="WARNING",
                tool_name="hybrid-search",
                status="SKIPPED",
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                workspace_id=self.workspace_id,
                cleaned_query_chars=len(cleaned_query),
                query_term_count=len(terms),
                candidate_count=0,
                result_count=0,
                error_code="EMPTY_SUMMARY_QUERY",
                message="摘要级检索查询清洗后为空",
            )
            return {"kind": "workspace_file_search", "ok": True, "query": query, "results": []}

        candidates = self._load_candidates(document_ids=document_ids or [])
        suggestions = self._load_suggestions(candidates)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for candidate in candidates:
            suggestion = suggestions.get(candidate.document.id)
            scored = _score_candidate(
                candidate=candidate,
                suggestion=suggestion,
                normalized_query=normalized_query,
                terms=terms,
            )
            if scored is not None:
                ranked.append(scored)

        ranked.sort(key=lambda item: (-item[0], item[1]["filename"], item[1]["document_id"]))
        result_rows = [
            item for _, item in ranked[: max(1, min(limit, 50))]
        ]
        log_event(
            "retrieval.summary_search.completed",
            level="WARNING" if not result_rows else "INFO",
            tool_name="hybrid-search",
            status="COMPLETED",
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            workspace_id=self.workspace_id,
            cleaned_query_chars=len(cleaned_query),
            query_term_count=len(terms),
            scoped_document_count=len(document_ids or []),
            candidate_count=len(candidates),
            result_count=len(result_rows),
            message="摘要级工作副本检索完成",
        )
        return {
            "kind": "workspace_file_search",
            "ok": True,
            "query": query,
            "results": result_rows,
        }

    def _load_candidates(self, *, document_ids: list[str]) -> list[_SearchCandidate]:
        """只读取授权范围内 ACTIVE 工作副本的当前版本摘要。"""

        query = (
            self.db.query(WorkingCopy, Document, DocumentSummary)
            .join(Document, Document.id == WorkingCopy.document_id)
            .join(
                DocumentSummary,
                (DocumentSummary.document_id == WorkingCopy.document_id)
                & (DocumentSummary.document_version_id == WorkingCopy.current_version_id),
            )
            .filter(
                WorkingCopy.status == "ACTIVE",
                DocumentSummary.status == "COMPLETED",
            )
        )
        if self.workspace_id:
            # 共享工作目录以 workspace 作为读取权限边界，Document.user_id 仅保留导入审计含义。
            query = query.filter(WorkingCopy.workspace_id == self.workspace_id)
        else:
            # 保留旧服务直接调用的用户隔离语义，避免无工作区上下文时扩大权限。
            query = query.filter(Document.user_id == self.user_id)
        if document_ids:
            query = query.filter(Document.id.in_(document_ids))
        rows = query.order_by(DocumentSummary.updated_at.desc()).limit(self.max_candidates).all()
        return [
            _SearchCandidate(working_copy=working_copy, document=document, summary=summary)
            for working_copy, document, summary in rows
        ]

    def _load_suggestions(
        self,
        candidates: list[_SearchCandidate],
    ) -> dict[str, DocumentCategorySuggestion]:
        """读取每个当前版本排名最高的分类建议，分类仅作为召回信号。"""

        version_by_document = {
            item.document.id: item.working_copy.current_version_id for item in candidates
        }
        if not version_by_document:
            return {}
        rows = (
            self.db.query(DocumentCategorySuggestion)
            .filter(DocumentCategorySuggestion.document_id.in_(list(version_by_document)))
            .order_by(
                DocumentCategorySuggestion.document_id.asc(),
                DocumentCategorySuggestion.rank.asc(),
                DocumentCategorySuggestion.created_at.desc(),
            )
            .all()
        )
        selected: dict[str, DocumentCategorySuggestion] = {}
        for row in rows:
            if row.document_id in selected:
                continue
            if row.document_version_id != version_by_document.get(row.document_id):
                continue
            if row.status not in {"SUGGESTED", "AUTO_APPLIED", "CONFIRMED"}:
                continue
            selected[row.document_id] = row
        return selected


def _score_candidate(
    *,
    candidate: _SearchCandidate,
    suggestion: DocumentCategorySuggestion | None,
    normalized_query: str,
    terms: list[str],
) -> tuple[float, dict[str, Any]] | None:
    """按最终文件名、分类和普通摘要计算可解释的确定性得分。"""

    filename_text = _normalize_text(candidate.working_copy.filename)
    summary_text = _normalize_text(
        " ".join(
            [
                candidate.summary.summary_text,
                json.dumps(candidate.summary.summary_json or {}, ensure_ascii=False),
            ]
        )
    )
    category_path = [str(item) for item in (suggestion.category_path_json if suggestion else [])]
    category_text = _normalize_text(" ".join(category_path))
    score = 0.0
    reasons: list[str] = []

    if normalized_query in filename_text:
        score += 12.0
        reasons.append("整理后的文件名与查询一致")
    elif normalized_query in summary_text:
        score += 8.0
        reasons.append("文档摘要与查询一致")
    elif normalized_query in category_text:
        score += 7.0
        reasons.append("文件分类与查询一致")

    filename_matches = _matched_terms(terms, filename_text)
    category_matches = _matched_terms(terms, category_text)
    summary_matches = _matched_terms(terms, summary_text)
    if filename_matches:
        score += sum(_term_weight(term) * 3.0 for term in filename_matches)
        reasons.append(f"文件名命中：{'、'.join(filename_matches[:4])}")
    if category_matches:
        score += sum(_term_weight(term) * 2.0 for term in category_matches)
        reasons.append(f"分类命中：{'、'.join(category_matches[:4])}")
    if summary_matches:
        score += sum(_term_weight(term) for term in summary_matches)
        reasons.append(f"摘要命中：{'、'.join(summary_matches[:4])}")
    if score <= 0:
        return None

    summary_payload = candidate.summary.summary_json or {}
    overview = str(summary_payload.get("overview") or candidate.summary.summary_text or "").strip()
    return score, {
        "working_copy_id": candidate.working_copy.id,
        "document_id": candidate.document.id,
        "document_version_id": candidate.working_copy.current_version_id,
        "filename": candidate.working_copy.filename,
        "category_path": category_path,
        "summary": overview[:500],
        "score": round(score, 4),
        "match_reasons": reasons,
        "evidence_refs": _summary_evidence_refs(summary_payload),
    }


def _query_terms(query: str) -> list[str]:
    """从自然语言请求提取稳定检索词，并保留中文主题的二至六字片段。"""

    text = normalize_file_search_query(query)
    raw_terms = re.findall(r"[a-z0-9][a-z0-9._-]*|[\u4e00-\u9fff]+", text)
    terms: set[str] = set()
    for term in raw_terms:
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            if len(term) <= 6:
                terms.add(term)
            for size in range(2, min(6, len(term)) + 1):
                terms.update(term[index : index + size] for index in range(len(term) - size + 1))
        else:
            terms.add(term)
    return sorted(terms, key=lambda item: (-len(item), item))


def _matched_terms(terms: list[str], text: str) -> list[str]:
    """去除被更长命中词覆盖的短词，避免重复累加得分。"""

    matches: list[str] = []
    for term in terms:
        normalized = _normalize_text(term)
        if len(normalized) < 2 or normalized not in text:
            continue
        if any(normalized in existing for existing in matches):
            continue
        matches.append(normalized)
    return matches[:12]


def _term_weight(term: str) -> float:
    """较长主题词比短片段具有更高区分度。"""

    return 1.0 + min(len(term), 8) / 8


def _normalize_text(value: str) -> str:
    """统一大小写并移除不参与检索的标点和空白。"""

    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value).lower())


def _summary_evidence_refs(summary_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """仅返回摘要中已校验的定位引用，供后续原文检索路由使用。"""

    refs: list[dict[str, Any]] = []
    for point in summary_payload.get("key_points", []) or []:
        if not isinstance(point, dict):
            continue
        for ref in point.get("evidence_refs", []) or []:
            if isinstance(ref, dict) and ref.get("quote"):
                refs.append(
                    {
                        "page_number": ref.get("page_number"),
                        "sheet_name": ref.get("sheet_name"),
                        "quote": str(ref.get("quote"))[:240],
                    }
                )
    return refs[:5]
