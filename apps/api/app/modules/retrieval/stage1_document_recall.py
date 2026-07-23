"""第一阶段数据库索引召回。

把"加载所有候选到 Python 内存再遍历评分"改为"PostgreSQL 索引查询"。
召回顺序：
1. normalized_filename 精确匹配（B-tree 索引）
2. Jieba/GIN search_vector 主召回（setweight: A=文件名, B=分类, C=元数据, D=摘要）
3. 受限 pg_trgm 补召回（仅当查询 ≥ 配置最小长度、精确+GIN 不足时启用）

候选收敛后以一次批量 JOIN 补齐显示字段，不逐文件 N+1 读取。
SQLite 下使用 deterministic token 覆盖降级。
"""

from __future__ import annotations

from typing import Any

from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentSearchProfile,
    DocumentSummary,
    WorkingCopy,
)


class Stage1DocumentRecallService:
    """第一阶段数据库索引召回。

    不直接访问文件系统，不修改任何数据。
    所有权校验在查询时通过 user_id + workspace_id + status 过滤完成。
    """

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

    def recall(self, *, parsed_query: Any, scope: Any) -> list[dict]:
        """执行第一阶段召回，返回候选列表。

        返回的结构化候选包含 working_copy_id、document_version_id、score、hit_source。
        候选收敛后调用 enrich() 补齐显示字段。
        """
        if self.db.bind.dialect.name == "postgresql":
            candidates = self._recall_postgresql(parsed_query, scope)
        else:
            candidates = self._recall_deterministic(parsed_query, scope)

        # 去重 + 上限
        seen = {}
        for c in candidates:
            wc_id = c.get("working_copy_id")
            if wc_id and wc_id not in seen:
                seen[wc_id] = c
        result = list(seen.values())
        result.sort(key=lambda x: -x.get("_score", 0.0))
        result = result[: self.config.retrieval_document_candidate_limit]

        # 富化
        return self._enrich(result, scope=scope)

    def enrich_fallback_versions(
        self,
        *,
        fallback_versions: list[dict],
        scope: Any,
    ) -> list[dict]:
        """把 Chunk 补召回的版本转换为已校验的工作副本候选。

        Chunk 索引只知道内容版本，普通用户结果必须回到 ACTIVE 工作副本和当前版本，
        不能返回空文件名、空 document_id 或历史版本。
        """
        score_by_version = {
            str(item.get("document_version_id")): float(item.get("score") or 0.0)
            for item in fallback_versions
            if item.get("document_version_id")
        }
        if not score_by_version:
            return []
        rows = (
            self.db.query(WorkingCopy.id, WorkingCopy.document_id, WorkingCopy.current_version_id)
            .join(Document, Document.id == WorkingCopy.document_id)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.current_version_id.in_(list(score_by_version)),
                Document.user_id == self.user_id,
            )
            .all()
        )
        candidates = [
            {
                "working_copy_id": row.id,
                "document_id": row.document_id,
                "document_version_id": row.current_version_id,
                "_score": score_by_version[row.current_version_id],
                "_hit_source": "chunk_fallback",
            }
            for row in rows
            if getattr(scope, "scope_mode", "global") != "strict"
            or row.document_id in set(getattr(scope, "strict_document_ids", ()) or ())
        ]
        return self._enrich(candidates, scope=scope)

    def _scope_predicates(self, scope: Any) -> list[Any]:
        """生成后端已解析的 L0/L1/L4 范围谓词。

        严格范围为空时必须返回 false，不能把“无法唯一解析附件”扩展为整个工作区。
        """
        if getattr(scope, "scope_mode", "global") != "strict":
            return []
        document_ids = list(getattr(scope, "strict_document_ids", ()) or ())
        if not document_ids:
            import sqlalchemy as sa

            return [sa.false()]
        return [DocumentSearchProfile.document_id.in_(document_ids)]

    def _recall_postgresql(
        self, parsed_query: Any, scope: Any
    ) -> list[dict]:
        """PostgreSQL 索引召回。

        召回顺序：
        1. normalized_filename 精确匹配
        2. search_vector GIN 主召回
        3. pg_trgm 补召回（若不足）
        """
        query_text = parsed_query.cleaned if hasattr(parsed_query, "cleaned") else ""
        if not query_text:
            return []

        candidates = []
        seen_wc_ids: set[str] = set()

        # 1. 精确文件名匹配
        exact = self._exact_filename_match(query_text, scope)
        for c in exact:
            wc_id = c.get("working_copy_id")
            if wc_id and wc_id not in seen_wc_ids:
                seen_wc_ids.add(wc_id)
                candidates.append(c)

        # 2. GIN 主召回
        gin = self._gin_search(query_text, scope)
        for c in gin:
            wc_id = c.get("working_copy_id")
            if wc_id and wc_id not in seen_wc_ids:
                seen_wc_ids.add(wc_id)
                candidates.append(c)

        # 3. 如果候选不足且查询达最小长度，启用 pg_trgm 补召回
        trgm_min = getattr(self.config, "retrieval_filename_trgm_min_chars", 4)
        candidate_limit = getattr(self.config, "retrieval_document_candidate_limit", 30)
        if len(candidates) < candidate_limit and len(query_text) >= trgm_min:
            trgm = self._trgm_search(query_text, scope)
            for c in trgm:
                wc_id = c.get("working_copy_id")
                if wc_id and wc_id not in seen_wc_ids:
                    seen_wc_ids.add(wc_id)
                    candidates.append(c)

        return candidates

    def _exact_filename_match(
        self, query_text: str, scope: Any
    ) -> list[dict]:
        """normalized_filename B-tree 精确匹配。"""
        from app.modules.retrieval.search_profile import _normalize_text

        normalized = _normalize_text(query_text)
        if not normalized:
            return []

        rows = (
            self.db.query(DocumentSearchProfile)
            .filter(
                DocumentSearchProfile.user_id == self.user_id,
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                DocumentSearchProfile.normalized_filename == normalized,
                *self._scope_predicates(scope),
            )
            .all()
        )
        return [
            {
                "working_copy_id": r.working_copy_id,
                "document_id": r.document_id,
                "document_version_id": r.document_version_id,
                "_score": 1.0,
                "_hit_source": "exact_filename",
            }
            for r in rows
        ]

    def _gin_search(self, query_text: str, scope: Any) -> list[dict]:
        """search_vector GIN 索引召回。

        仅 PostgreSQL 下生效；SQLite 退化为 deterministic 分词匹配。
        """
        if not query_text:
            return []

        # 获取分词后的词项
        terms = self._get_terms(query_text)
        if not terms:
            return []

        if self.db.bind.dialect.name != "postgresql":
            # SQLite deterministic: 在 search_text 列中匹配词项
            return self._deterministic_token_match(terms, scope)

        # PostgreSQL GIN 查询
        import sqlalchemy as sa

        ts_query_text = " | ".join(terms)
        ts_query = sa.func.websearch_to_tsquery("simple", ts_query_text)

        rows = (
            self.db.query(
                DocumentSearchProfile.working_copy_id,
                DocumentSearchProfile.document_id,
                DocumentSearchProfile.document_version_id,
                sa.func.ts_rank_cd(
                    DocumentSearchProfile.search_vector, ts_query
                ).label("score"),
            )
            .filter(
                DocumentSearchProfile.user_id == self.user_id,
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                DocumentSearchProfile.search_vector.op("@@")(ts_query),
                *self._scope_predicates(scope),
            )
            .order_by(sa.desc("score"))
            .limit(self.config.retrieval_document_candidate_limit)
            .all()
        )
        return [
            {
                "working_copy_id": r.working_copy_id,
                "document_id": r.document_id,
                "document_version_id": r.document_version_id,
                "_score": float(r.score) if hasattr(r, "score") else 0.5,
                "_hit_source": "gin_search",
            }
            for r in rows
        ]

    def _trgm_search(self, query_text: str, scope: Any) -> list[dict]:
        """受限 pg_trgm 补召回。仅 PostgreSQL 下生效。"""
        if self.db.bind.dialect.name != "postgresql":
            return []

        import sqlalchemy as sa

        threshold = getattr(
            self.config, "retrieval_filename_trgm_similarity_threshold", 0.25
        )
        limit = getattr(
            self.config, "retrieval_filename_trgm_candidate_limit", 20
        )

        from app.modules.retrieval.search_profile import _normalize_text

        normalized = _normalize_text(query_text)
        if not normalized:
            return []
        # `%` 是 pg_trgm 可索引谓词；先由它收窄候选，再按 similarity 排序。
        self.db.execute(
            sa.text("SELECT set_config('pg_trgm.similarity_threshold', :threshold, true)"),
            {"threshold": str(threshold)},
        )
        similarity = sa.func.similarity(DocumentSearchProfile.normalized_filename, normalized)

        rows = (
            self.db.query(
                DocumentSearchProfile.working_copy_id,
                DocumentSearchProfile.document_id,
                DocumentSearchProfile.document_version_id,
                similarity.label("score"),
            )
            .filter(
                DocumentSearchProfile.user_id == self.user_id,
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                DocumentSearchProfile.normalized_filename.op("%")(normalized),
                similarity >= threshold,
                *self._scope_predicates(scope),
            )
            .order_by(sa.desc("score"))
            .limit(limit)
            .all()
        )
        return [
            {
                "working_copy_id": r.working_copy_id,
                "document_id": r.document_id,
                "document_version_id": r.document_version_id,
                "_score": float(r.score) if hasattr(r, "score") else 0.3,
                "_hit_source": "trgm_fallback",
            }
            for r in rows
        ]

    def _deterministic_token_match(self, terms: list[str], scope: Any) -> list[dict]:
        """SQLite 下在 combined_search_text 中匹配词项。"""
        rows = (
            self.db.query(DocumentSearchProfile)
            .filter(
                DocumentSearchProfile.user_id == self.user_id,
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                *self._scope_predicates(scope),
            )
            .all()
        )

        results = []
        for r in rows:
            search_text = (r.combined_search_text or "").lower()
            score = 0.0
            for t in terms:
                if t.lower() in search_text:
                    score += 1.0
            if score > 0:
                results.append(
                    {
                        "working_copy_id": r.working_copy_id,
                        "document_id": r.document_id,
                        "document_version_id": r.document_version_id,
                        "_score": score / len(terms),
                        "_hit_source": "deterministic",
                    }
                )
        return results

    def _recall_deterministic(
        self, parsed_query: Any, scope: Any
    ) -> list[dict]:
        """SQLite deterministic 降级：完全在应用层匹配。"""
        query_text = parsed_query.cleaned if hasattr(parsed_query, "cleaned") else ""
        if not query_text:
            return []

        terms = self._get_terms(query_text)
        if not terms:
            return []

        return self._deterministic_token_match(terms, scope)

    def _get_terms(self, text: str) -> list[str]:
        """从查询文本提取分词词项。"""
        if self.tokenizer and hasattr(self.tokenizer, "tokenize"):
            try:
                return self.tokenizer.tokenize(text)
            except Exception:
                pass
        # 简单的 fallback 分词
        import re

        terms = re.findall(
            r"[a-z0-9][a-z0-9._-]*|[\u4e00-\u9fff]+", text.lower()
        )
        # 生成 2-4 字中文子串
        result = set(terms)
        for t in terms:
            if re.fullmatch(r"[\u4e00-\u9fff]+", t) and len(t) > 2:
                for size in range(2, min(5, len(t) + 1)):
                    for start in range(len(t) - size + 1):
                        result.add(t[start : start + size])
        return list(result)[:64]

    def _enrich(self, candidates: list[dict], *, scope: Any) -> list[dict]:
        """候选收敛后以一次批量 JOIN 补齐显示字段。

        禁止逐文件 N+1 查询。
        """
        if not candidates:
            return []

        wc_ids = [c["working_copy_id"] for c in candidates if c.get("working_copy_id")]
        if not wc_ids:
            return []

        rows = (
            self.db.query(
                WorkingCopy,
                Document,
                DocumentSummary,
                DocumentCategorySuggestion,
            )
            .join(Document, Document.id == WorkingCopy.document_id)
            .outerjoin(
                DocumentSummary,
                (DocumentSummary.document_id == WorkingCopy.document_id)
                & (
                    DocumentSummary.document_version_id
                    == WorkingCopy.current_version_id
                )
                # 失败或处理中摘要不能成为搜索结果的展示事实。
                & (DocumentSummary.status == "COMPLETED"),
            )
            .outerjoin(
                DocumentCategorySuggestion,
                (DocumentCategorySuggestion.document_id == WorkingCopy.document_id)
                & (
                    DocumentCategorySuggestion.document_version_id
                    == WorkingCopy.current_version_id
                )
                # 仅展示仍有效的分类建议，避免 REJECTED 历史记录污染结果卡。
                & (
                    DocumentCategorySuggestion.status.in_(
                        ["SUGGESTED", "AUTO_APPLIED", "CONFIRMED"]
                    )
                ),
            )
            .filter(
                WorkingCopy.id.in_(wc_ids),
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                Document.user_id == self.user_id,
            )
            .all()
        )

        # 按 working_copy_id 聚合
        enrich_map: dict[str, dict] = {}
        for wc, doc, summary, sug in rows:
            if wc.id not in enrich_map:
                enrich_map[wc.id] = {
                    "working_copy_id": wc.id,
                    "document_id": doc.id,
                    "document_version_id": wc.current_version_id or "",
                    "filename": wc.filename,
                    "category_path": [],
                    "summary": "",
                    "year": None,
                }
            if sug and sug.category_path_json:
                enrich_map[wc.id]["category_path"] = sug.category_path_json
            if summary:
                enrich_map[wc.id]["summary"] = (
                    summary.summary_text or ""
                )
                if summary.summary_json and isinstance(summary.summary_json, dict):
                    enrich_map[wc.id]["year"] = summary.summary_json.get("year")

        # 合并分数和信息来源
        score_map = {c["working_copy_id"]: c for c in candidates}
        result = []
        for wc_id, data in enrich_map.items():
            score_info = score_map.get(wc_id, {})
            # 最终事实校验：陈旧投影不能让旧内容版本或不在严格范围内的文件泄漏到结果。
            if data["document_version_id"] != score_info.get("document_version_id"):
                continue
            if getattr(scope, "scope_mode", "global") == "strict" and (
                data["document_id"] not in set(getattr(scope, "strict_document_ids", ()) or ())
            ):
                continue
            data["_score"] = score_info.get("_score", 0.0)
            data["_hit_source"] = score_info.get("_hit_source", "")
            data["_scope_weight"] = self._scope_weight(data["document_id"], scope)
            result.append(data)

        result.sort(key=lambda x: -x.get("_score", 0.0))
        return result

    @staticmethod
    def _scope_weight(document_id: str, scope: Any) -> float:
        """将 L0/L1/L4 范围转换为确定性排序权重，不改变全局召回集合。"""
        if document_id in set(getattr(scope, "strict_document_ids", ()) or ()):
            return 1.0
        if document_id in set(getattr(scope, "conversation_document_ids", ()) or ()):
            return 0.7
        return 0.4


class _DefaultConfig:
    """默认配置，当未传入 config 时使用。"""

    retrieval_document_candidate_limit: int = 30
    retrieval_document_detail_limit: int = 12
    retrieval_chunk_limit_per_document: int = 3
    retrieval_chunk_global_limit: int = 24
    retrieval_query_max_chars: int = 500
    retrieval_preview_max_chars: int = 240
    retrieval_filename_trgm_min_chars: int = 4
    retrieval_filename_trgm_candidate_limit: int = 20
    retrieval_filename_trgm_similarity_threshold: float = 0.25
