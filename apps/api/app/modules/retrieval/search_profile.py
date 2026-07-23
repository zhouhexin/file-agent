"""工作副本级瘦检索投影的 upsert、backfill、reconciliation 与失效管理。

本模块负责维护 document_search_profiles 表，该表是两阶段检索第一阶段
（文档级索引召回）的数据基础。投影只保存稳定 ID 和检索必需词项，
不保存完整分类、实体或摘要 JSON。候选收敛后的显示数据以一次批量 JOIN
从事实表读取。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import sqlalchemy as sa

from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentSearchProfile,
    DocumentSummary,
    WorkingCopy,
)


_NORMALIZE_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def _normalize_text(value: str) -> str:
    """统一大小写并移除不参与检索的标点和空白。

        结果用于 normalized_filename 的 B-tree 精确匹配、受限 pg_trgm 补召回和
        source_fingerprint 计算。
    """
    return _NORMALIZE_RE.sub("", str(value).lower())


class DocumentSearchProfileService:
    """管理 document_search_profiles 的创建、更新、失效和重建。

    所有写操作必须幂等，重复调用不产生重复记录或副作用。
    本服务不直接返回搜索候选（由 Stage1DocumentRecallService 负责）。
    """

    def __init__(self, *, db: Any, tokenizer: Any | None = None) -> None:
        """绑定当前数据库会话和可选分词器。

        分词器用于将文件名、分类、元数据和摘要文本转换为稳定词项。
        不传入分词器时，创建空投影（仅基础 ID 和状态字段）。
        """
        self.db = db
        if tokenizer is None:
            # 投影是生产检索的输入，不能因调用方遗漏分词器而把整段中文当成一个词项。
            from app.modules.chunks.tokenizer import (
                ChineseLexicalTokenizer,
                load_default_business_terms,
            )

            tokenizer = ChineseLexicalTokenizer(load_default_business_terms())
        self.tokenizer = tokenizer

    def upsert_current_profile(self, working_copy_id: str) -> dict[str, Any]:
        """为指定工作副本创建或更新检索投影。

        幂等：重复调用不产生重复记录，不产生重复的 source_fingerprint。
        应在同一事务内与工作副本状态变更一起调用。
        """
        wc = self.db.query(WorkingCopy).filter(
            WorkingCopy.id == working_copy_id
        ).first()
        if not wc:
            return {"ok": False, "error": "WORKING_COPY_NOT_FOUND"}

        # 显式查询 Document 获取 user_id（SQLAlchemy 无 backref 需手动 JOIN）
        doc = self.db.query(Document).filter(
            Document.id == wc.document_id
        ).first()

        # 构建投影字段
        normalized_filename = _normalize_text(wc.filename or "")

        # 读取关联的摘要和分类
        summary = self.db.query(DocumentSummary).filter(
            DocumentSummary.document_id == wc.document_id,
            DocumentSummary.document_version_id == wc.current_version_id,
            DocumentSummary.status == "COMPLETED",
        ).first()

        # 读取所有有效分类建议（不只排名第一）
        suggestions = self.db.query(DocumentCategorySuggestion).filter(
            DocumentCategorySuggestion.document_id == wc.document_id,
            DocumentCategorySuggestion.document_version_id == wc.current_version_id,
            DocumentCategorySuggestion.status.in_(
                ["SUGGESTED", "AUTO_APPLIED", "CONFIRMED"]
            ),
        ).order_by(
            DocumentCategorySuggestion.rank.asc()
        ).all()

        # 生成各字段的 search_text（分词词项）
        filename_terms = self._tokenize(wc.filename or "")

        category_terms_list = []
        for sug in suggestions:
            if sug.category_path_json:
                category_terms_list.append(
                    " ".join(str(p) for p in sug.category_path_json)
                )
        category_terms = self._tokenize(" ".join(category_terms_list))

        metadata_terms = self._tokenize(
            self._build_metadata_text(summary, suggestions)
        )

        summary_terms = self._tokenize(
            summary.summary_text if summary else ""
        )

        # 计算 fingerprint
        fp_parts = [
            wc.current_version_id or "",
            normalized_filename,
            summary.id if summary else "",
            ",".join(str(item.id) for item in suggestions),
            "jieba-v1",
        ]
        source_fingerprint = hashlib.sha256(
            "|".join(fp_parts).encode("utf-8")
        ).hexdigest()

        combined = " ".join([
            filename_terms,
            category_terms,
            metadata_terms,
            summary_terms,
        ]).strip()

        # 幂等 upsert：通过 working_copy_id 唯一约束
        existing = self.db.query(DocumentSearchProfile).filter(
            DocumentSearchProfile.working_copy_id == working_copy_id
        ).first()

        if existing:
            user_id = doc.user_id if doc else existing.user_id
            existing.user_id = user_id
            existing.status = wc.status or "ACTIVE"
            existing.normalized_filename = normalized_filename or None
            existing.filename_search_text = filename_terms or None
            existing.category_search_text = category_terms or None
            existing.metadata_search_text = metadata_terms or None
            existing.summary_search_text = summary_terms or None
            existing.combined_search_text = combined or None
            existing.source_fingerprint = source_fingerprint
            # PostgreSQL 由 migration trigger 与同事务显式刷新共同保证 search_vector；
            # SQLite 单元测试仅把此列视为普通文本占位。
            profile = existing
        else:
            user_id = doc.user_id if doc else ""
            profile = DocumentSearchProfile(
                id=str(__import__("uuid").uuid4()),
                user_id=user_id,
                workspace_id=wc.workspace_id,
                working_copy_id=working_copy_id,
                document_id=wc.document_id,
                document_version_id=wc.current_version_id or "",
                status=wc.status or "ACTIVE",
                normalized_filename=normalized_filename or None,
                filename_search_text=filename_terms or None,
                category_search_text=category_terms or None,
                metadata_search_text=metadata_terms or None,
                summary_search_text=summary_terms or None,
                combined_search_text=combined or None,
                source_fingerprint=source_fingerprint,
            )
            self.db.add(profile)

        self.db.flush()
        self._refresh_postgresql_search_vector(profile_id=profile.id)
        return {"ok": True, "profile_id": profile.id}

    def deactivate_profile(self, working_copy_id: str) -> dict[str, Any]:
        """将工作副本投影标记为 INACTIVE，不再参与检索。

        在文件进入回收站时调用。
        """
        profile = self.db.query(DocumentSearchProfile).filter(
            DocumentSearchProfile.working_copy_id == working_copy_id
        ).first()
        if profile:
            profile.status = "INACTIVE"
            self.db.flush()
        return {"ok": True}

    def backfill_profiles(
        self, batch_size: int = 100
    ) -> dict[str, Any]:
        """为所有 ACTIVE 工作副本补齐检索投影。

        分页执行，每批 batch_size 条，不把整个工作区加载到内存。
        幂等：已有投影的工作副本不会产生重复。
        """
        processed = 0

        while True:
            working_copies = (
                self.db.query(WorkingCopy)
                .filter(WorkingCopy.status == "ACTIVE")
                .outerjoin(
                    DocumentSearchProfile,
                    DocumentSearchProfile.working_copy_id == WorkingCopy.id,
                )
                .filter(DocumentSearchProfile.id.is_(None))
                # 查询条件会随着本批 upsert 改变；不能使用 offset，否则会跳过
                # 后续仍未投影的工作副本。
                .limit(batch_size)
                .all()
            )

            if not working_copies:
                break

            for wc in working_copies:
                self.upsert_current_profile(wc.id)
                processed += 1

            self.db.flush()

        return {"ok": True, "processed": processed}

    def reconcile_profiles(
        self, batch_size: int = 100
    ) -> dict[str, Any]:
        """对比事实表状态与 source_fingerprint，修复陈旧或缺失投影。

        分页执行，每批 batch_size 条。
        对比失败且 fingerprint 不匹配时重新 upsert。
        """
        fixed = 0
        last_profile_id: str | None = None

        while True:
            profiles = (
                self.db.query(DocumentSearchProfile)
                .filter(DocumentSearchProfile.status == "ACTIVE")
                .filter(
                    DocumentSearchProfile.id > last_profile_id
                    if last_profile_id is not None
                    else sa.true()
                )
                .order_by(DocumentSearchProfile.id.asc())
                .limit(batch_size)
                .all()
            )

            if not profiles:
                break

            for profile in profiles:
                last_profile_id = profile.id
                # 检查工作副本是否仍为 ACTIVE
                wc = self.db.query(WorkingCopy).filter(
                    WorkingCopy.id == profile.working_copy_id
                ).first()

                if not wc or wc.status != "ACTIVE":
                    profile.status = "INACTIVE"
                    fixed += 1
                    continue

                # 检查 fingerprint 是否匹配
                normalized_filename = _normalize_text(wc.filename or "")
                summary = self.db.query(DocumentSummary).filter(
                    DocumentSummary.document_id == wc.document_id,
                    DocumentSummary.document_version_id == wc.current_version_id,
                    DocumentSummary.status == "COMPLETED",
                ).first()

                suggestions = self.db.query(DocumentCategorySuggestion).filter(
                    DocumentCategorySuggestion.document_id == wc.document_id,
                    DocumentCategorySuggestion.document_version_id == wc.current_version_id,
                    DocumentCategorySuggestion.status.in_([
                        "SUGGESTED", "AUTO_APPLIED", "CONFIRMED",
                    ]),
                ).order_by(DocumentCategorySuggestion.rank.asc()).all()

                fp_parts = [
                    wc.current_version_id or "",
                    normalized_filename,
                    summary.id if summary else "",
                    ",".join(str(item.id) for item in suggestions),
                    "jieba-v1",
                ]
                expected_fp = hashlib.sha256(
                    "|".join(fp_parts).encode("utf-8")
                ).hexdigest()

                if profile.source_fingerprint != expected_fp:
                    self.upsert_current_profile(profile.working_copy_id)
                    fixed += 1

        # reconciliation 还必须发现完全缺失的投影；单独调用 backfill 保持可恢复、幂等。
        backfill_result = self.backfill_profiles(batch_size=batch_size)
        return {
            "ok": True,
            "fixed": fixed,
            "backfilled": int(backfill_result.get("processed") or 0),
        }

    def refresh_profiles_for_document_version(
        self,
        *,
        document_id: str,
        document_version_id: str,
    ) -> int:
        """刷新引用指定当前内容版本的活动工作副本投影。

        摘要和分类在异步任务中写入后必须调用本方法，避免把投影更新责任
        交给查询请求；只处理当前版本和 ACTIVE 工作副本，不能影响历史版本。
        """
        copies = (
            self.db.query(WorkingCopy.id)
            .filter(
                WorkingCopy.document_id == document_id,
                WorkingCopy.current_version_id == document_version_id,
                WorkingCopy.status == "ACTIVE",
            )
            .all()
        )
        for (working_copy_id,) in copies:
            self.upsert_current_profile(working_copy_id)
        return len(copies)

    def _refresh_postgresql_search_vector(self, *, profile_id: str) -> None:
        """在 PostgreSQL 写入加权 TSVECTOR，SQLite 测试不执行此 SQL。

        迁移中的 trigger 负责数据库级兜底；这里同步写入保证同一事务内
        新建或更新投影后即可被 GIN 检索，不能依赖后续查询再回填。
        """
        bind = getattr(self.db, "bind", None)
        if bind is None or bind.dialect.name != "postgresql":
            return
        self.db.execute(
            sa.text(
                """
                UPDATE document_search_profiles
                SET search_vector =
                    setweight(to_tsvector('simple', coalesce(filename_search_text, '')), 'A') ||
                    setweight(to_tsvector('simple', coalesce(category_search_text, '')), 'B') ||
                    setweight(to_tsvector('simple', coalesce(metadata_search_text, '')), 'C') ||
                    setweight(to_tsvector('simple', coalesce(summary_search_text, '')), 'D')
                WHERE id = :profile_id
                """
            ),
            {"profile_id": profile_id},
        )

    def _tokenize(self, text: str) -> str:
        """使用配置的分词器将文本转换为空格分隔的词项。

        如果不配置分词器，直接返回原文（适用于不依赖分词的测试场景）。
        """
        if not text:
            return ""
        if self.tokenizer:
            terms = self.tokenizer.tokenize(text)
            return " ".join(terms)
        # 无分词器时的轻量化：按空格和中英文边界切分
        terms = re.findall(r"[a-z0-9][a-z0-9._-]*|[\u4e00-\u9fff]+", text.lower())
        return " ".join(terms)

    def _build_metadata_text(
        self,
        summary: DocumentSummary | None,
        suggestions: list[DocumentCategorySuggestion],
    ) -> str:
        """从摘要 JSON 和分类建议中提取元数据文本。

        包括年份、关键词、实体等字段，作为 metadata_search_text 的来源。
        """
        parts = []

        if summary and summary.summary_json:
            sj = summary.summary_json if isinstance(summary.summary_json, dict) else {}
            for key in ("year", "keywords", "entities", "department", "document_number"):
                val = sj.get(key)
                if val:
                    if isinstance(val, list):
                        parts.extend(str(v) for v in val)
                    elif isinstance(val, str):
                        parts.append(val)

        for sug in suggestions:
            if sug.category_path_json:
                parts.extend(str(p) for p in sug.category_path_json)

        return " ".join(parts)
