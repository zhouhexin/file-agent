"""受管原始文件的只读检索分支。

本模块只查询已完成 ``SOURCE_ANALYSIS`` 的当前原始文件修订。它不读取真实文件、
不创建工作副本，也不把原始目录路径交给调用方；工作副本是否已覆盖同一修订由
数据库来源关系确定，避免同一文件在双范围检索结果中重复出现。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session

from app.db.models import (
    ManagedFile,
    ManagedFileRevision,
    ManagedFileSearchProfile,
    ManagedFileTextChunk,
    ManagedRoot,
    WorkingCopy,
)
from app.modules.retrieval.search_profile import _normalize_text


@dataclass(frozen=True)
class ManagedSourceSearchResult:
    """源侧候选的内部排序载体，最终输出不包含真实原始文件路径。"""

    payload: dict[str, Any]
    score: float


class ManagedSourceSearchService:
    """以源侧文件级资料和正文分块执行只读的词法召回。"""

    def __init__(self, *, db: Session, workspace_id: str, tokenizer: Any) -> None:
        """保存已校验的共享工作区与 CPU 分词器。"""

        self.db = db
        self.workspace_id = workspace_id
        self.tokenizer = tokenizer

    def search(
        self,
        *,
        parsed_query: Any,
        scope: Any,
        limit: int = 30,
        exact_phrase: str | None = None,
        unbounded_candidates: bool = False,
    ) -> dict[str, Any]:
        """返回未被活动工作副本覆盖的当前源文件结果。

        严格附件范围只允许读取已确认的工作副本 Document，不能将其扩张成原始目录
        全局扫描；因此严格范围下源侧分支为空。
        """

        if str(getattr(scope, "scope_mode", "global") or "global") == "strict":
            return {"results": [], "pending_count": 0, "failed_count": 0, "eligible_count": 0}
        cleaned = str(getattr(parsed_query, "cleaned", "") or "").strip()
        if not cleaned:
            return {"results": [], "pending_count": 0, "failed_count": 0, "eligible_count": 0}
        terms = list(dict.fromkeys(self.tokenizer.tokenize(cleaned)))[:64]
        if not terms:
            return {"results": [], "pending_count": 0, "failed_count": 0, "eligible_count": 0}

        safe_limit = (
            None
            if unbounded_candidates
            else max(1, min(int(limit), 100))
        )
        # 受管原始目录属于唯一共享文件域；新目录在首次命中前没有
        # ``WorkingCopyRoot`` 是正常状态，因此这里绝不能依赖工作副本根映射。
        # root_key/相对路径仅作为逻辑说明，绝不返回 container_path。
        base = (
            self.db.query(
                ManagedFileRevision,
                ManagedFile,
                ManagedRoot,
                ManagedFileSearchProfile,
            )
            .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
            .join(ManagedRoot, ManagedRoot.id == ManagedFile.root_id)
            .outerjoin(
                ManagedFileSearchProfile,
                ManagedFileSearchProfile.managed_file_revision_id == ManagedFileRevision.id,
            )
            .filter(
                ManagedFile.status == "ACTIVE",
                ManagedRoot.enabled.is_(True),
                ManagedFileRevision.is_current.is_(True),
            )
        )
        # PostgreSQL 先用源侧 FTS/GIN 将候选收敛，再读取有限 Chunk；不能在大目录
        # 中把全部摘要和正文块拉回 Python。SQLite 测试保持确定性内存降级。
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            ts_query = func.websearch_to_tsquery("simple", " OR ".join(terms))
            normalized_filename = _normalize_text(cleaned)
            # 不能只用文件级摘要投影筛选候选：用户经常只记得正文或 Excel
            # 单元格中的词。Chunk 已有独立 GIN 索引，使用相关子查询可让正文
            # 命中进入有限候选集，同时不把全部正文拉回应用进程扫描。
            source_chunk_match = exists().where(
                ManagedFileTextChunk.managed_file_revision_id == ManagedFileRevision.id,
                ManagedFileTextChunk.search_vector.op("@@")(ts_query),
            )
            base = base.filter(
                or_(
                    ManagedFileSearchProfile.search_vector.op("@@")(ts_query),
                    ManagedFileSearchProfile.normalized_filename == normalized_filename,
                    source_chunk_match,
                )
            )
            if safe_limit is not None:
                base = base.limit(max(safe_limit * 3, 50))
        rows = base.all()
        # 同一查询最多保留有限候选，但不能对每一份候选再单独查询工作副本或正文
        # Chunk；首次访问受管目录时这会放大为 N+1 数据库访问，反而掩盖掉“无需
        # 复制即可回答”的性能收益。
        managed_file_ids = [str(managed_file.id) for _revision, managed_file, _root, _profile in rows]
        covered_pairs = {
            (str(managed_file_id), str(content_sha256 or ""))
            for managed_file_id, content_sha256 in self.db.query(
                WorkingCopy.managed_file_id,
                WorkingCopy.imported_source_sha256,
            )
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.managed_file_id.in_(managed_file_ids or ["__none__"]),
            )
            .all()
        }
        ready_revision_ids = [
            str(revision.id)
            for revision, _managed_file, _root, profile in rows
            if revision.status == "READY" and profile is not None and profile.status == "ACTIVE"
        ]
        chunks_by_revision: dict[str, list[ManagedFileTextChunk]] = defaultdict(list)
        if ready_revision_ids:
            for chunk in (
                self.db.query(ManagedFileTextChunk)
                .filter(ManagedFileTextChunk.managed_file_revision_id.in_(ready_revision_ids))
                .order_by(
                    ManagedFileTextChunk.managed_file_revision_id.asc(),
                    ManagedFileTextChunk.chunk_index.asc(),
                )
                .all()
            ):
                chunks_by_revision[str(chunk.managed_file_revision_id)].append(chunk)
        pending_count = sum(1 for revision, *_ in rows if revision.status not in {"READY", "FAILED", "STALE"})
        failed_count = sum(1 for revision, *_ in rows if revision.status == "FAILED")
        results: list[ManagedSourceSearchResult] = []
        normalized_query = _normalize_text(cleaned)
        for revision, managed_file, root, profile in rows:
            if revision.status != "READY" or profile is None or profile.status != "ACTIVE":
                continue
            if (str(managed_file.id), str(revision.content_sha256 or "")) in covered_pairs:
                continue
            score, body_hit, preview, location = self._score_revision(
                revision_id=revision.id,
                profile=profile,
                filename=managed_file.filename,
                relative_path=managed_file.relative_path,
                terms=terms,
                normalized_query=normalized_query,
                chunks=chunks_by_revision.get(str(revision.id), []),
                exact_phrase=exact_phrase,
            )
            if score <= 0:
                continue
            reasons = self._match_reasons(
                filename=managed_file.filename,
                profile=profile,
                body_hit=body_hit,
                normalized_query=normalized_query,
            )
            results.append(
                ManagedSourceSearchResult(
                    score=score,
                    payload={
                        "resource_type": "MANAGED_SOURCE",
                        "working_copy_id": None,
                        # 逻辑 DocumentVersion 只用于受控后续证据读取，前端不可把它当下载地址。
                        "document_id": revision.analysis_document_id,
                        "document_version_id": revision.analysis_document_version_id,
                        "can_open": False,
                        "availability_message": "已从受管原始文件检索到，工作副本正在后台生成",
                        "managed_file_id": managed_file.id,
                        "managed_file_revision_id": revision.id,
                        "filename": managed_file.filename,
                        "root_key": root.root_key,
                        "relative_path": managed_file.relative_path,
                        "category_path": [],
                        "year": list(profile.years_json or []),
                        "overview": str(profile.summary or "")[:500],
                        "match_reasons": reasons,
                        "match_location": location,
                        "evidence_preview": preview,
                        "relevance_tier": (
                            "RELATED"
                            if normalized_query == _normalize_text(managed_file.filename)
                            else "POSSIBLE"
                        ),
                        "_body_phrase_hit": body_hit,
                        "_score": score,
                    },
                )
            )
        results.sort(key=lambda item: (-item.score, str(item.payload.get("managed_file_revision_id") or "")))
        bounded_results = results if safe_limit is None else results[:safe_limit]
        return {
            "results": [item.payload for item in bounded_results],
            "pending_count": pending_count,
            "failed_count": failed_count,
            "eligible_count": len(rows),
        }

    def _score_revision(
        self,
        *,
        revision_id: str,
        profile: ManagedFileSearchProfile,
        filename: str,
        relative_path: str,
        terms: list[str],
        normalized_query: str,
        chunks: list[ManagedFileTextChunk],
        exact_phrase: str | None,
    ) -> tuple[float, bool, str, dict[str, Any] | None]:
        """在已分析修订的有限资料内计算确定性相关度与可引用正文预览。"""

        metadata = " ".join(
            [
                str(profile.search_text or ""),
                str(profile.summary or ""),
                str(filename or ""),
                str(relative_path or ""),
                " ".join(str(value) for value in (profile.keywords_json or [])),
                " ".join(str(value) for value in (profile.entities_json or [])),
            ]
        ).casefold()
        matched_metadata = sum(1 for term in terms if term.casefold() in metadata)
        exact_filename = bool(normalized_query and normalized_query == _normalize_text(filename))
        best_chunk: ManagedFileTextChunk | None = None
        exact_body_chunk: ManagedFileTextChunk | None = None
        best_chunk_matches = 0
        normalized_exact_phrase = str(exact_phrase or "").casefold().strip()
        for chunk in chunks:
            text = f"{chunk.search_text} {chunk.text_content}".casefold()
            matches = sum(1 for term in terms if term.casefold() in text)
            if matches > best_chunk_matches:
                best_chunk, best_chunk_matches = chunk, matches
            # 受控短语策略要求连续正文证据；不能因为同一 Chunk 中散落了多个
            # 词就把它标成短语命中。文件名精确匹配不在这里替代正文证据。
            if (
                normalized_exact_phrase
                and normalized_exact_phrase in str(chunk.text_content or "").casefold()
                and exact_body_chunk is None
            ):
                exact_body_chunk = chunk
        if exact_body_chunk is not None:
            best_chunk = exact_body_chunk
        score = (matched_metadata / max(1, len(terms))) * 0.45
        score += (best_chunk_matches / max(1, len(terms))) * 0.55
        if exact_body_chunk is not None:
            score = max(score, 0.55)
        if exact_filename:
            score += 1.0
        if not best_chunk and not matched_metadata:
            return 0.0, False, "", None
        preview = str(best_chunk.text_content or "")[:240] if best_chunk else ""
        location = (
            {
                "page_number": best_chunk.page_number,
                "sheet_name": best_chunk.sheet_name,
                "cell_range": best_chunk.cell_range,
            }
            if best_chunk is not None
            else None
        )
        return (
            score,
            bool(exact_body_chunk) if normalized_exact_phrase else bool(best_chunk_matches),
            preview,
            location,
        )

    @staticmethod
    def _match_reasons(
        *,
        filename: str,
        profile: ManagedFileSearchProfile,
        body_hit: bool,
        normalized_query: str,
    ) -> list[str]:
        """生成和工作副本结果一致的可读原因，不透露源目录绝对路径。"""

        reasons: list[str] = []
        if normalized_query and normalized_query == _normalize_text(filename):
            reasons.append("文件名精确匹配查询")
        if profile.summary:
            reasons.append("摘要或关键词命中查询")
        if body_hit:
            reasons.append("原文或表格单元格命中查询")
        return reasons or ["命中相关原始文件资料"]
