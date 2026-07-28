"""分类建议歧义选择的持久化服务。

选择项保存工作副本、版本、建议和 taxonomy 分类的完整绑定；普通页面只看到文件名、
分类标签和后端签发的 option_id。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    ClassificationClarification,
    DocumentCategorySuggestion,
    WorkingCopy,
    new_uuid,
    utcnow,
)
from app.modules.file_lifecycle.shared_access import (
    CanonicalWorkingFileError,
    CanonicalWorkingFileResolver,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.schemas import CategoryNode


class ClassificationClarificationError(ValueError):
    """分类选择不存在、越权、过期或已被文件状态变化作废。"""


@dataclass(frozen=True)
class ResolvedClassificationSelection:
    """后端验证后的分类决定参数。"""

    clarification_id: str
    option_id: str
    action: str
    suggestion_id: str
    working_copy_id: str
    document_version_id: str
    target_category_id: str | None
    relation_role: str
    agent_run_id: str | None


class ClassificationClarificationService:
    """管理当前用户会话中的文件与分类建议选择。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db
        self.resolver = CanonicalWorkingFileResolver(db)

    def create(
        self,
        *,
        conversation_id: str,
        user_id: str,
        agent_run_id: str | None,
        action: str,
        suggestion_ids: list[str],
        target_category_id: str | None = None,
        target_category_ids: list[str] | None = None,
        relation_role: str = "RELATED",
    ) -> ClassificationClarification:
        """为后端已召回的建议创建选择卡，并替代旧的待选择卡。"""

        now = utcnow()
        (
            self.db.query(ClassificationClarification)
            .filter(
                ClassificationClarification.conversation_id == conversation_id,
                ClassificationClarification.user_id == user_id,
                ClassificationClarification.status == "WAITING_SELECTION",
            )
            .update(
                {
                    ClassificationClarification.status: "SUPERSEDED",
                    ClassificationClarification.resolved_at: now,
                },
                synchronize_session=False,
            )
        )
        suggestions = (
            self.db.query(DocumentCategorySuggestion)
            .filter(DocumentCategorySuggestion.id.in_(list(dict.fromkeys(suggestion_ids))))
            .order_by(
                DocumentCategorySuggestion.document_id.asc(),
                DocumentCategorySuggestion.rank.asc(),
            )
            .all()
        )
        options: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        normalized_targets = list(
            dict.fromkeys(
                str(item).strip()
                for item in (
                    target_category_ids
                    if target_category_ids is not None
                    else [target_category_id]
                )
                if str(item or "").strip()
            )
        )
        targets: list[str | None] = normalized_targets or [None]
        target_labels = _taxonomy_labels()
        for suggestion in suggestions:
            try:
                canonical = self.resolver.resolve_suggestion(suggestion)
            except CanonicalWorkingFileError:
                continue
            for target_id in targets:
                key = (
                    canonical.working_copy.id,
                    f"{suggestion.id}:{target_id or ''}",
                )
                if key in seen:
                    continue
                seen.add(key)
                target_label = target_labels.get(str(target_id or ""))
                if target_id and target_label is None:
                    continue
                options.append(
                    {
                        "id": new_uuid(),
                        "working_copy_id": canonical.working_copy.id,
                        "document_version_id": canonical.document_version.id,
                        "suggestion_id": suggestion.id,
                        "category_id": suggestion.category_id,
                        "target_category_id": target_id,
                        "relation_role": relation_role,
                        "filename": canonical.working_copy.filename,
                        "category_label": (
                            target_label
                            or " / ".join(
                                str(item)
                                for item in suggestion.category_path_json
                                if str(item)
                            )
                            or suggestion.category_name
                        ),
                    }
                )
        if not options:
            raise ClassificationClarificationError("没有可确认的活动共享文件分类")
        record = ClassificationClarification(
            conversation_id=conversation_id,
            user_id=user_id,
            agent_run_id=agent_run_id,
            action=action,
            options_json=options,
            status="WAITING_SELECTION",
            expires_at=now + timedelta(hours=24),
        )
        self.db.add(record)
        self.db.flush()
        return record

    def resolve(
        self,
        *,
        clarification_id: str,
        user_id: str,
        option_id: str,
    ) -> ResolvedClassificationSelection:
        """校验 option_id 并返回绑定参数；重复选择同一项保持幂等。"""

        record = (
            self.db.query(ClassificationClarification)
            .filter(ClassificationClarification.id == clarification_id)
            .with_for_update()
            .one_or_none()
        )
        if record is None or record.user_id != user_id:
            raise ClassificationClarificationError("分类选择不存在")
        now = utcnow()
        if record.status == "WAITING_SELECTION" and _is_expired(
            record.expires_at, now
        ):
            record.status = "EXPIRED"
            record.resolved_at = now
            self.db.flush()
            raise ClassificationClarificationError("分类选择已过期，请重新发起")
        if record.status == "RESOLVED":
            if record.selected_option_id != option_id:
                raise ClassificationClarificationError("分类选择已经处理")
            saved = dict(record.resolution_json or {})
            return _selection_from_payload(record, option_id, saved)
        if record.status != "WAITING_SELECTION":
            raise ClassificationClarificationError("分类选择已经失效")
        option = next(
            (
                item
                for item in record.options_json
                if isinstance(item, dict) and str(item.get("id")) == option_id
            ),
            None,
        )
        if option is None:
            raise ClassificationClarificationError("选择项不属于当前分类任务")
        suggestion = self.db.get(
            DocumentCategorySuggestion, str(option.get("suggestion_id") or "")
        )
        if suggestion is None:
            raise ClassificationClarificationError("分类建议已经失效")
        try:
            canonical = self.resolver.resolve_suggestion(suggestion)
        except CanonicalWorkingFileError as exc:
            record.status = "SUPERSEDED"
            record.resolved_at = now
            self.db.flush()
            raise ClassificationClarificationError(str(exc)) from exc
        if (
            canonical.working_copy.id != option.get("working_copy_id")
            or canonical.document_version.id != option.get("document_version_id")
        ):
            record.status = "SUPERSEDED"
            record.resolved_at = now
            self.db.flush()
            raise ClassificationClarificationError("文件版本已经变化，请重新选择")
        return _selection_from_payload(record, option_id, option)

    def mark_resolved(
        self,
        *,
        clarification_id: str,
        user_id: str,
        option_id: str,
        feedback_id: str,
    ) -> None:
        """分类事务成功后再消费选择卡，失败时保留选择供用户重试。"""

        record = (
            self.db.query(ClassificationClarification)
            .filter(ClassificationClarification.id == clarification_id)
            .with_for_update()
            .one_or_none()
        )
        if (
            record is None
            or record.user_id != user_id
            or record.status not in {"WAITING_SELECTION", "RESOLVED"}
        ):
            raise ClassificationClarificationError("分类选择已经失效")
        if record.status == "RESOLVED":
            if record.selected_option_id != option_id:
                raise ClassificationClarificationError("分类选择已经处理")
            return
        option = next(
            item
            for item in record.options_json
            if isinstance(item, dict) and str(item.get("id")) == option_id
        )
        record.status = "RESOLVED"
        record.selected_option_id = option_id
        record.resolution_json = {**option, "feedback_id": feedback_id}
        record.resolved_at = utcnow()
        self.db.flush()

    def get_public(self, *, clarification_id: str, user_id: str) -> dict[str, Any]:
        """返回不含内部业务 ID 的选择卡。"""

        record = self.db.get(ClassificationClarification, clarification_id)
        if record is None or record.user_id != user_id:
            raise ClassificationClarificationError("分类选择不存在")
        if record.status == "WAITING_SELECTION" and _is_expired(
            record.expires_at, utcnow()
        ):
            record.status = "EXPIRED"
            record.resolved_at = utcnow()
            self.db.flush()
        return self.public_payload(record)

    @staticmethod
    def public_payload(record: ClassificationClarification) -> dict[str, Any]:
        """投影文件名和分类标签，隐藏 suggestion/document 等内部 ID。"""

        options = [
            {
                "id": str(item.get("id") or ""),
                "filename": str(item.get("filename") or ""),
                "category_label": str(item.get("category_label") or ""),
            }
            for item in record.options_json
            if isinstance(item, dict)
        ]
        return {
            "id": record.id,
            "status": record.status,
            "prompt": "请选择要确认或纠正的具体文件分类。",
            "action": record.action,
            "options": options,
            "expires_at": record.expires_at.isoformat(),
        }


