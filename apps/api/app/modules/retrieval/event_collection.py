"""明确事件“全部材料”请求的受控同义匹配与同目录集合扩展。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_

from app.core.logging import log_event
from app.db.models import (
    ManagedFile,
    ManagedFileRevision,
    ManagedFileSearchProfile,
    ManagedRoot,
    WorkingCopy,
)


MAX_EVENT_DIRECTORIES = 8
MAX_EVENT_COLLECTION_FILES = 50
_COLLECTION_MARKERS = ("全部", "所有", "全套")
_EVENT_ACTION_EQUIVALENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("授牌", ("授牌", "揭牌")),
)


@dataclass(frozen=True)
class EventCollectionRequest:
    """经过确定性门槛确认的事件集合查询。"""

    subject_phrase: str
    action_phrases: tuple[str, ...]


def resolve_event_collection_request(parsed_query: Any) -> EventCollectionRequest | None:
    """仅识别“明确年月 + 相关 + 全部材料”的窄范围事件查询。"""

    original = str(getattr(parsed_query, "original", "") or "")
    cleaned = str(getattr(parsed_query, "cleaned", "") or "").strip()
    if (
        str(getattr(parsed_query, "relation_mode", "")) != "RELATED"
        or not getattr(parsed_query, "year", None)
        or not getattr(parsed_query, "month", None)
        or not any(marker in original for marker in _COLLECTION_MARKERS)
    ):
        return None
    for action, equivalents in _EVENT_ACTION_EQUIVALENTS:
        if action not in cleaned:
            continue
        subject = cleaned.replace(action, " ", 1).strip(" 的与和及")
        if len(subject) >= 4:
            return EventCollectionRequest(
                subject_phrase=subject,
                action_phrases=equivalents,
            )
    return None


class EventCollectionSearchService:
    """由年月和主题动作双重验证的锚点扩展同一叶子目录配套文件。"""

    def __init__(
        self,
        *,
        db: Any,
        workspace_id: str,
        phrase_strategy: Any,
        stage1_service: Any,
    ) -> None:
        self.db = db
        self.workspace_id = workspace_id
        self.phrase_strategy = phrase_strategy
        self.stage1_service = stage1_service

    def search(
        self,
        *,
        original_query: str,
        parsed_query: Any,
        scope: Any,
        request: EventCollectionRequest,
    ) -> dict[str, Any]:
        """先取主题与事件动作交集，再扩展锚点所在的事件专属目录。"""

        topic_result = self.phrase_strategy.search(
            original_query=original_query,
            parsed_query=parsed_query,
            scope=scope,
            phrases=[request.subject_phrase],
            require_body_evidence=False,
        )
        action_result = self.phrase_strategy.search(
            original_query=original_query,
            parsed_query=parsed_query,
            scope=scope,
            phrases=list(request.action_phrases),
            require_body_evidence=False,
        )
        topic_by_id = _results_by_id(topic_result.get("results"))
        action_by_id = _results_by_id(action_result.get("results"))
        anchor_ids = set(topic_by_id).intersection(action_by_id)
        anchors: list[dict[str, Any]] = []
        for result_id in anchor_ids:
            item = {**topic_by_id[result_id]}
            item["relevance_tier"] = "SUPPORTED"
            item["match_reasons"] = _append_reason(
                item.get("match_reasons"),
                "已同时确认事件主题、年月与授牌/揭牌动作",
            )
            anchors.append(item)
        anchors.sort(key=_result_sort_key)

        collection_files, expansion_partial = self._expand_directories(
            anchors=anchors,
            request=request,
            parsed_query=parsed_query,
            scope=scope,
        )
        # 唯一日期事件目录一旦解析成功，它就是“全部材料”的确定范围；不能再把
        # 其他目录中内容相似的锚点并入集合。找不到唯一目录时才保留锚点结果降级。
        results = collection_files or anchors
        results = sorted(results, key=_result_sort_key)
        verified_count = sum(
            1 for item in results if item.get("relevance_tier") == "SUPPORTED"
        )
        expanded_count = max(0, len(results) - verified_count)
        partial = (
            bool(topic_result.get("partial"))
            or bool(action_result.get("partial"))
            or expansion_partial
        )
        log_event(
            "retrieval.event_collection.completed",
            tool_name="hybrid-search",
            status="DEGRADED" if partial else "COMPLETED",
            workspace_id=self.workspace_id,
            anchor_count=verified_count,
            expanded_count=expanded_count,
            result_count=len(results),
            partial=partial,
            message="事件全部材料检索与同目录扩展完成",
        )
        return {
            "ok": True,
            "kind": "workspace_file_search",
            "query": original_query,
            "total_returned": len(results),
            "supported_count": len(results),
            "possible_count": 0,
            "partial": partial,
            "candidate_limit_reached": bool(
                topic_result.get("candidate_limit_reached")
                or action_result.get("candidate_limit_reached")
                or expansion_partial
            ),
            "results": results,
            "user_message": (
                f"找到 {verified_count} 个内容已验证的事件材料，并补充 "
                f"{expanded_count} 个同一日期事件目录内的配套文件。"
                if results
                else "未找到同时满足主题、年月和事件动作的材料。"
            ),
        }

    def _expand_directories(
        self,
        *,
        anchors: list[dict[str, Any]],
        request: EventCollectionRequest,
        parsed_query: Any,
        scope: Any,
    ) -> tuple[list[dict[str, Any]], bool]:
        """唯一解析受管源事件目录，并递归列出其中全部扫描文件。"""

        if str(getattr(scope, "scope_mode", "global") or "global") != "global":
            return [], False

        anchor_working_copy_ids = [
            str(item.get("working_copy_id") or "")
            for item in anchors
            if item.get("working_copy_id")
        ]
        anchor_revision_ids = [
            str(item.get("managed_file_revision_id") or "")
            for item in anchors
            if item.get("managed_file_revision_id")
        ]
        anchor_managed_file_ids = {
            str(item.get("managed_file_id") or "")
            for item in anchors
            if item.get("managed_file_id")
        }
        if anchor_working_copy_ids:
            anchor_managed_file_ids.update(
                str(value)
                for (value,) in self.db.query(WorkingCopy.managed_file_id)
                .filter(
                    WorkingCopy.id.in_(anchor_working_copy_ids),
                    WorkingCopy.workspace_id == self.workspace_id,
                    WorkingCopy.status == "ACTIVE",
                )
                .all()
                if value
            )
        if anchor_revision_ids:
            anchor_managed_file_ids.update(
                str(value)
                for (value,) in self.db.query(ManagedFileRevision.managed_file_id)
                .filter(ManagedFileRevision.id.in_(anchor_revision_ids))
                .all()
                if value
            )
        anchor_managed_file_ids.discard("")
        if not anchor_managed_file_ids:
            return [], False

        anchor_rows = (
            self.db.query(ManagedFile)
            .filter(
                ManagedFile.id.in_(anchor_managed_file_ids),
                ManagedFile.status == "ACTIVE",
            )
            .all()
        )
        directory_keys: list[tuple[str, str]] = []
        for row in anchor_rows:
            parent = _event_directory_path(
                row.relative_path,
                subject_phrase=request.subject_phrase,
                action_phrases=request.action_phrases,
                year=int(getattr(parsed_query, "year")),
                month=int(getattr(parsed_query, "month")),
            )
            if not parent:
                continue
            key = (str(row.root_id), parent)
            if key not in directory_keys:
                directory_keys.append(key)
        # “全部材料”只能落到唯一事件目录。多个同名日期目录属于真实范围歧义，
        # 不得把它们合并执行；这里保留已验证锚点并标记 partial。
        if len(directory_keys) > MAX_EVENT_DIRECTORIES:
            return [], True
        if len(directory_keys) != 1:
            return [], bool(directory_keys)

        root_id, parent = directory_keys[0]
        root = self.db.get(ManagedRoot, root_id)
        rows = (
            self.db.query(
                ManagedFile,
                ManagedFileRevision,
                ManagedFileSearchProfile,
            )
            .outerjoin(
                ManagedFileRevision,
                and_(
                    ManagedFileRevision.managed_file_id == ManagedFile.id,
                    ManagedFileRevision.is_current.is_(True),
                ),
            )
            .outerjoin(
                ManagedFileSearchProfile,
                ManagedFileSearchProfile.managed_file_revision_id
                == ManagedFileRevision.id,
            )
            .filter(
                ManagedFile.root_id == root_id,
                ManagedFile.status == "ACTIVE",
                ManagedFile.relative_path.startswith(f"{parent}/"),
            )
            .order_by(ManagedFile.relative_path.asc())
            .limit(MAX_EVENT_COLLECTION_FILES + 1)
            .all()
        )
        if len(rows) > MAX_EVENT_COLLECTION_FILES:
            rows = rows[:MAX_EVENT_COLLECTION_FILES]
            partial = True
        else:
            partial = False

        managed_file_ids = [str(managed_file.id) for managed_file, _revision, _profile in rows]
        working_copies = (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.managed_file_id.in_(managed_file_ids or ["__none__"]),
                WorkingCopy.status == "ACTIVE",
            )
            .all()
        )
        working_copy_by_file = {
            str(row.managed_file_id): row for row in working_copies
        }
        working_copy_ids = [str(row.id) for row in working_copies]
        enriched = self.stage1_service.enrich_working_copy_ids(
            working_copy_ids=working_copy_ids,
            scope=scope,
        )
        enriched_by_id = {
            str(item.get("working_copy_id") or ""): item
            for item in enriched
            if item.get("working_copy_id")
        }
        anchors_by_managed_file = {
            str(item.get("managed_file_id") or ""): item
            for item in anchors
            if item.get("managed_file_id")
        }
        anchors_by_working_copy = {
            str(item.get("working_copy_id") or ""): item
            for item in anchors
            if item.get("working_copy_id")
        }
        anchors_by_revision = {
            str(item.get("managed_file_revision_id") or ""): item
            for item in anchors
            if item.get("managed_file_revision_id")
        }
        collection: list[dict[str, Any]] = []
        for managed_file, revision, profile in rows:
            working_copy = working_copy_by_file.get(str(managed_file.id))
            working_copy_id = str(working_copy.id) if working_copy is not None else ""
            item = (
                dict(enriched_by_id[working_copy_id])
                if working_copy_id in enriched_by_id
                else _event_file_result(
                    root=root,
                    managed_file=managed_file,
                    revision=revision,
                    profile=profile,
                    working_copy=working_copy,
                )
            )
            item.update(
                {
                    "managed_file_id": managed_file.id,
                    "managed_file_revision_id": revision.id if revision is not None else None,
                    "root_key": root.root_key if root is not None else None,
                    "relative_path": managed_file.relative_path,
                    "filename": managed_file.filename,
                    "source_analysis_status": revision.status if revision is not None else "PENDING",
                }
            )
            notice = str((profile.topic_summary_json or {}).get("analysis_notice") or "") if profile is not None else ""
            if notice:
                item["source_analysis_notice"] = notice
                item["summary"] = str(item.get("summary") or notice)
            anchor = (
                anchors_by_managed_file.get(str(managed_file.id))
                or anchors_by_working_copy.get(working_copy_id)
                or anchors_by_revision.get(str(revision.id) if revision is not None else "")
            )
            if anchor is not None:
                collection.append(
                    {
                        **item,
                        **anchor,
                        "managed_file_id": managed_file.id,
                        "managed_file_revision_id": revision.id if revision is not None else None,
                        "root_key": root.root_key if root is not None else None,
                        "relative_path": managed_file.relative_path,
                        "filename": managed_file.filename,
                        "source_analysis_status": revision.status if revision is not None else "PENDING",
                    }
                )
                continue
            companion = {**item, "relevance_tier": "RELATED"}
            companion["match_reasons"] = _append_reason(
                item.get("match_reasons"),
                "位于唯一匹配年月、主题和事件动作的业务目录",
            )
            companion.pop("_score", None)
            companion.pop("_hit_source", None)
            collection.append(companion)
        return collection, partial


def _event_file_result(
    *,
    root: ManagedRoot | None,
    managed_file: ManagedFile,
    revision: ManagedFileRevision | None,
    profile: ManagedFileSearchProfile | None,
    working_copy: WorkingCopy | None,
) -> dict[str, Any]:
    """为目录成员生成不依赖正文成功状态的安全文件投影。"""

    if profile is not None and profile.status == "ACTIVE":
        summary = str(profile.summary or "")
        years = list(profile.years_json or [])
    else:
        summary = ""
        years = []
    revision_status = str(revision.status if revision is not None else "PENDING")
    if not summary and revision_status == "FAILED":
        summary = "源侧正文分析未完成；当前仍可按受管目录关系和文件元数据列出。"
    elif not summary and revision_status not in {"READY", "STALE"}:
        summary = "源侧正文分析尚未完成；当前仍可按受管目录关系和文件元数据列出。"

    if working_copy is not None:
        return {
            "resource_type": "WORKING_COPY",
            "working_copy_id": working_copy.id,
            "document_id": working_copy.document_id,
            "document_version_id": working_copy.current_version_id,
            "can_open": True,
            "availability_message": "工作副本已就绪。",
            "category_path": [],
            "year": years,
            "summary": summary,
            "overview": summary,
        }
    return {
        "resource_type": "MANAGED_SOURCE",
        "working_copy_id": None,
        "document_id": (
            revision.analysis_document_id
            if revision is not None and revision.status == "READY"
            else None
        ),
        "document_version_id": (
            revision.analysis_document_version_id
            if revision is not None and revision.status == "READY"
            else None
        ),
        "can_open": False,
        "availability_message": (
            "已从受管原始目录列出，工作副本正在后台生成。"
            if revision_status == "READY"
            else "已从受管原始目录列出，正文分析或工作副本尚未就绪。"
        ),
        "root_key": root.root_key if root is not None else None,
        "category_path": [],
        "year": years,
        "summary": summary,
        "overview": summary,
    }


def _results_by_id(value: Any) -> dict[str, dict[str, Any]]:
    return {
        _result_id(item): item
        for item in (value if isinstance(value, list) else [])
        if isinstance(item, dict) and _result_id(item)
    }


def _result_id(item: dict[str, Any]) -> str:
    return str(
        item.get("working_copy_id")
        or item.get("managed_file_revision_id")
        or item.get("document_version_id")
        or item.get("document_id")
        or ""
    )


def _append_reason(value: Any, reason: str) -> list[str]:
    reasons = [str(item) for item in value if str(item)] if isinstance(value, list) else []
    if reason not in reasons:
        reasons.append(reason)
    return reasons[:6]


def _event_directory_path(
    relative_path: Any,
    *,
    subject_phrase: str,
    action_phrases: tuple[str, ...],
    year: int,
    month: int,
) -> str:
    """从外到内选择首个匹配事件的祖先目录，避免嵌套照片目录缩小集合。"""

    normalized = re.sub(r"/+", "/", str(relative_path or "").replace("\\", "/")).strip("/")
    directories = [part for part in normalized.split("/")[:-1] if part]
    for index, leaf in enumerate(directories, start=1):
        if _directory_matches_event(
            leaf,
            subject_phrase=subject_phrase,
            action_phrases=action_phrases,
            year=year,
            month=month,
        ):
            return "/".join(directories[:index])
    return ""


def _compact(value: Any) -> str:
    return re.sub(r"[\s_\-—/\\]+", "", str(value or "").casefold())


def _directory_matches_event(
    leaf: str,
    *,
    subject_phrase: str,
    action_phrases: tuple[str, ...],
    year: int,
    month: int,
) -> bool:
    """要求叶子目录同时包含明确年月、完整主题和受控事件动作。"""

    compact_leaf = _compact(leaf)
    has_subject = _compact(subject_phrase) in compact_leaf
    has_action = any(_compact(action) in compact_leaf for action in action_phrases)
    date_forms = (
        f"{year}{month:02d}",
        f"{year}年{month}月",
        f"{year}年{month:02d}月",
    )
    has_date = any(_compact(value) in compact_leaf for value in date_forms)
    return has_subject and has_action and has_date


def _result_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    return (
        str(item.get("relative_path") or item.get("filename") or ""),
        _result_id(item),
    )
