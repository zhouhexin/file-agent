"""两阶段文件检索的编排入口。

TwoStageFileSearchService 组合：
1. 第一阶段：Stage1DocumentRecallService（基于 document_search_profiles 索引）
2. 必要时：fallback_recall（基于 document_chunks GIN）
3. 第二阶段：在候选版本内精查 DocumentChunk
4. SearchEvidenceProjector：读取 EvidenceSpan 位置和短预览
5. 确定性融合排序

不直接访问文件系统、不返回正文、不修改任何数据。
embedding 分支关闭时，其权重重新分配给 Chunk 词法相关度。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

import sqlalchemy as sa
from app.core.logging import log_event
from app.db.models import Document, WorkingCopy
from app.modules.retrieval.chunk_lexical_search import DocumentChunkLexicalSearchService
from app.modules.retrieval.evidence_projector import SearchEvidenceProjector
from app.modules.retrieval.query_parser import exact_short_chinese_phrase
from app.modules.retrieval.stage1_document_recall import Stage1DocumentRecallService
from app.modules.retrieval.managed_source_search import ManagedSourceSearchService


class _DefaultConfig:
    retrieval_document_candidate_limit: int = 30
    retrieval_document_detail_limit: int = 12
    retrieval_chunk_limit_per_document: int = 3
    retrieval_chunk_global_limit: int = 24
    retrieval_query_max_chars: int = 500
    retrieval_preview_max_chars: int = 240
    retrieval_statement_timeout_ms: int = 2000
    retrieval_filename_trgm_min_chars: int = 4
    retrieval_filename_trgm_candidate_limit: int = 20
    retrieval_filename_trgm_similarity_threshold: float = 0.25
    two_stage_retrieval_enabled: bool = True


class TwoStageFileSearchService:
    """两阶段文件检索唯一编排入口。"""

    # 版本化权重常量
    WEIGHT_DOCUMENT = 0.40
    WEIGHT_CHUNK = 0.35
    WEIGHT_SCOPE = 0.20
    WEIGHT_TIME = 0.05
    WEIGHT_EXACT_FILENAME_BOOST = 0.15
    WEIGHT_EXACT_YEAR_BOOST = 0.10

    def __init__(
        self,
        *,
        db: Any,
        user_id: str,
        workspace_id: str,
        config: Any | None = None,
        tokenizer: Any | None = None,
    ) -> None:
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.config = config or _DefaultConfig()
        self.tokenizer = tokenizer
        self.stage1 = Stage1DocumentRecallService(
            db=db, user_id=user_id, workspace_id=workspace_id,
            config=self.config, tokenizer=tokenizer,
        )
        self.stage2 = DocumentChunkLexicalSearchService(
            db=db,
            user_id=user_id,
            workspace_id=workspace_id,
            tokenizer=tokenizer,
        )
        self.evidence = SearchEvidenceProjector(
            db=db, user_id=user_id, workspace_id=workspace_id,
        )
        self.managed_source = ManagedSourceSearchService(
            db=db, workspace_id=workspace_id, tokenizer=tokenizer,
        )

    def search(
        self,
        *,
        query: str,
        parsed_query: Any | None = None,
        scope: Any | None = None,
        exact_phrase: str | None = None,
        require_body_evidence: bool = False,
        include_internal_match_flags: bool = False,
    ) -> dict[str, Any]:
        """执行两阶段检索，返回确定性融合结果。"""

        started_at = time.perf_counter()
        query = str(query or "")[: min(int(self.config.retrieval_query_max_chars), 500)]
        cleaned_query = str(getattr(parsed_query, "cleaned", "") or "")
        query_fingerprint = (
            hashlib.sha256(cleaned_query.encode("utf-8")).hexdigest()[:12]
            if cleaned_query
            else None
        )
        scope_mode = str(getattr(scope, "scope_mode", "global") or "global")
        log_event(
            "retrieval.search.started",
            tool_name="hybrid-search",
            status="RUNNING",
            workspace_id=self.workspace_id,
            query_chars=len(query),
            cleaned_query_chars=len(cleaned_query),
            query_term_count=len(list(getattr(parsed_query, "terms", []) or [])),
            exact_short_phrase_mode=bool(
                exact_phrase or exact_short_chinese_phrase(cleaned_query)
            ),
            query_fingerprint=query_fingerprint,
            scope_mode=scope_mode,
            message="两阶段文件检索开始",
        )
        if not query or not (parsed_query and parsed_query.cleaned):
            log_event(
                "retrieval.query.rejected",
                level="WARNING",
                tool_name="hybrid-search",
                status="SKIPPED",
                workspace_id=self.workspace_id,
                query_chars=len(query),
                cleaned_query_chars=len(cleaned_query),
                query_fingerprint=query_fingerprint,
                error_code="EMPTY_CLEANED_QUERY",
                message="查询清洗后为空，停止检索",
            )
            return {
                "ok": True,
                "kind": "workspace_file_search",
                "query": query,
                "total_returned": 0,
                "partial": False,
                "results": [],
                "user_message": "",
            }

        # PostgreSQL 一旦 SQL 失败会把整个事务标记为 aborted。检索与 AgentRun 审计共用
        # 请求级 Session，因此每段可能降级的只读 SQL 都必须放进独立 savepoint。
        with self.db.begin_nested():
            self._apply_postgresql_statement_timeout()

        # 一阶段：文档级索引召回
        stage1_started_at = time.perf_counter()
        try:
            with self.db.begin_nested():
                stage1_candidates = self.stage1.recall(
                    parsed_query=parsed_query, scope=scope,
                )
        except Exception as exc:
            log_event(
                "retrieval.stage1.failed",
                level="ERROR",
                tool_name="hybrid-search",
                status="FAILED",
                duration_ms=int((time.perf_counter() - stage1_started_at) * 1000),
                workspace_id=self.workspace_id,
                query_fingerprint=query_fingerprint,
                error_code=exc.__class__.__name__,
                message="文件级候选召回失败",
            )
            raise
        log_event(
            "retrieval.stage1.completed",
            tool_name="hybrid-search",
            status="COMPLETED",
            duration_ms=int((time.perf_counter() - stage1_started_at) * 1000),
            workspace_id=self.workspace_id,
            query_fingerprint=query_fingerprint,
            candidate_count=len(stage1_candidates),
            scope_mode=scope_mode,
            message="文件级候选召回完成",
        )

        # 必要时补召回
        candidate_limit = min(int(self.config.retrieval_document_candidate_limit), 50)
        # 候选达到保护上限时，系统无法证明是否仍有未进入精查阶段的匹配文件。
        # 该事实必须显式传给完整性回执，不能只作为内部性能参数静默丢弃。
        candidate_limit_reached = len(stage1_candidates) >= candidate_limit
        chunk_degraded = False
        fallback_count = 0
        if exact_phrase or len(stage1_candidates) < candidate_limit:
            fallback_started_at = time.perf_counter()
            try:
                with self.db.begin_nested():
                    if getattr(scope, "scope_mode", "global") == "strict":
                        fallback_versions = self._strict_scope_fallback(
                            query=parsed_query.cleaned,
                            scope=scope,
                            exact_phrase=exact_phrase,
                        )
                    else:
                        fallback_versions = self.stage2.fallback_recall(
                            query=parsed_query.cleaned,
                            workspace_id=self.workspace_id,
                            max_versions=10,
                            exact_phrase_override=exact_phrase,
                        )
                    fallback_count = len(fallback_versions)
                    enriched_fallback = self.stage1.enrich_fallback_versions(
                        fallback_versions=fallback_versions,
                        scope=scope,
                    )
                    # 连续短语的正文命中比文件级宽泛候选更可靠，必须优先进入二阶段；
                    # 否则大量摘要 OR 候选可能挤掉真正含有完整短语的文件。
                    stage1_candidates = (
                        self._merge_fallback(enriched_fallback, stage1_candidates)
                        if exact_phrase
                        else self._merge_fallback(stage1_candidates, enriched_fallback)
                    )
                log_event(
                    "retrieval.chunk_fallback.completed",
                    tool_name="hybrid-search",
                    status="COMPLETED",
                    duration_ms=int((time.perf_counter() - fallback_started_at) * 1000),
                    workspace_id=self.workspace_id,
                    query_fingerprint=query_fingerprint,
                    fallback_version_count=fallback_count,
                    merged_candidate_count=len(stage1_candidates),
                    scope_mode=scope_mode,
                    message="正文 Chunk 补召回完成",
                )
            except Exception as exc:
                # 补召回失败不阻塞主路径
                chunk_degraded = True
                log_event(
                    "retrieval.chunk_fallback.failed",
                    level="ERROR",
                    tool_name="hybrid-search",
                    status="DEGRADED",
                    duration_ms=int((time.perf_counter() - fallback_started_at) * 1000),
                    workspace_id=self.workspace_id,
                    query_fingerprint=query_fingerprint,
                    error_code=exc.__class__.__name__,
                    message="正文 Chunk 补召回失败，检索降级为文件级候选",
                )
        else:
            log_event(
                "retrieval.chunk_fallback.skipped",
                tool_name="hybrid-search",
                status="SKIPPED",
                workspace_id=self.workspace_id,
                query_fingerprint=query_fingerprint,
                candidate_count=len(stage1_candidates),
                candidate_limit=candidate_limit,
                message="文件级候选已达到上限，无需正文补召回",
            )

        # 取 top N 候选进入第二阶段
        detail_limit = min(int(self.config.retrieval_document_detail_limit), 20)
        version_ids = [
            c.get("document_version_id")
            for c in stage1_candidates[:detail_limit]
            if c.get("document_version_id")
        ]

        # 二阶段：在候选版本内精查
        chunk_results = []
        if version_ids:
            detail_started_at = time.perf_counter()
            try:
                with self.db.begin_nested():
                    chunk_results = self.stage2.search(
                        query=parsed_query.cleaned,
                        document_version_ids=version_ids,
                        limit=min(int(self.config.retrieval_chunk_global_limit), 24),
                        exact_phrase_override=exact_phrase,
                    )
                chunk_results = self._limit_chunks_per_document(chunk_results)
                log_event(
                    "retrieval.stage2.completed",
                    tool_name="hybrid-search",
                    status="COMPLETED",
                    duration_ms=int((time.perf_counter() - detail_started_at) * 1000),
                    workspace_id=self.workspace_id,
                    query_fingerprint=query_fingerprint,
                    candidate_version_count=len(version_ids),
                    chunk_result_count=len(chunk_results),
                    message="候选文档内正文精查完成",
                )
            except Exception as exc:
                chunk_results = []
                chunk_degraded = True
                log_event(
                    "retrieval.stage2.failed",
                    level="ERROR",
                    tool_name="hybrid-search",
                    status="DEGRADED",
                    duration_ms=int((time.perf_counter() - detail_started_at) * 1000),
                    workspace_id=self.workspace_id,
                    query_fingerprint=query_fingerprint,
                    candidate_version_count=len(version_ids),
                    error_code=exc.__class__.__name__,
                    message="候选文档内正文精查失败",
                )
        else:
            log_event(
                "retrieval.stage2.skipped",
                level="WARNING",
                tool_name="hybrid-search",
                status="SKIPPED",
                workspace_id=self.workspace_id,
                query_fingerprint=query_fingerprint,
                candidate_version_count=0,
                error_code="NO_CANDIDATE_VERSIONS",
                message="没有可进入正文精查的当前文档版本",
            )

        # 显式年月是硬过滤条件，不只是排序加分。组合查询先按主题召回，再在相同
        # 候选版本内验证日期；这样“2017年6月的授牌材料”不会被当成一个不存在的连续短语。
        year_match_version_ids: set[str] = set()
        explicit_year = getattr(parsed_query, "year", None)
        explicit_month = getattr(parsed_query, "month", None)
        if explicit_year and explicit_month:
            explicit_date_query = f"{int(explicit_year)}年{int(explicit_month)}月"
        elif explicit_year:
            explicit_date_query = str(explicit_year)
        elif explicit_month:
            explicit_date_query = f"{int(explicit_month)}月"
        else:
            explicit_date_query = ""
        if explicit_date_query and version_ids:
            if cleaned_query == explicit_date_query:
                year_match_version_ids = {
                    str(item.get("document_version_id"))
                    for item in chunk_results
                    if item.get("document_version_id")
                }
            else:
                year_started_at = time.perf_counter()
                try:
                    with self.db.begin_nested():
                        year_chunks = self.stage2.search(
                            query=explicit_date_query,
                            document_version_ids=version_ids,
                            limit=min(int(self.config.retrieval_chunk_global_limit), 24),
                            exact_phrase_override=explicit_date_query,
                        )
                    year_match_version_ids = {
                        str(item.get("document_version_id"))
                        for item in year_chunks
                        if item.get("document_version_id")
                    }
                    log_event(
                        "retrieval.year_filter.completed",
                        tool_name="hybrid-search",
                        status="COMPLETED",
                        duration_ms=int((time.perf_counter() - year_started_at) * 1000),
                        workspace_id=self.workspace_id,
                        query_fingerprint=query_fingerprint,
                        candidate_version_count=len(version_ids),
                        matched_version_count=len(year_match_version_ids),
                        year=int(explicit_year) if explicit_year else None,
                        month=int(explicit_month) if explicit_month else None,
                        message="显式年月候选验证完成",
                    )
                except Exception as exc:
                    chunk_degraded = True
                    log_event(
                        "retrieval.year_filter.failed",
                        level="ERROR",
                        tool_name="hybrid-search",
                        status="DEGRADED",
                        duration_ms=int((time.perf_counter() - year_started_at) * 1000),
                        workspace_id=self.workspace_id,
                        query_fingerprint=query_fingerprint,
                        error_code=exc.__class__.__name__,
                        year=int(explicit_year) if explicit_year else None,
                        month=int(explicit_month) if explicit_month else None,
                        message="显式年月正文验证失败，保留文件名和摘要日期校验",
                    )

        # Evidence 投影
        evidence_map = {}
        if chunk_results:
            chunk_ids = [c["chunk_id"] for c in chunk_results if c.get("chunk_id")]
            if chunk_ids:
                evidence_started_at = time.perf_counter()
                try:
                    with self.db.begin_nested():
                        evidence_map = self.evidence.project(
                            chunk_ids=chunk_ids,
                            max_preview_chars=self.config.retrieval_preview_max_chars,
                        )
                    log_event(
                        "retrieval.evidence.completed",
                        tool_name="hybrid-search",
                        status="COMPLETED",
                        duration_ms=int((time.perf_counter() - evidence_started_at) * 1000),
                        workspace_id=self.workspace_id,
                        query_fingerprint=query_fingerprint,
                        requested_chunk_count=len(chunk_ids),
                        projected_evidence_count=len(evidence_map),
                        message="检索证据投影完成",
                    )
                except Exception as exc:
                    evidence_map = {}
                    chunk_degraded = True
                    log_event(
                        "retrieval.evidence.failed",
                        level="ERROR",
                        tool_name="hybrid-search",
                        status="DEGRADED",
                        duration_ms=int((time.perf_counter() - evidence_started_at) * 1000),
                        workspace_id=self.workspace_id,
                        query_fingerprint=query_fingerprint,
                        requested_chunk_count=len(chunk_ids),
                        error_code=exc.__class__.__name__,
                        message="检索证据投影失败",
                    )
        else:
            log_event(
                "retrieval.evidence.skipped",
                level="WARNING",
                tool_name="hybrid-search",
                status="SKIPPED",
                workspace_id=self.workspace_id,
                query_fingerprint=query_fingerprint,
                error_code="NO_CHUNK_RESULTS",
                message="没有正文命中，跳过证据投影",
            )

        # 融合排序
        fused = self._fuse_and_rank(
            stage1_candidates=stage1_candidates,
            chunk_results=chunk_results,
            evidence_map=evidence_map,
            parsed_query=parsed_query,
            scope=scope,
            year_match_version_ids=year_match_version_ids,
        )
        # 源侧分支只补充当前未被同修订工作副本覆盖的记录。两边均已完成
        # 相关性校验后才合并，不能把扩大召回候选误放入用户最终列表。
        source_state = {"results": [], "pending_count": 0, "failed_count": 0, "eligible_count": 0}
        if bool(getattr(self.config, "managed_source_search_enabled", True)):
            try:
                # 源侧搜索只是双范围补充分支。其索引异常不能回滚已经成功的
                # 工作副本检索，也不能让用户误以为工作副本结果不存在。
                with self.db.begin_nested():
                    source_state = self.managed_source.search(
                        parsed_query=parsed_query,
                        scope=scope,
                        limit=candidate_limit,
                        # 短语策略需要源侧同样提供连续正文命中，不能只让工作
                        # 副本分支执行该约束，否则双范围结果会出现分级不一致。
                        exact_phrase=exact_phrase,
                    )
            except Exception as exc:
                source_state = {
                    "results": [],
                    "pending_count": 0,
                    "failed_count": 0,
                    "eligible_count": 0,
                    "degraded": True,
                }
                log_event(
                    "retrieval.managed_source.failed",
                    level="ERROR",
                    tool_name="hybrid-search",
                    status="DEGRADED",
                    workspace_id=self.workspace_id,
                    query_fingerprint=query_fingerprint,
                    error_code=exc.__class__.__name__,
                    message="受管原始文件检索分支失败，保留工作副本检索结果",
                )
            source_results = list(source_state.get("results") or [])
            # 源侧服务已经按“内容一致的活动工作副本”完成去重。此处不能再按
            # ``managed_file_id`` 排除，否则源文件出现新修订而旧工作副本仍存在时，
            # 新修订会被静默遗漏。
            fused.extend(source_results)
            fused.sort(
                key=lambda item: (
                    -float(item.get("_score") or 0.0),
                    str(item.get("working_copy_id") or item.get("managed_file_revision_id") or ""),
                )
            )
        if require_body_evidence:
            # “正文提到/包含/出现”是事实约束；没有 Chunk 连续命中的文件即使文件名或摘要相关，
            # 也不能作为正文命中返回给用户。
            fused = [
                item for item in fused if bool(item.get("_body_phrase_hit"))
            ]
        if not include_internal_match_flags:
            for item in fused:
                item.pop("_body_phrase_hit", None)

        partial = chunk_degraded or bool(
            source_state.get("pending_count")
            or source_state.get("failed_count")
            or source_state.get("degraded")
        )
        log_event(
            "retrieval.search.completed",
            level="WARNING" if partial or not fused else "INFO",
            tool_name="hybrid-search",
            status="DEGRADED" if partial else "COMPLETED",
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            workspace_id=self.workspace_id,
            query_fingerprint=query_fingerprint,
            stage1_candidate_count=len(stage1_candidates),
            fallback_version_count=fallback_count,
            detail_version_count=len(version_ids),
            chunk_result_count=len(chunk_results),
            evidence_count=len(evidence_map),
            source_result_count=len(source_state.get("results") or []),
            source_pending_count=int(source_state.get("pending_count") or 0),
            source_failed_count=int(source_state.get("failed_count") or 0),
            result_count=len(fused),
            partial=partial,
            candidate_limit_reached=candidate_limit_reached,
            message="两阶段文件检索完成",
        )
        return {
            "ok": True,
            "kind": "workspace_file_search",
            "query": query,
            "total_returned": len(fused),
            "partial": partial,
            "candidate_limit_reached": candidate_limit_reached,
            "managed_source_coverage": {
                "eligible_file_count": int(source_state.get("eligible_count") or 0),
                "pending_file_count": int(source_state.get("pending_count") or 0),
                "failed_file_count": int(source_state.get("failed_count") or 0),
            },
            "results": fused,
            "user_message": self._build_user_message(fused, partial),
        }

    def _apply_postgresql_statement_timeout(self) -> None:
        """在当前事务内限定检索 SQL 耗时，不影响连接池的后续业务请求。"""
        bind = getattr(self.db, "bind", None)
        if bind is None or bind.dialect.name != "postgresql":
            return
        timeout = min(int(self.config.retrieval_statement_timeout_ms), 2000)
        self.db.execute(
            sa.text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{max(100, timeout)}ms"},
        )

    def _merge_fallback(
        self,
        stage1_candidates: list[dict],
        fallback_candidates: list[dict],
    ) -> list[dict]:
        """合并补召回结果到第一阶段候选。"""
        seen = {c.get("working_copy_id") for c in stage1_candidates}
        for candidate in fallback_candidates:
            working_copy_id = candidate.get("working_copy_id")
            if working_copy_id and working_copy_id not in seen:
                stage1_candidates.append(candidate)
                seen.add(working_copy_id)
        return stage1_candidates

    def _strict_scope_fallback(
        self, *, query: str, scope: Any, exact_phrase: str | None = None
    ) -> list[dict]:
        """只在后端已解析的 L0 文件当前版本内补召回，禁止扩大到工作区。"""
        document_ids = list(getattr(scope, "strict_document_ids", ()) or ())
        if not document_ids:
            return []
        version_ids = [
            row.current_version_id
            for row in (
                self.db.query(WorkingCopy.current_version_id)
                .join(Document, Document.id == WorkingCopy.document_id)
                .filter(
                    WorkingCopy.workspace_id == self.workspace_id,
                    WorkingCopy.status == "ACTIVE",
                    WorkingCopy.document_id.in_(document_ids),
                    WorkingCopy.current_version_id.isnot(None),
                )
                .all()
            )
        ]
        return self.stage2.search(
            query=query,
            document_version_ids=version_ids,
            limit=min(int(self.config.retrieval_chunk_global_limit), 24),
            exact_phrase_override=exact_phrase,
        )

    def _limit_chunks_per_document(self, chunks: list[dict]) -> list[dict]:
        """应用每版本 3 个、全局 24 个硬上限，防止单份长文档耗尽结果预算。"""
        per_document_limit = min(int(self.config.retrieval_chunk_limit_per_document), 3)
        global_limit = min(int(self.config.retrieval_chunk_global_limit), 24)
        counts: dict[str, int] = {}
        result: list[dict] = []
        for chunk in chunks:
            version_id = str(chunk.get("document_version_id") or "")
            if not version_id or counts.get(version_id, 0) >= per_document_limit:
                continue
            result.append(chunk)
            counts[version_id] = counts.get(version_id, 0) + 1
            if len(result) >= global_limit:
                break
        return result

    def _fuse_and_rank(
        self,
        *,
        stage1_candidates: list[dict],
        chunk_results: list[dict],
        evidence_map: dict[str, dict],
        parsed_query: Any,
        scope: Any,
        year_match_version_ids: set[str] | None = None,
    ) -> list[dict]:
        """确定性融合排序。"""

        # 收集所有候选的工作副本和版本
        # 第一阶段结果有 working_copy_id 和 document_version_id
        # 补召回结果只有 document_version_id
        version_to_chunk_score: dict[str, float] = {}
        for c in chunk_results:
            vid = c.get("document_version_id")
            if vid:
                current = version_to_chunk_score.get(vid, 0.0)
                version_to_chunk_score[vid] = max(current, float(c.get("score", 0.0)))

        # 构建每文档的最终结果
        results = []
        for c in stage1_candidates:
            wc_id = c.get("working_copy_id")
            vid = c.get("document_version_id")
            if (
                getattr(parsed_query, "year", None)
                or getattr(parsed_query, "month", None)
            ) and not self._matches_explicit_date(
                candidate=c,
                year=(int(parsed_query.year) if parsed_query.year else None),
                month=(
                    int(parsed_query.month)
                    if getattr(parsed_query, "month", None)
                    else None
                ),
                year_match_version_ids=year_match_version_ids or set(),
            ):
                continue
            doc_score = float(c.get("_score", 0.0))
            chunk_score = version_to_chunk_score.get(vid, 0.0)

            # 归一化（简单线性归一化）
            # 注意：实际归一化需要候选集合统计，这里使用 doc_score/2 + chunk_score/2 作为简单加权
            # 简化版融合：document_score 加权 + chunk_score 加权
            fused_score = (
                doc_score * self.WEIGHT_DOCUMENT
                + chunk_score * self.WEIGHT_CHUNK
                + float(c.get("_scope_weight", 0.4)) * self.WEIGHT_SCOPE
                + 0.5 * self.WEIGHT_TIME   # 默认 time_weight
            )

            # 精确文件名加权
            if parsed_query and parsed_query.cleaned:
                if parsed_query.cleaned in (c.get("filename") or ""):
                    fused_score += self.WEIGHT_EXACT_FILENAME_BOOST

            # 显式年份加权
            if parsed_query and parsed_query.year:
                year_val = c.get("year")
                if year_val == parsed_query.year:
                    fused_score += self.WEIGHT_EXACT_YEAR_BOOST

            # 查找 Evidence
            evidence_preview = ""
            match_location = None
            for chunk in chunk_results:
                if chunk.get("document_version_id") == vid:
                    cid = chunk.get("chunk_id")
                    if cid and cid in evidence_map:
                        ev = evidence_map[cid]
                        evidence_preview = ev.get("preview", "")
                        match_location = {
                            "page_number": ev.get("page_number"),
                            "sheet_name": ev.get("sheet_name"),
                            "cell_range": ev.get("cell_range"),
                        }
                        break

            # 推荐原因（用户可理解）
            reasons = self._build_match_reasons(c, chunk_score > 0, evidence_preview)

            results.append(
                {
                    "working_copy_id": wc_id,
                    "resource_type": "WORKING_COPY",
                    "managed_file_id": c.get("managed_file_id"),
                    "document_id": c.get("document_id"),
                    "document_version_id": vid,
                    "filename": c.get("filename", ""),
                    # 逻辑路径只用于用户区分同名文件，绝不能替换为容器绝对路径。
                    "root_key": c.get("root_key"),
                    "relative_path": c.get("relative_path"),
                    "category_path": c.get("category_path", []),
                    "year": c.get("year"),
                    "overview": c.get("summary", "")[:500],
                    "match_reasons": reasons,
                    "match_location": match_location,
                    "evidence_preview": evidence_preview,
                    "_body_phrase_hit": chunk_score > 0,
                    "_score": fused_score,
                    # 普通检索的基础词法结果仍需上层策略完成相关性分级；不应
                    # 因为有文件名或摘要命中就自动触发批量工作副本物化。
                    "relevance_tier": "RELATED" if c.get("_hit_source") == "exact_filename" else "POSSIBLE",
                }
            )

        # 排序：按融合分倒序，并列时用 stable working_copy_id
        results.sort(key=lambda r: (-r["_score"], r["working_copy_id"] or ""))
        # 移除内部 _score
        for r in results:
            del r["_score"]
        return results

    @staticmethod
    def _matches_explicit_date(
        *,
        candidate: dict,
        year: int | None,
        month: int | None,
        year_match_version_ids: set[str],
    ) -> bool:
        """通过结构化摘要、文件名、摘要文本或正文 Chunk 验证显式年月。"""

        version_id = str(candidate.get("document_version_id") or "")
        if version_id in year_match_version_ids:
            return True
        searchable_text = " ".join(
            [
                str(candidate.get("filename") or ""),
                str(candidate.get("summary") or ""),
            ]
        )
        if month is not None:
            month_pattern = rf"(?:0?{month})\s*月"
            if year is not None:
                if re.search(
                    rf"{year}\s*(?:年|[-_./])?\s*0?{month}(?:\s*月)?",
                    searchable_text,
                ):
                    return True
                # 同时指定年月时，只有结构化年份不能单独满足月份条件。
                return False
            return re.search(month_pattern, searchable_text) is not None
        if year is None:
            return True
        year_text = str(year)
        structured_year = candidate.get("year")
        if isinstance(structured_year, (list, tuple, set)):
            if year_text in {str(item) for item in structured_year}:
                return True
        elif structured_year is not None and str(structured_year) == year_text:
            return True
        if year_text in str(candidate.get("filename") or ""):
            return True
        if year_text in str(candidate.get("summary") or ""):
            return True
        return False

    def _build_match_reasons(
        self,
        candidate: dict,
        chunk_hit: bool,
        evidence_preview: str,
    ) -> list[str]:
        """生成用户可理解的推荐原因。"""
        reasons = []
        filename = candidate.get("filename", "")
        hit_source = candidate.get("_hit_source", "")
        category_path = candidate.get("category_path") or []
        overview = candidate.get("summary", "") or ""

        if hit_source == "exact_filename":
            reasons.append("整理后的文件名精确匹配查询")
        elif hit_source == "exact_short_phrase":
            reasons.append("文件名、摘要或实体短语连续命中查询")
        elif hit_source == "gin_search" and filename:
            reasons.append(f"文件名命中：{filename}")
        elif hit_source == "trgm_fallback":
            reasons.append("文件名模糊匹配（轻微错字）")
        elif hit_source in {"chunk_fallback", "exact_short_phrase_chunk"}:
            reasons.append("原文命中查询")

        if category_path:
            cat_str = "/".join(category_path)
            if cat_str:
                reasons.append(f"分类命中：{cat_str}")

        if overview:
            reasons.append("摘要命中查询词")

        if chunk_hit:
            reasons.append("原文 Chunk 命中查询词")

        if not reasons:
            reasons.append("命中相关文档")

        return reasons[:5]

    def _build_user_message(
        self, results: list[dict], partial: bool
    ) -> str:
        """生成对用户的友好提示。"""
        if not results:
            return "未找到相关文件。请尝试补充主题、年份、单位或文档类型。"
        if partial:
            return "找到部分文件，但部分原文索引暂不可用。"
        return ""