def _selection_from_payload(
    record: ClassificationClarification,
    option_id: str,
    payload: dict[str, Any],
) -> ResolvedClassificationSelection:
    """把持久化选项转换为内部结构化决定参数。"""

    return ResolvedClassificationSelection(
        clarification_id=record.id,
        option_id=option_id,
        action=record.action,
        suggestion_id=str(payload.get("suggestion_id") or ""),
        working_copy_id=str(payload.get("working_copy_id") or ""),
        document_version_id=str(payload.get("document_version_id") or ""),
        target_category_id=(
            str(payload.get("target_category_id"))
            if payload.get("target_category_id")
            else None
        ),
        relation_role=str(payload.get("relation_role") or "RELATED"),
        agent_run_id=record.agent_run_id,
    )


def _is_expired(expires_at: datetime, now: datetime) -> bool:
    """兼容 SQLite 朴素时间与 PostgreSQL 时区时间。"""

    left = expires_at
    right = now
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left <= right


def _taxonomy_labels() -> dict[str, str]:
    """生成稳定分类 ID 到用户可读路径的索引。"""

    labels: dict[str, str] = {}

    def walk(node: CategoryNode, parents: list[str]) -> None:
        path = [*parents, node.name]
        if node.id:
            labels[node.id] = " / ".join(path)
        for child in node.children:
            walk(child, path)

    for root in load_default_taxonomy().categories:
        walk(root, [])
    return labels
