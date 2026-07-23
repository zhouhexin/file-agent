"""候选 DocumentVersion 内的 CPU Chunk 词法检索。

本模块只提供阶段四可复用的受控底层能力，不负责会话范围判断或最终回答；调用方必须先给出候选版本 ID。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentChunk, DocumentIndexRun, WorkingCopy
from app.modules.chunks.tokenizer import ChineseLexicalTokenizer, load_default_business_terms


MAX_QUERY_CHARS = 2000
MAX_QUERY_TOKENS = 64
MAX_CANDIDATE_VERSION_IDS = 500
MAX_FALLBACK_VERSION_AGGREGATIONS = 10


class DocumentChunkLexicalSearchService:
    """在当前用户和明确候选版本边界内检索 Chunk，禁止隐式扩大到全库。"""

    def __init__(
        self,
        *,
        db: Session,
        user_id: str,
        tokenizer: ChineseLexicalTokenizer | None = None,
    ) -> None:
        """保存用户所有权边界和 CPU 分词器。"""

        self.db = db
        self.user_id = user_id
        self.tokenizer = tokenizer or ChineseLexicalTokenizer(load_default_business_terms())

    def search(self, *, query: str, document_version_ids: list[str], limit: int = 20) -> list[dict[str, Any]]:
        """返回不含正文、分词文本和 embedding 的相关 Chunk 定位结果。"""

        # 候选范围和查询词项必须有确定上限，避免异常 Planner 输出放大为超长 IN/tsquery 查询。
        version_ids = list(dict.fromkeys(str(item) for item in document_version_ids if str(item)))[
            :MAX_CANDIDATE_VERSION_IDS
        ]
        tokens = self.tokenizer.tokenize(str(query or "")[:MAX_QUERY_CHARS])[:MAX_QUERY_TOKENS]
        if not version_ids or not tokens:
            return []
        safe_limit = max(1, min(int(limit), 100))
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            return self._search_postgresql(tokens=tokens, version_ids=version_ids, limit=safe_limit)
        return self._search_deterministic(tokens=tokens, version_ids=version_ids, limit=safe_limit)

    def _base_query(self):
        """构造所有权和成功索引运行过滤，任何搜索分支都不能绕过。"""

        return (
            self.db.query(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .join(DocumentIndexRun, DocumentIndexRun.id == DocumentChunk.index_run_id)
            .filter(Document.user_id == self.user_id, DocumentIndexRun.status == "COMPLETED")
        )

    def _search_postgresql(self, *, tokens: list[str], version_ids: list[str], limit: int) -> list[dict[str, Any]]:
        """使用 ``simple`` tsquery 和 pg_trgm 排序，中文分词已在应用层完成。"""

        # websearch_to_tsquery 接收绑定参数并解析 OR，不允许用户直接注入 PostgreSQL tsquery 语法。
        query_text = " OR ".join(dict.fromkeys(tokens))
        ts_query = func.websearch_to_tsquery("simple", query_text)
        rank = func.ts_rank_cd(DocumentChunk.search_vector, ts_query)
        trigram_score = func.similarity(DocumentChunk.search_text, " ".join(tokens))
        rows = (
            self._base_query()
            .filter(DocumentChunk.document_version_id.in_(version_ids))
            .filter(
                or_(
                    DocumentChunk.search_vector.op("@@")(ts_query),
                    trigram_score >= 0.1,
                )
            )
            .with_entities(DocumentChunk, rank.label("fts_rank"), trigram_score.label("trigram_score"))
            .order_by(rank.desc(), trigram_score.desc(), DocumentChunk.chunk_index.asc())
            .limit(limit)
            .all()
        )
        return [
            _safe_result(chunk, score=float(fts_rank or 0) + float(trigram or 0) * 0.25)
            for chunk, fts_rank, trigram in rows
        ]

    def _search_deterministic(self, *, tokens: list[str], version_ids: list[str], limit: int) -> list[dict[str, Any]]:
        """SQLite 测试和数据库降级环境使用同一词项计算确定性覆盖率。"""

        unique_tokens = list(dict.fromkeys(tokens))
        chunks = self._base_query().filter(DocumentChunk.document_version_id.in_(version_ids)).all()
        ranked: list[tuple[float, DocumentChunk]] = []
        for chunk in chunks:
            indexed = set(str(chunk.search_text or "").split())
            matched = sum(1 for token in unique_tokens if token in indexed or token in chunk.text_content.lower())
            if matched:
                ranked.append((matched / len(unique_tokens), chunk))
        ranked.sort(key=lambda item: (-item[0], item[1].chunk_index, item[1].id))
        return [_safe_result(chunk, score=score) for score, chunk in ranked[:limit]]

    def fallback_recall(
        self,
        *,
        query: str,
        workspace_id: str,
        max_versions: int = MAX_FALLBACK_VERSION_AGGREGATIONS,
        limit_chunks: int = 30,
    ) -> list[dict[str, Any]]:
        """全局 Chunk GIN 候选补召回。

        阶段四第一阶段文档召回候选不足时调用。
        联结当前用户 ACTIVE 工作副本的当前版本，按版本聚合，最多补充 max_versions 个版本。
        只返回 document_version_id、最佳 Chunk ID、位置和分数，不返回正文。
        """
        if not query:
            return []

        tokens = self.tokenizer.tokenize(str(query)[:MAX_QUERY_CHARS])[:MAX_QUERY_TOKENS]
        if not tokens:
            return []

        max_versions = max(1, min(int(max_versions), 50))
        limit_chunks = max(1, min(int(limit_chunks), 100))

        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            return self._fallback_postgresql(
                tokens=tokens, workspace_id=workspace_id,
                max_versions=max_versions, limit_chunks=limit_chunks,
            )
        return self._fallback_deterministic(
            tokens=tokens, workspace_id=workspace_id,
            max_versions=max_versions, limit_chunks=limit_chunks,
        )

    def _fallback_postgresql(
        self, *, tokens: list[str], workspace_id: str,
        max_versions: int, limit_chunks: int,
    ) -> list[dict[str, Any]]:
        """PostgreSQL 全局 Chunk GIN 补召回。"""

        query_text = " OR ".join(dict.fromkeys(tokens))
        ts_query = func.websearch_to_tsquery("simple", query_text)
        rank = func.ts_rank_cd(DocumentChunk.search_vector, ts_query)

        # 子查询：当前工作区下 ACTIVE 工作副本的最新版本
        active_subq = (
            self.db.query(
                WorkingCopy.document_id.label("document_id"),
                WorkingCopy.current_version_id.label("version_id"),
            )
            .filter(
                WorkingCopy.workspace_id == workspace_id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.current_version_id.isnot(None),
            )
            .subquery()
        )

        # 取 top chunk
        ranked_chunks = (
            self.db.query(
                DocumentChunk,
                rank.label("fts_rank"),
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .join(DocumentIndexRun, DocumentIndexRun.id == DocumentChunk.index_run_id)
            .join(
                active_subq,
                (active_subq.c.version_id == DocumentChunk.document_version_id)
                & (active_subq.c.document_id == DocumentChunk.document_id),
            )
            .filter(
                Document.user_id == self.user_id,
                DocumentIndexRun.status == "COMPLETED",
                DocumentChunk.search_vector.op("@@")(ts_query),
            )
            .order_by(rank.desc(), DocumentChunk.chunk_index.asc())
            .limit(limit_chunks)
            .all()
        )

        # 按版本聚合
        version_map: dict[str, dict[str, Any]] = {}
        for chunk, fts_rank in ranked_chunks:
            version_id = chunk.document_version_id
            if version_id in version_map:
                continue
            if len(version_map) >= max_versions:
                break
            version_map[version_id] = _safe_result(
                chunk, score=float(fts_rank or 0.0),
            )

        return list(version_map.values())

    def _fallback_deterministic(
        self, *, tokens: list[str], workspace_id: str,
        max_versions: int, limit_chunks: int,
    ) -> list[dict[str, Any]]:
        """SQLite 全局 deterministic Chunk 补召回。"""

        unique_tokens = list(dict.fromkeys(tokens))

        # 子查询：active working copies
        active_version_ids = [
            row.version_id for row in self.db.query(
                WorkingCopy.current_version_id.label("version_id"),
            )
            .filter(
                WorkingCopy.workspace_id == workspace_id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.current_version_id.isnot(None),
            )
            .all()
        ]

        if not active_version_ids:
            return []

        chunks = (
            self._base_query()
            .filter(DocumentChunk.document_version_id.in_(active_version_ids))
            .all()
        )

        ranked: list[tuple[float, DocumentChunk]] = []
        for chunk in chunks:
            indexed = set(str(chunk.search_text or "").split())
            matched = sum(
                1 for token in unique_tokens
                if token in indexed or token in (chunk.text_content or "").lower()
            )
            if matched:
                ranked.append((matched / len(unique_tokens), chunk))

        ranked.sort(key=lambda item: (-item[0], item[1].chunk_index, item[1].id))

        # 按版本聚合
        version_map: dict[str, dict[str, Any]] = {}
        for score, chunk in ranked[:limit_chunks]:
            version_id = chunk.document_version_id
            if version_id in version_map:
                continue
            if len(version_map) >= max_versions:
                break
            version_map[version_id] = _safe_result(chunk, score=score)

        return list(version_map.values())


def _safe_result(chunk: DocumentChunk, *, score: float) -> dict[str, Any]:
    """生成只含稳定 ID、分数和位置的结果，正文必须由后续 EvidenceValidator 受控读取。"""

    return {
        "chunk_id": chunk.id,
        "document_id": chunk.document_id,
        "document_version_id": chunk.document_version_id,
        "extraction_run_id": chunk.extraction_run_id,
        "score": round(max(0.0, score), 6),
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "sheet_name": chunk.sheet_name,
        "cell_range": chunk.cell_range,
    }
