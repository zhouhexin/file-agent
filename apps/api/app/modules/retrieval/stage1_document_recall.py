"""第一阶段数据库索引召回。

把"加载所有候选到 Python 内存再遍历评分"改为"PostgreSQL 索引查询"。
召回顺序：
1. normalized_filename 精确匹配（B-tree 索引）
2. normalized_filename 连续短语匹配（pg_trgm GIN 索引）
3. Jieba/GIN search_vector 主召回（setweight: A=文件名, B=分类, C=元数据, D=摘要）
4. 受限 pg_trgm 补召回（仅当查询 ≥ 配置最小长度、精确+GIN 不足时启用）

候选收敛后以有界批量查询补齐显示字段，不执行一对多联合 JOIN 或逐文件 N+1 读取。
SQLite 下使用 deterministic token 覆盖降级。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import tuple_

from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentChunk,
    DocumentIndexRun,
    DocumentSearchProfile,
    DocumentSummary,
    ManagedFile,
    ManagedRoot,
    WorkingCopy,
)
from app.modules.retrieval.query_parser import exact_short_chinese_phrase


class Stage1DocumentRecallService:
    """第一阶段数据库索引召回。

    不直接访问文件系统，不修改任何数据。
    共享工作目录以 workspace_id + ACTIVE 状态作为可见范围；user_id 只保留为
    导入审计信息，不能把同一共享目录再次切成按创建者隔离的检索集合。
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

    def recall(
        self,
        *,
        parsed_query: Any,
        scope: Any,
        unbounded_candidates: bool = False,
    ) -> list[dict]:
        """执行第一阶段召回，返回候选列表。

        返回的结构化候选包含 working_copy_id、document_version_id、score、hit_source。
        候选收敛后调用 enrich() 补齐显示字段。普通检索继续使用配置上限；只有
        上层明确执行“两个完整条件先分别召回、再取交集”时才允许无界候选，避免
        在取交集前丢掉排名靠后的真实交集文件。
        """
        if self.db.bind.dialect.name == "postgresql":
            candidates = self._recall_postgresql(
                parsed_query,
                scope,
                unbounded_candidates=unbounded_candidates,
            )
        else:
            candidates = self._recall_deterministic(
                parsed_query,
                scope,
                unbounded_candidates=unbounded_candidates,
            )

        # 去重；普通检索随后应用上限，交集专用召回保留完整候选集合。
        seen = {}
        for c in candidates:
            wc_id = c.get("working_copy_id")
            if wc_id and wc_id not in seen:
                seen[wc_id] = c
        result = list(seen.values())
        result.sort(key=lambda x: -x.get("_score", 0.0))
        if not unbounded_candidates:
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

    def enrich_working_copy_ids(
        self,
        *,
        working_copy_ids: list[str] | tuple[str, ...],
        scope: Any,
    ) -> list[dict]:
        """有界补齐已由后端确认的活动工作副本显示字段。

        该入口不执行关键词召回，只供事件集合等确定性关系扩展复用现有批量富化，
        避免重新实现权限、当前版本和逻辑路径校验。
        """

        safe_ids = list(
            dict.fromkeys(str(value) for value in working_copy_ids if str(value))
        )[:100]
        if not safe_ids:
            return []
        rows = (
            self.db.query(
                WorkingCopy.id,
                WorkingCopy.document_id,
                WorkingCopy.current_version_id,
            )
            .filter(
                WorkingCopy.id.in_(safe_ids),
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
            )
            .all()
        )
        candidates = [
            {
                "working_copy_id": row.id,
                "document_id": row.document_id,
                "document_version_id": row.current_version_id,
                "_score": 0.0,
                "_hit_source": "verified_event_directory",
            }
            for row in rows
            if row.current_version_id
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

    def _working_copy_scope_predicates(self, scope: Any) -> list[Any]:
        """为直接关联工作副本的 Chunk 召回生成同一严格范围谓词。"""

        if getattr(scope, "scope_mode", "global") != "strict":
            return []
        document_ids = list(getattr(scope, "strict_document_ids", ()) or ())
        if not document_ids:
            import sqlalchemy as sa

            return [sa.false()]
        return [WorkingCopy.document_id.in_(document_ids)]

    def _recall_postgresql(
        self,
        parsed_query: Any,
        scope: Any,
        *,
        unbounded_candidates: bool = False,
    ) -> list[dict]:
        """PostgreSQL 索引召回。

        召回顺序：
        1. normalized_filename 精确匹配
        2. normalized_filename 连续短语匹配
        3. search_vector GIN 主召回
        4. pg_trgm 补召回（若不足）
        """
        query_text = parsed_query.cleaned if hasattr(parsed_query, "cleaned") else ""
        if not query_text:
            return []
        short_phrase = exact_short_chinese_phrase(query_text)
        if short_phrase:
            # 短人名和短业务实体必须连续匹配，不能退化为单字 OR 或文件名模糊匹配。
            return self._exact_short_phrase_match(
                short_phrase,
                parsed_query,
                scope,
                unbounded_candidates=unbounded_candidates,
            )

        candidates = []
        seen_wc_ids: set[str] = set()

        # 1. 精确文件名匹配
        exact = self._exact_filename_match(query_text, scope)
        for c in exact:
            wc_id = c.get("working_copy_id")
            if wc_id and wc_id not in seen_wc_ids:
                seen_wc_ids.add(wc_id)
                candidates.append(c)

        # 2. 文件名连续短语必须先于宽泛 GIN 候选进入结果，避免明确目标被候选上限挤掉。
        filename_phrase = self._filename_phrase_match(
            query_text,
            parsed_query,
            scope,
            unbounded_candidates=unbounded_candidates,
        )
        for c in filename_phrase:
            wc_id = c.get("working_copy_id")
            if wc_id and wc_id not in seen_wc_ids:
                seen_wc_ids.add(wc_id)
                candidates.append(c)

        # 3. GIN 主召回
        gin = self._gin_search(
            query_text,
            scope,
            unbounded_candidates=unbounded_candidates,
        )
        for c in gin:
            wc_id = c.get("working_copy_id")
            if wc_id and wc_id not in seen_wc_ids:
                seen_wc_ids.add(wc_id)
                candidates.append(c)

        # 4. 如果候选不足且查询达最小长度，启用 pg_trgm 补召回
        trgm_min = getattr(self.config, "retrieval_filename_trgm_min_chars", 4)
        candidate_limit = getattr(self.config, "retrieval_document_candidate_limit", 30)
        if (
            (unbounded_candidates or len(candidates) < candidate_limit)
            and len(query_text) >= trgm_min
        ):
            trgm = self._trgm_search(
                query_text,
                scope,
                unbounded_candidates=unbounded_candidates,
            )
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
                "_score": 3.0,
                "_hit_source": "exact_filename",
            }
            for r in rows
        ]

    def _filename_phrase_match(
        self,
        query_text: str,
        parsed_query: Any,
        scope: Any,
        *,
        unbounded_candidates: bool = False,
    ) -> list[dict]:
        """用文件名 pg_trgm 索引连续匹配完整短语，并优先保留显式年份命中。

        该分支只读取活动瘦投影。它不把短语拆成 OR，也不替代后续正文精查；
        作用是防止文件名已经明确包含机构或主题的文件被宽泛 GIN 候选上限挤掉。
        """

        import sqlalchemy as sa

        from app.modules.retrieval.search_profile import _normalize_text

        normalized = _normalize_text(query_text)
        if not normalized:
            return []
        phrase_match = DocumentSearchProfile.normalized_filename.contains(normalized)
        year = getattr(parsed_query, "year", None)
        year_match = (
            DocumentSearchProfile.normalized_filename.contains(str(year))
            if year is not None
            else None
        )
        score = (
            sa.case((year_match, 2.2), else_=2.1)
            if year_match is not None
            else sa.literal(2.1)
        )
        # PostgreSQL 会把 ``ORDER BY 2.1`` 解释为列序号并拒绝非整数常量。
        # 无显式年份时所有命中分数相同，只需稳定按文件名排序。
        ordering = [DocumentSearchProfile.normalized_filename.asc()]
        if year_match is not None:
            ordering.insert(0, score.desc())
        query = (
            self.db.query(
                DocumentSearchProfile.working_copy_id,
                DocumentSearchProfile.document_id,
                DocumentSearchProfile.document_version_id,
                score.label("score"),
            )
            .filter(
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                phrase_match,
                *self._scope_predicates(scope),
            )
            .order_by(*ordering)
        )
        if not unbounded_candidates:
            query = query.limit(self.config.retrieval_document_candidate_limit)
        rows = query.all()
        return [
            {
                "working_copy_id": row.working_copy_id,
                "document_id": row.document_id,
                "document_version_id": row.document_version_id,
                "_score": float(row.score),
                "_hit_source": "filename_phrase",
            }
            for row in rows
        ]

    def _exact_short_phrase_match(
        self,
        phrase: str,
        parsed_query: Any,
        scope: Any,
        *,
        unbounded_candidates: bool = False,
    ) -> list[dict]:
        """在有索引的文件名和瘦投影中连续匹配短中文实体。

        显式年份只调整候选优先级，不排除正文中有年份但文件名无年份的有效结果。
        """

        import sqlalchemy as sa

        from app.modules.retrieval.search_profile import _normalize_text

        normalized = _normalize_text(phrase)
        if not normalized:
            return []
        filename_match = DocumentSearchProfile.normalized_filename.contains(normalized)
        year = getattr(parsed_query, "year", None)
        year_match = (
            DocumentSearchProfile.normalized_filename.contains(str(year))
            if year is not None
            else None
        )
        score = (
            sa.case(
                (sa.and_(filename_match, year_match), 2.2),
                (filename_match, 2.1),
                else_=1.0,
            )
            if year_match is not None
            else sa.case((filename_match, 2.1), else_=1.0)
        )
        ordering = [score.desc(), DocumentSearchProfile.normalized_filename.asc()]
        profile_query = (
            self.db.query(
                DocumentSearchProfile.working_copy_id,
                DocumentSearchProfile.document_id,
                DocumentSearchProfile.document_version_id,
                score.label("score"),
            )
            .filter(
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                sa.or_(
                    filename_match,
                    # combined_search_text 有 pg_trgm GIN；直接匹配连续汉字可同时兼容
                    # Jieba 词项和 deterministic fallback 保存的完整中文片段。
                    DocumentSearchProfile.combined_search_text.contains(phrase),
                ),
                *self._scope_predicates(scope),
            )
            .order_by(*ordering)
        )
        if not unbounded_candidates:
            profile_query = profile_query.limit(
                self.config.retrieval_document_candidate_limit
            )
        profile_rows = profile_query.all()
        results = [
            {
                "working_copy_id": row.working_copy_id,
                "document_id": row.document_id,
                "document_version_id": row.document_version_id,
                "_score": float(row.score),
                "_hit_source": "exact_short_phrase",
            }
            for row in profile_rows
        ]
        seen_working_copy_ids = {
            str(item.get("working_copy_id") or "") for item in results
        }
        # 历史瘦投影可能只含文件名和摘要，而正文 Chunk 已经完整建立。短人名
        # 必须同时从带 pg_trgm 索引的 Chunk 连续召回，否则“正文有该人员”仍会
        # 被第一阶段挡掉，第二阶段没有机会验证证据。
        chunk_query = (
            self.db.query(
                WorkingCopy.id.label("working_copy_id"),
                WorkingCopy.document_id,
                WorkingCopy.current_version_id.label("document_version_id"),
            )
            .join(
                DocumentChunk,
                DocumentChunk.document_version_id == WorkingCopy.current_version_id,
            )
            .join(
                DocumentIndexRun,
                DocumentIndexRun.id == DocumentChunk.index_run_id,
            )
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                DocumentIndexRun.status == "COMPLETED",
                DocumentChunk.search_text.contains(phrase),
                *self._working_copy_scope_predicates(scope),
            )
            .group_by(
                WorkingCopy.id,
                WorkingCopy.document_id,
                WorkingCopy.current_version_id,
            )
        )
        if not unbounded_candidates:
            chunk_query = chunk_query.limit(
                self.config.retrieval_document_candidate_limit
            )
        chunk_rows = chunk_query.all()
        for row in chunk_rows:
            if str(row.working_copy_id) in seen_working_copy_ids:
                continue
            results.append(
                {
                    "working_copy_id": row.working_copy_id,
                    "document_id": row.document_id,
                    "document_version_id": row.document_version_id,
                    "_score": 1.4,
                    "_hit_source": "exact_short_phrase_chunk",
                }
            )
        return results

    def _gin_search(
        self,
        query_text: str,
        scope: Any,
        *,
        unbounded_candidates: bool = False,
    ) -> list[dict]:
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

        query = (
            self.db.query(
                DocumentSearchProfile.working_copy_id,
                DocumentSearchProfile.document_id,
                DocumentSearchProfile.document_version_id,
                sa.func.ts_rank_cd(
                    DocumentSearchProfile.search_vector, ts_query
                ).label("score"),
            )
            .filter(
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                DocumentSearchProfile.search_vector.op("@@")(ts_query),
                *self._scope_predicates(scope),
            )
            .order_by(sa.desc("score"))
        )
        if not unbounded_candidates:
            query = query.limit(self.config.retrieval_document_candidate_limit)
        rows = query.all()
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

    def _trgm_search(
        self,
        query_text: str,
        scope: Any,
        *,
        unbounded_candidates: bool = False,
    ) -> list[dict]:
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

        query = (
            self.db.query(
                DocumentSearchProfile.working_copy_id,
                DocumentSearchProfile.document_id,
                DocumentSearchProfile.document_version_id,
                similarity.label("score"),
            )
            .filter(
                DocumentSearchProfile.workspace_id == self.workspace_id,
                DocumentSearchProfile.status == "ACTIVE",
                DocumentSearchProfile.normalized_filename.op("%")(normalized),
                similarity >= threshold,
                *self._scope_predicates(scope),
            )
            .order_by(sa.desc("score"))
        )
        if not unbounded_candidates:
            query = query.limit(limit)
        rows = query.all()
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
        self,
        parsed_query: Any,
        scope: Any,
        *,
        unbounded_candidates: bool = False,
    ) -> list[dict]:
        """SQLite deterministic 降级：完全在应用层匹配。"""
        query_text = parsed_query.cleaned if hasattr(parsed_query, "cleaned") else ""
        if not query_text:
            return []
        short_phrase = exact_short_chinese_phrase(query_text)
        if short_phrase:
            return self._exact_short_phrase_match(
                short_phrase,
                parsed_query,
                scope,
                unbounded_candidates=unbounded_candidates,
            )

        terms = self._get_terms(query_text)
        if not terms:
            return []

        # SQLite 测试与降级路径保持同一优先级语义，确保文件名完整短语不会被
        # 大量只在摘要中命中拆分词项的候选挤出。
        return [
            *self._filename_phrase_match(query_text, parsed_query, scope),
            *self._deterministic_token_match(terms, scope),
        ]

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
        """候选收敛后以有界批量查询补齐显示字段。

        禁止一对多联合 JOIN 和逐文件 N+1 查询。
        """
        if not candidates:
            return []

        wc_ids = [c["working_copy_id"] for c in candidates if c.get("working_copy_id")]
        if not wc_ids:
            return []

        # 显示字段分三次有界批量读取。不能把多条摘要和多条分类建议同时
        # LEFT JOIN，否则两组一对多记录会相乘，候选很少也可能超过 SQL 超时。
        # 受管根和源文件都是一对一关系，可在基础查询中读取安全逻辑位置；
        # 绝不向检索结果透传容器绝对路径。
        base_rows = (
            self.db.query(WorkingCopy, Document, ManagedFile, ManagedRoot)
            .join(Document, Document.id == WorkingCopy.document_id)
            # 逻辑原始路径用于让同名文件可区分；外连接兼容历史工作副本和
            # SQLite 测试数据中尚未补齐 ManagedFile 的记录，绝不因此丢失结果。
            .outerjoin(ManagedFile, ManagedFile.id == WorkingCopy.managed_file_id)
            .outerjoin(ManagedRoot, ManagedRoot.id == ManagedFile.root_id)
            .filter(
                WorkingCopy.id.in_(wc_ids),
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
            )
            .all()
        )
        version_pairs = list(
            dict.fromkeys(
                (str(document.id), str(working_copy.current_version_id))
                for working_copy, document, _managed_file, _managed_root in base_rows
                if working_copy.current_version_id
            )
        )
        summaries = (
            self.db.query(DocumentSummary)
            .filter(
                tuple_(
                    DocumentSummary.document_id,
                    DocumentSummary.document_version_id,
                ).in_(version_pairs),
                DocumentSummary.status == "COMPLETED",
            )
            .order_by(DocumentSummary.updated_at.desc())
            .all()
            if version_pairs
            else []
        )
        suggestions = (
            self.db.query(DocumentCategorySuggestion)
            .filter(
                tuple_(
                    DocumentCategorySuggestion.document_id,
                    DocumentCategorySuggestion.document_version_id,
                ).in_(version_pairs),
                DocumentCategorySuggestion.status.in_(
                    ["SUGGESTED", "AUTO_APPLIED", "CONFIRMED"]
                ),
            )
            .order_by(
                DocumentCategorySuggestion.rank.asc(),
                DocumentCategorySuggestion.updated_at.desc(),
            )
            .all()
            if version_pairs
            else []
        )
        summary_by_version: dict[tuple[str, str], DocumentSummary] = {}
        for summary in summaries:
            summary_by_version.setdefault(
                (str(summary.document_id), str(summary.document_version_id)),
                summary,
            )
        suggestion_by_version: dict[
            tuple[str, str], DocumentCategorySuggestion
        ] = {}
        for suggestion in suggestions:
            suggestion_by_version.setdefault(
                (
                    str(suggestion.document_id),
                    str(suggestion.document_version_id),
                ),
                suggestion,
            )

        # 按 working_copy_id 聚合
        enrich_map: dict[str, dict] = {}
        for wc, doc, managed_file, managed_root in base_rows:
            version_key = (str(doc.id), str(wc.current_version_id or ""))
            summary = summary_by_version.get(version_key)
            sug = suggestion_by_version.get(version_key)
            enrich_map[wc.id] = {
                "resource_type": "WORKING_COPY",
                "working_copy_id": wc.id,
                "managed_file_id": managed_file.id if managed_file else wc.managed_file_id,
                "document_id": doc.id,
                "document_version_id": wc.current_version_id or "",
                "filename": wc.filename,
                "root_key": managed_root.root_key if managed_root else None,
                # 源文件相对路径是用户可理解的稳定逻辑位置；源记录缺失的历史
                # 工作副本才回退到自身路径，且仍不暴露服务器存储根。
                "relative_path": (
                    managed_file.relative_path if managed_file else wc.relative_path
                ),
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
