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

from typing import Any

import sqlalchemy as sa
from app.db.models import Document, WorkingCopy
from app.modules.retrieval.chunk_lexical_search import DocumentChunkLexicalSearchService
from app.modules.retrieval.evidence_projector import SearchEvidenceProjector
from app.modules.retrieval.stage1_document_recall import Stage1DocumentRecallService


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
            db=db, user_id=user_id, tokenizer=tokenizer,
        )
        self.evidence = SearchEvidenceProjector(
            db=db, user_id=user_id, workspace_id=workspace_id,
        )

    def search(
        self,
        *,
        query: str,
        parsed_query: Any | None = None,
        scope: Any | None = None,
    ) -> dict[str, Any]:
        """执行两阶段检索，返回确定性融合结果。"""

        query = str(query or "")[: min(int(self.config.retrieval_query_max_chars), 500)]
        if not query or not (parsed_query and parsed_query.cleaned):
            return {
                "ok": True,
                "kind": "workspace_file_search",
                "query": query,
                "total_returned": 0,
                "partial": False,
                "results": [],
                "user_message": "",
            }

        self._apply_postgresql_statement_timeout()

        # 一阶段：文档级索引召回
        stage1_candidates = self.stage1.recall(
            parsed_query=parsed_query, scope=scope,
        )

        # 必要时补召回
        candidate_limit = min(int(self.config.retrieval_document_candidate_limit), 50)
        chunk_degraded = False
        if len(stage1_candidates) < candidate_limit:
            try:
                if getattr(scope, "scope_mode", "global") == "strict":
                    fallback_versions = self._strict_scope_fallback(
                        query=parsed_query.cleaned, scope=scope,
                    )
                else:
                    fallback_versions = self.stage2.fallback_recall(
                        query=parsed_query.cleaned,
                        workspace_id=self.workspace_id,
                        max_versions=10,
                    )
                stage1_candidates = self._merge_fallback(
                    stage1_candidates,
                    self.stage1.enrich_fallback_versions(
                        fallback_versions=fallback_versions,
                        scope=scope,
                    ),
                )
            except Exception:
                # 补召回失败不阻塞主路径
                chunk_degraded = True

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
            try:
                chunk_results = self.stage2.search(
                    query=parsed_query.cleaned,
                    document_version_ids=version_ids,
                    limit=min(int(self.config.retrieval_chunk_global_limit), 24),
                )
                chunk_results = self._limit_chunks_per_document(chunk_results)
            except Exception:
                chunk_results = []
                chunk_degraded = True

        # Evidence 投影
        evidence_map = {}
        if chunk_results:
            chunk_ids = [c["chunk_id"] for c in chunk_results if c.get("chunk_id")]
            if chunk_ids:
                try:
                    evidence_map = self.evidence.project(
                        chunk_ids=chunk_ids,
                        max_preview_chars=self.config.retrieval_preview_max_chars,
                    )
                except Exception:
                    evidence_map = {}

        # 融合排序
        fused = self._fuse_and_rank(
            stage1_candidates=stage1_candidates,
            chunk_results=chunk_results,
            evidence_map=evidence_map,
            parsed_query=parsed_query,
            scope=scope,
        )

        partial = chunk_degraded
        return {
            "ok": True,
            "kind": "workspace_file_search",
            "query": query,
            "total_returned": len(fused),
            "partial": partial,
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

    def _strict_scope_fallback(self, *, query: str, scope: Any) -> list[dict]:
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
                    Document.user_id == self.user_id,
                )
                .all()
            )
        ]
        return self.stage2.search(
            query=query,
            document_version_ids=version_ids,
            limit=min(int(self.config.retrieval_chunk_global_limit), 24),
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
                    "document_id": c.get("document_id"),
                    "document_version_id": vid,
                    "filename": c.get("filename", ""),
                    "category_path": c.get("category_path", []),
                    "year": c.get("year"),
                    "overview": c.get("summary", "")[:500],
                    "match_reasons": reasons,
                    "match_location": match_location,
                    "evidence_preview": evidence_preview,
                    "_score": fused_score,
                }
            )

        # 排序：按融合分倒序，并列时用 stable working_copy_id
        results.sort(key=lambda r: (-r["_score"], r["working_copy_id"] or ""))
        # 移除内部 _score
        for r in results:
            del r["_score"]
        return results

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
        elif hit_source == "gin_search" and filename:
            reasons.append(f"文件名命中：{filename}")
        elif hit_source == "trgm_fallback":
            reasons.append("文件名模糊匹配（轻微错字）")
        elif hit_source == "chunk_fallback":
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
